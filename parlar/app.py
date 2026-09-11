"""Orquestador del daemon ParlAR.

Ownership y threading
---------------------
El hilo ``trabajador`` es el único dueño de Segmentador y de las dos
estrategias STT. UI, control y hotkeys pueden pedir transiciones, pero
``_transicion_lock`` las serializa. ``_estado_cv`` protege estado/health y
``_salida_lock`` linealiza la validación de generación con cada efecto
externo. El callback de PortAudio solamente copia y encola ``FrameAudio`` ya
etiquetado por la generación de la apertura que lo produjo.

STOP corta la aceptación de audio y deja que el worker cierre la frase
aceptada. START durante STOPPING es una sustitución explícita: invalida la
generación anterior. Shutdown es terminal: invalida primero, espera al worker
y recién entonces cierra sinks.
"""

import sys
import threading
import time
from enum import Enum
from typing import Optional

from .capturador_audio import CapturadorMic, Segmentador, crear_vad
from .cliente_guionar import crear_cliente
from .config import Config
from .control import ServidorControl, normalizar_comando
from .daemon_atajos import DaemonAtajos
from .entrega import EstadoEntrega, ResultadoDistribucion, ResultadoSink
from .indicador import crear_ui
from .inyector_salida import Inyector
from .motor_transcripcion import MotorWhisper, TranscriptorFrase, TranscriptorStreaming
from .procesador_texto import ProcesadorTexto
from .sesion import crear_salida_sesion


class EstadoApp(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"


class App:
    def __init__(self, cfg: Config, *, motor=None, frases=None, streaming=None,
                 proc=None, inyector=None, guionar=None, sesion=None, mic=None,
                 ui=None, control=None, atajos=None,
                 vad_factory=crear_vad, segmentador_factory=Segmentador):
        self.cfg = cfg
        self.grabando = threading.Event()
        self.saliendo = threading.Event()
        self._necesita_espacio = False
        self.ultima_entrega = ResultadoDistribucion.omitido()

        self._transicion_lock = threading.Lock()
        self._estado_cv = threading.Condition()
        self._salida_lock = threading.RLock()
        self._estado = EstadoApp.IDLE
        self._ultimo_error = ""
        self._fallo_fatal_worker = False
        self._generacion = 0
        self._sesion_activa: Optional[int] = None
        self._modo_solicitado = cfg.mode
        self._modo_por_sesion: dict[int, str] = {}
        self._stops_listos: set[int] = set()
        self._errores_stop: set[int] = set()
        self._shutdown_completo = threading.Event()
        self._trabajador_hilo: Optional[threading.Thread] = None
        self._vad_factory = vad_factory
        self._segmentador_factory = segmentador_factory

        print("=" * 60)
        print("ParlAR: dictado local. Nada sale de esta máquina.")
        print("=" * 60)

        if motor is None and (frases is None or streaming is None):
            motor = MotorWhisper(cfg.model_size, cfg.device, cfg.compute_type,
                                 cfg.language, cfg.beam_size)
        self.motor = motor
        self.frases = frases or TranscriptorFrase(self.motor)
        self.streaming = streaming or TranscriptorStreaming(
            self.motor, cfg.sample_rate, cfg.stream_interval_s, cfg.stream_trim_s)
        self.proc = proc or ProcesadorTexto(
            cfg.remove_fillers, cfg.voice_commands, cfg.rewrite_mode,
            cfg.ollama_model, cfg.ollama_url, cfg.comando_enviar)
        self.inyector = inyector or Inyector(
            cfg.injector, cfg.type_delay_ms, cfg.notify)
        self.guionar = guionar or crear_cliente(cfg.guionar, cfg.guionar_socket)
        self.sesion = sesion or crear_salida_sesion(cfg.guardar_sesion)
        self.salidas = [self.inyector, self.guionar, self.sesion]
        if cfg.guionar:
            print(f"[guionar] integración activa (socket: {self.guionar.ruta})")
        self.mic = mic or CapturadorMic(cfg.sample_rate, cfg.frame_samples)
        self.ui = ui or crear_ui(cfg.overlay, al_click=self.alternar)
        self.control = control or ServidorControl(self._atender_comando)
        self.atajos = atajos or DaemonAtajos(
            cfg.hotkey_toggle, cfg.hotkey_quit,
            al_alternar=self.alternar, al_salir=self.salir)

    # ------------------------------------------------------------ ciclo de vida

    @property
    def estado(self) -> EstadoApp:
        with self._estado_cv:
            return self._estado

    @property
    def generacion_activa(self) -> Optional[int]:
        with self._estado_cv:
            return self._sesion_activa

    def esperar_estado(self, estado: EstadoApp, timeout: float = 2.0) -> bool:
        """Espera determinista usada también por las regresiones de lifecycle."""
        with self._estado_cv:
            return self._estado_cv.wait_for(lambda: self._estado == estado, timeout)

    def _iniciar_trabajador(self):
        with self._estado_cv:
            if self._trabajador_hilo is not None and self._trabajador_hilo.is_alive():
                return
            if self._fallo_fatal_worker or self._estado in (
                    EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                return
            hilo = threading.Thread(
                target=self._trabajador, name="trabajador", daemon=True)
            self._trabajador_hilo = hilo
            hilo.start()

    def ejecutar(self):
        self._iniciar_trabajador()
        self.control.iniciar()
        self.atajos.iniciar()
        pista = (f"atajo {self.cfg.hotkey_toggle}" if self.atajos._listener
                 else "`parlarctl alternar`")
        print(f"[app] listo. Modo: {self.cfg.mode}. Iniciá/detené con {pista}, "
              f"o con un click en el punto del indicador.")
        try:
            self.ui.ejecutar()
        except KeyboardInterrupt:
            pass
        finally:
            self.salir()

    def alternar(self):
        estado = self.estado
        if estado in (EstadoApp.RECORDING, EstadoApp.STARTING):
            return self.detener_grabacion()
        return self.iniciar_grabacion()

    def iniciar_grabacion(self) -> bool:
        self._iniciar_trabajador()
        with self._transicion_lock:
            with self._estado_cv:
                if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                    self._ultimo_error = "la aplicación se está cerrando"
                    return False
                if self._fallo_fatal_worker:
                    self._ultimo_error = "el worker no está operativo"
                    return False
                if self._estado in (EstadoApp.STARTING, EstadoApp.RECORDING):
                    return True
                anterior = self._sesion_activa
                self._generacion += 1
                generacion = self._generacion
                self._estado = EstadoApp.STARTING
                self._ultimo_error = ""
                self._estado_cv.notify_all()

            with self._salida_lock:
                with self._estado_cv:
                    self._sesion_activa = generacion
                    self._modo_por_sesion = {generacion: self._modo_solicitado}
                    self._estado_cv.notify_all()
                if anterior is not None:
                    self.mic.descartar_pendientes(anterior)
                    self.guionar.enviar_parcial("")
                self.inyector.reiniciar_registro()
                self._necesita_espacio = False

            try:
                abierta = self.mic.iniciar(generacion)
                if abierta is False:
                    raise RuntimeError("el micrófono ya tiene una apertura activa")
            except Exception as exc:
                with self._salida_lock:
                    with self._estado_cv:
                        if self._sesion_activa == generacion:
                            self._sesion_activa = None
                        self._modo_por_sesion.pop(generacion, None)
                        self._estado = EstadoApp.ERROR
                        self._ultimo_error = f"micrófono: {exc}"
                        self._estado_cv.notify_all()
                self.grabando.clear()
                self.ui.fijar_estado("error")
                print(f"[app] no se pudo abrir el micrófono: {exc}", file=sys.stderr)
                return False

            with self._estado_cv:
                self._estado = EstadoApp.RECORDING
                self._estado_cv.notify_all()
            self.grabando.set()
            self.ui.fijar_estado("recording")
            print(f"[app] ● grabando (sesión {generacion})")
            return True

    def detener_grabacion(self, generacion_esperada: Optional[int] = None) -> bool:
        with self._transicion_lock:
            with self._estado_cv:
                if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                    return True
                if generacion_esperada is not None \
                        and self._sesion_activa != generacion_esperada:
                    return False
                if self._estado in (EstadoApp.IDLE, EstadoApp.ERROR):
                    return True
                if self._estado == EstadoApp.STOPPING:
                    return True
                if self._estado != EstadoApp.RECORDING:
                    return False
                generacion = self._sesion_activa
                self._estado = EstadoApp.STOPPING
                self._estado_cv.notify_all()

            self.grabando.clear()
            error = None
            try:
                self.mic.detener(vaciar=False)
            except Exception as exc:
                error = exc
                print(f"[app] falló el cierre del micrófono: {exc}", file=sys.stderr)

            with self._estado_cv:
                if generacion is not None:
                    self._stops_listos.add(generacion)
                    if error is not None:
                        self._errores_stop.add(generacion)
                        self._ultimo_error = f"micrófono: {error}"
                self._estado_cv.notify_all()
            self.ui.fijar_estado("transcribing")
            print(f"[app] ◌ deteniendo (sesión {generacion})")
            return error is None

    def cambiar_modo(self, modo: str) -> bool:
        if modo not in ("utterance", "streaming"):
            return False
        with self._transicion_lock:
            with self._estado_cv:
                if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                    return False
                self._modo_solicitado = modo
                self.cfg.mode = modo
                if self._sesion_activa is not None:
                    self._modo_por_sesion[self._sesion_activa] = modo
                self._estado_cv.notify_all()
        return True

    def salir(self):
        propietario = False
        with self._transicion_lock:
            with self._estado_cv:
                if self._estado == EstadoApp.CLOSED:
                    return
                if self._estado == EstadoApp.SHUTTING_DOWN:
                    espera = self._shutdown_completo
                else:
                    self._estado = EstadoApp.SHUTTING_DOWN
                    self._ultimo_error = ""
                    self.grabando.clear()
                    self.saliendo.set()
                    self._estado_cv.notify_all()
                    espera = self._shutdown_completo
                    propietario = True

            if propietario:
                with self._salida_lock:
                    with self._estado_cv:
                        self._sesion_activa = None
                        self._modo_por_sesion.clear()
                        self._estado_cv.notify_all()
                try:
                    self.mic.detener(vaciar=True)
                except Exception as exc:
                    print(f"[app] error cerrando micrófono: {type(exc).__name__}",
                          file=sys.stderr)

        if not propietario:
            espera.wait()
            return

        hilo = self._trabajador_hilo
        if hilo is threading.current_thread():
            # Si una acción del propio pipeline pide salir, otro hilo espera
            # su retorno antes de cerrar los recursos que todavía posee.
            threading.Thread(
                target=self._finalizar_shutdown, args=(hilo,),
                name="shutdown-finalizer", daemon=True).start()
            return
        self._finalizar_shutdown(hilo)

    def _finalizar_shutdown(self, hilo):
        if hilo is not None:
            hilo.join()

        self.control.detener()
        self.atajos.detener()
        for salida in self.salidas:
            salida.cerrar()
        self.ui.cerrar()
        with self._estado_cv:
            self._estado = EstadoApp.CLOSED
            self._estado_cv.notify_all()
        self._shutdown_completo.set()

    # ------------------------------------------------------------ trabajador

    def _crear_segmentador(self):
        cfg = self.cfg
        return self._segmentador_factory(
            cfg.sample_rate, cfg.frame_ms,
            self._vad_factory(cfg.vad_aggressiveness, cfg.sample_rate),
            cfg.silence_ms, cfg.preroll_ms,
            cfg.max_utterance_s, cfg.min_speech_ms)

    def _trabajador(self):
        try:
            self._bucle_trabajador()
        except Exception as exc:
            self._registrar_fallo_worker(exc)

    def _bucle_trabajador(self):
        generacion = None
        segmentador = None
        modo_unidad = None
        modo_entre_unidades = None
        secuencia_esperada = None

        while not self.saliendo.is_set():
            if generacion is not None and not self._sesion_procesable(generacion):
                self.streaming.reiniciar()
                self._descartar_stop(generacion)
                generacion = segmentador = modo_unidad = modo_entre_unidades = None
                secuencia_esperada = None

            item = self.mic.leer_frame(timeout=0.05)
            if item is not None:
                if not self._sesion_procesable(item.generacion):
                    continue
                if generacion != item.generacion:
                    self.streaming.reiniciar()
                    generacion = item.generacion
                    segmentador = self._crear_segmentador()
                    modo_entre_unidades = self._modo_de(generacion)
                    modo_unidad = None
                    secuencia_esperada = 1

                secuencia = getattr(item, "secuencia", 0)
                hay_gap = getattr(item, "discontinuidad_antes", False)
                if secuencia > 0:
                    hay_gap = hay_gap or secuencia != secuencia_esperada
                    secuencia_esperada = secuencia + 1
                if hay_gap:
                    modo_unidad, modo_entre_unidades = \
                        self._resolver_discontinuidad(
                            segmentador, modo_unidad, generacion)

                if not segmentador.en_voz:
                    nuevo = self._modo_de(generacion)
                    if nuevo != modo_entre_unidades:
                        self.streaming.reiniciar()
                        modo_entre_unidades = nuevo
                for evento in segmentador.procesar(item.audio):
                    if evento.tipo == "inicio_voz":
                        modo_unidad = modo_entre_unidades
                        iniciar_unidad = getattr(self.inyector, "iniciar_unidad", None)
                        if iniciar_unidad:
                            iniciar_unidad(generacion)
                        self._evento_vad(True, generacion)
                    elif evento.tipo == "frame_voz" and modo_unidad == "streaming":
                        self.streaming.aceptar_audio(evento.audio)
                        self._paso_streaming(generacion)
                    elif evento.tipo == "frase":
                        self._evento_vad(False, generacion)
                        self._cerrar_unidad(evento.audio, modo_unidad, generacion)
                        modo_unidad = None
                        modo_entre_unidades = self._modo_de(generacion)
                continue

            if generacion is not None and self._stop_listo(generacion):
                for evento in segmentador.finalizar():
                    if evento.tipo == "frase":
                        self._evento_vad(False, generacion)
                        self._cerrar_unidad(evento.audio, modo_unidad, generacion)
                self._completar_stop(generacion)
                self.streaming.reiniciar()
                generacion = segmentador = modo_unidad = modo_entre_unidades = None
                secuencia_esperada = None
            elif generacion is not None and modo_unidad == "streaming":
                self._paso_streaming(generacion)
            else:
                stop_sin_audio = self._stop_activo_sin_audio()
                if stop_sin_audio is not None:
                    self._completar_stop(stop_sin_audio)

    def _resolver_discontinuidad(self, segmentador, modo_unidad,
                                 generacion: int):
        """Traza una frontera real antes del primer frame posterior al gap."""
        for evento in segmentador.discontinuidad():
            if evento.tipo == "frase":
                self._evento_vad(False, generacion)
                self._cerrar_unidad(
                    evento.audio, modo_unidad, generacion)
        # ``finalizar`` reinicia streaming cuando había una unidad abierta.
        # Este reinicio adicional cubre gaps fuera de voz y fakes parciales.
        self.streaming.reiniciar()
        with self._salida_lock:
            if self._puede_emit(generacion):
                self.guionar.enviar_parcial("")
                cancelar = getattr(self.inyector, "cancelar_unidad", None)
                if cancelar:
                    cancelar()
        return None, self._modo_de(generacion)

    def _cerrar_unidad(self, audio, modo: Optional[str], generacion: int):
        try:
            if modo == "streaming":
                self._vaciar_streaming(generacion)
            else:
                self._atender_frase(audio, generacion)
        finally:
            finalizar = getattr(self.inyector, "finalizar_unidad", None)
            if finalizar:
                finalizar()

    def _atender_frase(self, audio, generacion: int):
        self._estado_visual_si_vigente("transcribing", generacion)
        t0 = time.time()
        try:
            crudo = self.frases.transcribir(audio)
        except Exception as exc:
            print(f"[app] la transcripción falló: {type(exc).__name__}",
                  file=sys.stderr)
            crudo = ""
        dt = time.time() - t0
        if crudo:
            print(f"[app] transcripción lista en {dt:.2f}s")
            procesado = self.proc.procesar_frase(crudo)
            self._emitir(procesado, generacion)
        self._estado_visual_si_vigente("recording", generacion)

    def _paso_streaming(self, generacion: int):
        try:
            trozo = self.streaming.procesar()
        except Exception as exc:
            print(f"[app] falló la decodificación streaming: {type(exc).__name__}",
                  file=sys.stderr)
            return
        if trozo:
            texto = self.proc.procesar_fragmento(trozo)
            if texto:
                self._escribir_en_todas(texto, generacion)
        parcial = self.streaming.hipotesis_pendiente()
        with self._salida_lock:
            if self._puede_emit(generacion):
                self.guionar.enviar_parcial(parcial)

    def _vaciar_streaming(self, generacion: int):
        cola = self.streaming.finalizar()
        with self._salida_lock:
            if not self._puede_emit(generacion):
                print(f"[app] resultado stale descartado (sesión {generacion})")
                return
            self.guionar.enviar_parcial("")
            if cola:
                texto = self.proc.procesar_fragmento(cola)
                if texto:
                    self._escribir_en_todas_bajo_lock(texto)

    def _escribir_en_todas(self, texto: str, generacion: int):
        with self._salida_lock:
            if not self._puede_emit(generacion):
                print(f"[app] resultado stale descartado (sesión {generacion})")
                return False
            self._escribir_en_todas_bajo_lock(texto)
            return True

    def _escribir_en_todas_bajo_lock(self, texto: str):
        return self._distribuir_texto(texto, texto, registrar=False)

    @staticmethod
    def _resultado_inyector(valor) -> EstadoEntrega:
        if isinstance(valor, ResultadoSink):
            return valor.estado
        return EstadoEntrega.INSERTED if valor else EstadoEntrega.FAILED

    @staticmethod
    def _intentar_sink(funcion, exito: EstadoEntrega) -> EstadoEntrega:
        try:
            resultado = funcion()
            if isinstance(resultado, ResultadoSink):
                return resultado.estado
            return exito if resultado is not False else EstadoEntrega.FAILED
        except Exception as exc:
            print(f"[app] salida {exito.value} falló: {type(exc).__name__}",
                  file=sys.stderr)
            return EstadoEntrega.FAILED

    def _distribuir_texto(self, texto_inyector: str, texto_confirmado: str,
                          *, registrar: bool = True) -> ResultadoDistribucion:
        try:
            inyector = self._resultado_inyector(
                self.inyector.escribir_texto(texto_inyector, registrar=registrar))
        except Exception as exc:
            print(f"[app] salida inserted falló: {type(exc).__name__}",
                  file=sys.stderr)
            inyector = EstadoEntrega.FAILED
        guionar = (EstadoEntrega.SKIPPED if getattr(self.guionar, "es_nulo", False)
                   else self._intentar_sink(
                       lambda: self.guionar.escribir_texto(texto_confirmado),
                       EstadoEntrega.MIRRORED))
        sesion = (EstadoEntrega.SKIPPED if getattr(self.sesion, "es_nulo", False)
                  else self._intentar_sink(
                      lambda: self.sesion.escribir_texto(texto_confirmado),
                      EstadoEntrega.PERSISTED))
        resultado = ResultadoDistribucion(inyector, guionar, sesion)
        self.ultima_entrega = resultado
        return resultado

    # ------------------------------------------------------------ emisión

    def _emitir(self, p, generacion: int):
        if p.comando == "detener":
            self.detener_grabacion(generacion_esperada=generacion)
            return
        with self._salida_lock:
            if not self._puede_emit(generacion):
                print(f"[app] resultado stale descartado (sesión {generacion})")
                self.ultima_entrega = ResultadoDistribucion.omitido()
                return self.ultima_entrega
            if p.comando == "nueva_linea":
                if not self.cfg.comando_enviar:
                    self.ultima_entrega = ResultadoDistribucion.omitido()
                    return self.ultima_entrega
                cantidad = p.carga.count("\n") if p.carga else 1
                self.inyector.nueva_linea(cantidad)
                self._necesita_espacio = False
                return
            if p.comando == "borrar_ultima":
                if not self.inyector.borrar_ultima_oracion():
                    print("[app] no hay nada para borrar")
                return
            if p.comando == "enviar":
                if not self.cfg.comando_enviar:
                    self.ultima_entrega = ResultadoDistribucion.omitido()
                    return self.ultima_entrega
                self.inyector.presionar_enter()
                self._necesita_espacio = False
                return
            if p.texto:
                salida = (" " + p.texto) if self._necesita_espacio else p.texto
                resultado = self._distribuir_texto(salida, p.texto)
                if resultado.inyector == EstadoEntrega.INSERTED:
                    self._necesita_espacio = True
                return resultado

    # ------------------------------------------------------------ estado interno

    def _puede_emit(self, generacion: int) -> bool:
        with self._estado_cv:
            return (self._sesion_activa == generacion
                    and self._estado in (EstadoApp.RECORDING, EstadoApp.STOPPING))

    def _sesion_procesable(self, generacion: int) -> bool:
        with self._estado_cv:
            return (self._sesion_activa == generacion
                    and self._estado in (EstadoApp.STARTING, EstadoApp.RECORDING,
                                         EstadoApp.STOPPING))

    def _modo_de(self, generacion: int) -> str:
        with self._estado_cv:
            return self._modo_por_sesion.get(generacion, self._modo_solicitado)

    def _stop_listo(self, generacion: int) -> bool:
        with self._estado_cv:
            return generacion in self._stops_listos

    def _stop_activo_sin_audio(self) -> Optional[int]:
        with self._estado_cv:
            if (self._estado == EstadoApp.STOPPING
                    and self._sesion_activa in self._stops_listos):
                return self._sesion_activa
            return None

    def _descartar_stop(self, generacion: int):
        with self._estado_cv:
            self._stops_listos.discard(generacion)
            self._errores_stop.discard(generacion)
            self._estado_cv.notify_all()

    def _completar_stop(self, generacion: int):
        with self._estado_cv:
            self._stops_listos.discard(generacion)
            con_error = generacion in self._errores_stop
            self._errores_stop.discard(generacion)
            if self._sesion_activa != generacion or self._estado != EstadoApp.STOPPING:
                return
            self._sesion_activa = None
            self._modo_por_sesion.pop(generacion, None)
            self._estado = EstadoApp.ERROR if con_error else EstadoApp.IDLE
            self._estado_cv.notify_all()
        self.ui.fijar_estado("error" if con_error else "idle")
        print(f"[app] ○ detenido (sesión {generacion})")

    def _evento_vad(self, hablando: bool, generacion: int):
        with self._salida_lock:
            if not self._puede_emit(generacion):
                return
            for salida in self.salidas:
                salida.evento_vad(hablando)
            self.ui.fijar_estado("recording")

    def _estado_visual_si_vigente(self, estado: str, generacion: int):
        with self._salida_lock:
            if self._puede_emit(generacion):
                self.ui.fijar_estado(estado)

    def _registrar_fallo_worker(self, exc: Exception):
        print(f"[app] fallo inesperado del worker: {type(exc).__name__}",
              file=sys.stderr)
        with self._transicion_lock:
            with self._salida_lock:
                with self._estado_cv:
                    if self._estado not in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                        self._estado = EstadoApp.ERROR
                        self._ultimo_error = f"worker: {type(exc).__name__}"
                        self._fallo_fatal_worker = True
                    self._sesion_activa = None
                    self._modo_por_sesion.clear()
                    self.grabando.clear()
                    self._estado_cv.notify_all()
            try:
                self.mic.detener(vaciar=True)
            except Exception:
                pass
        self.ui.fijar_estado("error")

    # ------------------------------------------------------------ control

    def _respuesta_estado(self) -> str:
        with self._estado_cv:
            estado = self._estado
            error = self._ultimo_error
        if estado == EstadoApp.ERROR:
            base = f"error detalle={error or 'desconocido'}"
        elif estado == EstadoApp.RECORDING:
            base = "grabando"
        elif estado == EstadoApp.STOPPING:
            base = "deteniendo"
        elif estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
            base = "cerrando" if estado == EstadoApp.SHUTTING_DOWN else "cerrado"
        else:
            base = "inactivo"
        respuesta = (base + f" modo={self._modo_solicitado} "
                     f"reescritura={self.cfg.rewrite_mode}")
        obtener_estado = getattr(self.mic, "estado_captura", None)
        if obtener_estado is None:
            return respuesta
        captura = obtener_estado()
        if captura.degradada:
            salud = "degradado"
        elif captura.frames_descartados:
            salud = "recuperado-con-perdida"
        else:
            salud = "saludable"
        return (respuesta + f" audio={salud} drops={captura.frames_descartados} "
                f"discontinuidades={captura.discontinuidades} "
                f"cola={captura.queue_depth}/{self.mic.capacidad} "
                f"backlog_ms={captura.backlog_ms:.1f}")

    def _atender_comando(self, cmd: str) -> str:
        partes = normalizar_comando(cmd)
        if not partes:
            return "ERR vacío"
        op = partes[0]
        if op == "alternar":
            return self._respuesta_control_lifecycle(self.alternar())
        if op == "iniciar":
            return self._respuesta_control_lifecycle(self.iniciar_grabacion())
        if op == "detener":
            return self._respuesta_control_lifecycle(self.detener_grabacion())
        if op == "estado":
            return self._respuesta_estado()
        if op == "modo" and len(partes) > 1 and partes[1] in ("utterance", "streaming"):
            return (f"OK modo={partes[1]}" if self.cambiar_modo(partes[1])
                    else "ERR aplicación cerrada")
        if op == "reescritura" and len(partes) > 1 \
                and partes[1] in ("none", "formal", "concise", "email"):
            with self._estado_cv:
                if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                    return "ERR aplicación cerrada"
            self.cfg.rewrite_mode = partes[1]
            self.proc.rewrite_mode = partes[1]
            return f"OK reescritura={partes[1]}"
        if op == "salir":
            threading.Thread(target=self.salir, name="shutdown", daemon=True).start()
            return "OK chau"
        return "ERR comando desconocido"

    def _respuesta_control_lifecycle(self, ok: bool) -> str:
        with self._estado_cv:
            estado = self._estado
            error = self._ultimo_error
        if not ok or estado == EstadoApp.ERROR:
            return f"ERR {error or estado.value}"
        if estado == EstadoApp.RECORDING:
            return "OK grabando"
        if estado == EstadoApp.STOPPING:
            return "OK deteniendo"
        if estado == EstadoApp.IDLE:
            return "OK detenido"
        return f"ERR {estado.value}"
