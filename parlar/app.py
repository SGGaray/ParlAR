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

import json
import re
import sys
import threading
import time
from enum import Enum
from typing import Optional

from .capturador_audio import CapturadorMic, Segmentador, crear_vad
from .config import Config
from .control import ServidorControl, normalizar_comando
from .control_gesto_dictado import ControlGestoDictado
from .coordinador_salida import crear_coordinador_salida
from .daemon_atajos import DaemonAtajos
from .entrega import EstadoEntrega
from .estado_operativo import (
    ErrorInicializacionSTT,
    EstadoComponente,
    EstadoOperativoStore,
    EstadoRuntime,
    ProblemaOperativo,
    Severidad,
    puede_dictar,
    serializar_estado,
)
from .estado_ui import EstadoInterfaz
from .indicador import crear_ui
from .inyector_salida import backend_inyector_disponible, detectar_sesion
from .motor_transcripcion import MotorWhisper, TranscriptorFrase, TranscriptorStreaming
from .procesador_texto import ProcesadorTexto
from .settings_backend import (
    ErrorDispositivosAudio,
    ErrorInicializacionAudio,
    listar_dispositivos_entrada,
    resolver_dispositivo_entrada,
)
from .tray import crear_tray, resumir_estado_tray, serializar_estado_tray


def resolver_entrada_productiva(
        preferencia: str, *, listar_entradas=None, resolver_entrada=None):
    """Resuelve una preferencia persistida al índice PortAudio de este inicio."""
    listar = listar_entradas or listar_dispositivos_entrada
    resolver = resolver_entrada or resolver_dispositivo_entrada
    try:
        inventario = listar()
    except ErrorDispositivosAudio as exc:
        raise ErrorInicializacionAudio(
            f"no se pudo obtener el inventario de entradas: {exc}"
        ) from exc

    resolucion = resolver(preferencia, inventario)
    if resolucion.indice is None:
        motivo = resolucion.motivo or "sin_motivo"
        if "inventario_vacio" in motivo:
            detalle = "no hay dispositivos de entrada disponibles"
        elif "default_ambiguo" in motivo:
            detalle = "el dispositivo de entrada predeterminado es ambiguo"
        elif "default_no_disponible" in motivo:
            detalle = "no hay un dispositivo de entrada predeterminado"
        else:
            detalle = "la entrada configurada no pudo resolverse"
        raise ErrorInicializacionAudio(f"{detalle} (motivo: {motivo})")
    return resolucion


class EstadoApp(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"


def foco_settings_seguro(estado: EstadoApp) -> bool:
    """Evita convertir Settings en destino mientras hay dictado en curso."""
    return estado not in {
        EstadoApp.STARTING,
        EstadoApp.RECORDING,
        EstadoApp.STOPPING,
    }


class App:
    def __init__(self, cfg: Config, *, motor=None, frases=None, streaming=None,
                 proc=None, inyector=None, guionar=None, sesion=None, mic=None,
                 ui=None, control=None, atajos=None, guardia_instancia=None,
                 vad_factory=crear_vad, segmentador_factory=Segmentador,
                 listar_entradas=None, resolver_entrada=None,
                 estado_operativo=None, tray=None):
        self.estado_operativo = estado_operativo or EstadoOperativoStore(
            gesto_requerido=detectar_sesion() != "wayland")
        try:
            cfg.validate()
        except Exception as exc:
            self._fallo_operativo(
                "configuracion_invalida", "runtime",
                "La configuración de ParlAR no es válida.", exc)
            raise
        self.cfg = cfg
        self.grabando = threading.Event()
        self.saliendo = threading.Event()

        self._transicion_lock = threading.Lock()
        self._estado_cv = threading.Condition()
        self._salida_lock = threading.RLock()
        self._estado = EstadoApp.IDLE
        self._pausado = False
        self._pausa_pendiente = False
        self._ultimo_error = ""
        self._fallo_fatal_worker = False
        self._generacion = 0
        self._sesion_activa: Optional[int] = None
        self._modo_solicitado = cfg.mode
        self._modo_por_sesion: dict[int, str] = {}
        self._stops_listos: set[int] = set()
        self._errores_stop: set[int] = set()
        self._shutdown_completo = threading.Event()
        self._shutdown_errores: list[str] = []
        self._trabajador_hilo: Optional[threading.Thread] = None
        self._stt_health = "healthy"
        self._vad_health = "healthy"
        self._stt_failures = 0
        self._vad_failures = 0
        self._last_stt_error_type: Optional[str] = None
        self._last_vad_error_type: Optional[str] = None
        self._vad_factory = vad_factory
        self._segmentador_factory = segmentador_factory

        print("=" * 60)
        print("ParlAR: dictado local, sin telemetría.")
        print("=" * 60)

        estado_interfaz = EstadoInterfaz()
        self.resolucion_entrada = None
        mic_productivo = mic
        if mic_productivo is None:
            try:
                self.resolucion_entrada = resolver_entrada_productiva(
                    cfg.audio_input_device,
                    listar_entradas=listar_entradas,
                    resolver_entrada=resolver_entrada,
                )
            except Exception as exc:
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "audio_no_disponible", "audio", Severidad.BLOCKING,
                        "No hay un micrófono disponible para dictar.",
                        self._detalle_error(exc)),
                )
                raise
            if self.resolucion_entrada.usando_fallback:
                dispositivo = self.resolucion_entrada.dispositivo
                nombre = (
                    dispositivo.nombre
                    if dispositivo is not None
                    else "desconocido"
                )
                print(
                    "[audio] la entrada configurada no está disponible; "
                    f"se usará la predeterminada {nombre!r} "
                    f"(motivo: {self.resolucion_entrada.motivo})",
                    file=sys.stderr,
                )
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.FALLBACK,
                    dispositivo_audio=nombre,
                    problema=ProblemaOperativo(
                        "audio_fallback", "audio", Severidad.WARNING,
                        "El micrófono guardado no está disponible; se usa el predeterminado.",
                        self.resolucion_entrada.motivo),
                )
            else:
                dispositivo = self.resolucion_entrada.dispositivo
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.READY,
                    dispositivo_audio=(
                        dispositivo.nombre if dispositivo is not None else None),
                )
            mic_productivo = CapturadorMic(
                cfg.sample_rate,
                cfg.frame_samples,
                input_device=self.resolucion_entrada.indice,
                publicar_nivel_visual=estado_interfaz.fijar_nivel_visual,
                publicar_descriptor_visual=(
                    estado_interfaz.fijar_descriptor_visual),
            )
        else:
            self.estado_operativo.fijar_componente(
                "audio", EstadoComponente.READY)

        if motor is None and (frases is None or streaming is None):
            self.estado_operativo.fijar_componente(
                "stt", EstadoComponente.LOADING)
            try:
                motor = MotorWhisper(
                    cfg.model_size,
                    cfg.device,
                    cfg.compute_type,
                    cfg.language,
                    cfg.beam_size,
                    initial_prompt=cfg.construir_contexto_stt(),
                )
            except Exception as exc:
                mensaje = (
                    "No se pudo iniciar la aceleración CUDA solicitada."
                    if cfg.device == "cuda"
                    else "No se pudo cargar el modelo de reconocimiento.")
                self.estado_operativo.fijar_componente(
                    "stt", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "stt_carga_fallida", "stt", Severidad.BLOCKING,
                        mensaje, self._detalle_error(exc)),
                )
                raise ErrorInicializacionSTT(mensaje) from exc
        self.estado_operativo.fijar_componente(
            "stt", EstadoComponente.READY)
        self.motor = motor
        self.frases = frases or TranscriptorFrase(self.motor)
        self.streaming = streaming or TranscriptorStreaming(
            self.motor, cfg.sample_rate, cfg.stream_interval_s, cfg.stream_trim_s)
        self.proc = proc or ProcesadorTexto(
            cfg.remove_fillers, cfg.voice_commands, cfg.rewrite_mode,
            cfg.ollama_model, cfg.ollama_url, cfg.comando_enviar)
        self.salida = crear_coordinador_salida(
            cfg,
            inyector=inyector,
            guionar=guionar,
            sesion=sesion,
        )
        backend_salida = getattr(self.salida.inyector, "backend", None)
        if backend_salida and not backend_inyector_disponible(backend_salida):
            self.estado_operativo.fijar_componente(
                "output", EstadoComponente.ERROR,
                backend_salida=backend_salida,
                problema=ProblemaOperativo(
                    "inyector_no_disponible", "output", Severidad.BLOCKING,
                    "No hay una salida disponible para entregar el dictado.",
                    f"backend={backend_salida}"),
            )
        elif backend_salida == "clipboard":
            self.estado_operativo.fijar_componente(
                "output", EstadoComponente.DEGRADED,
                backend_salida=backend_salida,
                problema=ProblemaOperativo(
                    "inyector_clipboard", "output", Severidad.WARNING,
                    "La escritura directa no está disponible; se usará el portapapeles.",
                ),
            )
        else:
            self.estado_operativo.fijar_componente(
                "output", EstadoComponente.READY,
                backend_salida=backend_salida or "personalizado")
        if cfg.guionar:
            print(f"[guionar] integración activa (socket: {self.guionar.ruta})")
        self.mic = mic_productivo
        self.ui = ui or crear_ui(
            cfg.overlay,
            estado_ui=estado_interfaz,
            overlay_position=cfg.overlay_position,
        )
        self.control = control or ServidorControl(
            self._atender_comando, guardia_instancia=guardia_instancia)
        self.gesto_dictado = ControlGestoDictado(
            self.iniciar_grabacion,
            self.detener_grabacion,
            al_presionado=getattr(
                self.ui, "fijar_presionado", None
            ),
            al_continuo=getattr(
                self.ui, "fijar_continuo", None
            ),
        )
        self.atajos = atajos or DaemonAtajos(
            cfg.hotkey_toggle,
            cfg.hotkey_quit,
            al_presionar=self.gesto_dictado.presionar,
            al_soltar=self.gesto_dictado.soltar,
            al_salir=self.salir,
            al_cancelar=self.cancelar_grabacion,
        )
        self.tray = tray or crear_tray(al_fallo=self._reportar_fallo_tray)

    @staticmethod
    def _detalle_error(exc) -> str:
        return f"{type(exc).__name__}: {exc}"

    def _fallo_operativo(self, codigo, componente, mensaje, exc):
        self.estado_operativo.reportar_problema(ProblemaOperativo(
            codigo, componente, Severidad.BLOCKING, mensaje,
            self._detalle_error(exc)))

    def _reportar_fallo_tray(self, detalle: str) -> None:
        print(f"[tray] no disponible ({detalle})", file=sys.stderr)
        self.estado_operativo.reportar_problema(ProblemaOperativo(
            "tray_no_disponible", "tray", Severidad.WARNING,
            "El acceso desde el área de estado no está disponible; "
            "ParlAR sigue operativo.", detalle))

    # ------------------------------------------------------------ ciclo de vida

    @property
    def estado(self) -> EstadoApp:
        with self._estado_cv:
            return self._estado

    @property
    def inyector(self):
        return self.salida.inyector

    @property
    def guionar(self):
        return self.salida.guionar

    @property
    def sesion(self):
        return self.salida.sesion

    @property
    def ultima_entrega(self):
        return self.salida.ultima_entrega

    @property
    def _necesita_espacio(self):
        return self.salida.necesita_espacio

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
            try:
                hilo.start()
            except BaseException:
                self._trabajador_hilo = None
                raise

    def ejecutar(self):
        try:
            self._iniciar_trabajador()
            try:
                self.control.iniciar()
            except Exception as exc:
                self.estado_operativo.fijar_componente(
                    "control", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "control_no_disponible", "control", Severidad.BLOCKING,
                        "No se pudo iniciar el control local de ParlAR.",
                        self._detalle_error(exc)),
                )
                self.estado_operativo.actualizar_runtime(EstadoRuntime.ERROR)
                raise
            self.estado_operativo.fijar_componente(
                "control", EstadoComponente.READY)
            try:
                hotkey_listo = bool(self.atajos.iniciar())
            except Exception as exc:
                self.estado_operativo.fijar_componente(
                    "hotkey", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "hotkey_inicio_fallido", "hotkey", Severidad.BLOCKING,
                        "No se pudo iniciar el atajo global.",
                        self._detalle_error(exc)),
                )
                self.estado_operativo.actualizar_runtime(EstadoRuntime.ERROR)
                raise
            if hotkey_listo:
                self.estado_operativo.fijar_componente(
                    "hotkey", EstadoComponente.READY)
            else:
                gesto_requerido = self.estado_operativo.snapshot().gesto_requerido
                self.estado_operativo.fijar_componente(
                    "hotkey", EstadoComponente.ERROR if gesto_requerido
                    else EstadoComponente.UNAVAILABLE,
                    problema=ProblemaOperativo(
                        "hotkey_no_disponible", "hotkey",
                        Severidad.BLOCKING if gesto_requerido
                        else Severidad.WARNING,
                        "El atajo global no está disponible; usá parlarctl.",
                    ),
                )
            self._comprobar_guionar()
            self.estado_operativo.actualizar_runtime(EstadoRuntime.READY)
            try:
                self.tray.iniciar()
            except Exception as exc:
                self._reportar_fallo_tray(self._detalle_error(exc))
            prefijo = (
                "[app] listo."
                if puede_dictar(self.estado_operativo.snapshot())
                else "[app] iniciado, pero requiere atención."
            )
            print(f"{prefijo} Modo: {self.cfg.mode}. {self._pista_controles()}")
            self.ui.ejecutar()
        except KeyboardInterrupt:
            pass
        finally:
            self.salir()

    def _comprobar_guionar(self):
        if not self.cfg.guionar:
            return
        comprobar = getattr(self.guionar, "comprobar_disponibilidad", None)
        if comprobar is not None and not comprobar():
            self.estado_operativo.reportar_problema(ProblemaOperativo(
                "guionar_no_disponible", "guionar", Severidad.WARNING,
                "GuionAR no está disponible; el dictado seguirá en la salida principal.",
            ))

    def _pista_controles(self) -> str:
        if self.atajos._listener:
            controles = (
                f"Mantené {self.cfg.hotkey_toggle} para dictar; "
                "doble toque para modo continuo; Esc cancela"
            )
        else:
            controles = (
                "Usá `parlarctl iniciar`, `detener`, `cancelar` o `alternar`"
            )
        return controles + "."

    def _ui_preparar_generacion(self, generacion: int):
        publicar = getattr(self.ui, "preparar_generacion", None)
        return publicar(generacion) if publicar else True

    def _ui_iniciar_captura(self, generacion: int):
        publicar = getattr(self.ui, "iniciar_captura", None)
        if publicar:
            return publicar(generacion)
        self.ui.fijar_estado("recording")
        return True

    def _ui_cerrar_captura(self, generacion: int):
        publicar = getattr(self.ui, "cerrar_captura", None)
        if publicar:
            return publicar(generacion, trabajo_pendiente=True)
        self.ui.fijar_estado("transcribing")
        return True

    def _ui_iniciar_trabajo(self, generacion: int):
        publicar = getattr(self.ui, "iniciar_trabajo", None)
        if publicar:
            return publicar(generacion)
        if self.estado != EstadoApp.RECORDING:
            self.ui.fijar_estado("transcribing")
        return True

    def _ui_finalizar_trabajo(self, generacion: int):
        publicar = getattr(self.ui, "finalizar_trabajo", None)
        if publicar:
            return publicar(generacion)
        if self.estado == EstadoApp.RECORDING:
            self.ui.fijar_estado("recording")
        return True

    def _ui_invalidar_generacion(self, generacion: int):
        publicar = getattr(self.ui, "invalidar_generacion", None)
        if publicar:
            return publicar(generacion)
        self.ui.fijar_estado("idle")
        return True

    def _ui_completar_generacion(
            self, generacion: int, *, con_error: bool = False):
        publicar = getattr(self.ui, "completar_generacion", None)
        if publicar:
            return publicar(generacion, con_error=con_error)
        self.ui.fijar_estado("error" if con_error else "idle")
        return True

    def alternar(self):
        estado = self.estado
        if estado in (EstadoApp.RECORDING, EstadoApp.STARTING):
            return self.detener_grabacion()
        return self.iniciar_grabacion()

    @property
    def pausado(self) -> bool:
        with self._estado_cv:
            return self._pausado

    def pausar(self) -> bool:
        """Impide START nuevos y resuelve con suavidad la sesión vigente."""
        with self._estado_cv:
            if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                self._ultimo_error = "la aplicación se está cerrando"
                return False
            if self._pausado:
                return True
            self._pausado = True
            estado = self._estado
            self._pausa_pendiente = estado in {
                EstadoApp.STARTING, EstadoApp.RECORDING, EstadoApp.STOPPING}
            self._estado_cv.notify_all()
        self.estado_operativo.actualizar_pausa(True)

        # Quita continuo, doble toque y releases pendientes antes de cerrar la
        # captura. No toca el listener global ni la configuración del atajo.
        self.gesto_dictado.reiniciar()
        resultado = True
        if estado == EstadoApp.STARTING:
            resultado = self.cancelar_grabacion()
        elif estado == EstadoApp.RECORDING:
            resultado = self.detener_grabacion()

        with self._estado_cv:
            if self._estado in {EstadoApp.IDLE, EstadoApp.ERROR}:
                self._pausa_pendiente = False
                self._estado_cv.notify_all()
        return resultado

    def reanudar(self) -> bool:
        """Vuelve a admitir dictados sin iniciar uno automáticamente."""
        with self._estado_cv:
            if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                self._ultimo_error = "la aplicación se está cerrando"
                return False
            self._pausado = False
            self._pausa_pendiente = False
            self._ultimo_error = ""
            self._estado_cv.notify_all()
        self.estado_operativo.actualizar_pausa(False)
        self.gesto_dictado.reiniciar()
        return True

    def iniciar_grabacion(self) -> bool:
        self._iniciar_trabajador()
        with self._transicion_lock:
            with self._estado_cv:
                if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                    self._ultimo_error = "la aplicación se está cerrando"
                    return False
                if self._pausado:
                    self._ultimo_error = "ParlAR está pausado"
                    return False
                if self._fallo_fatal_worker:
                    self._ultimo_error = "el worker no está operativo"
                    return False
                if self._estado in (EstadoApp.STARTING, EstadoApp.RECORDING):
                    return True
                anterior = self._sesion_activa
                self._generacion += 1
                generacion = self._generacion
                # START reemplaza cualquier generación anterior. Sus señales
                # de stop ya no pueden ser consumidas legítimamente.
                self._stops_listos.clear()
                self._errores_stop.clear()
                self._estado = EstadoApp.STARTING
                self._ultimo_error = ""
                self._estado_cv.notify_all()

            with self._salida_lock:
                with self._estado_cv:
                    if self._estado != EstadoApp.STARTING:
                        return False
                    self._sesion_activa = generacion
                    self._modo_por_sesion = {generacion: self._modo_solicitado}
                    self._estado_cv.notify_all()
                self._ui_preparar_generacion(generacion)
                if anterior is not None:
                    self.mic.descartar_pendientes(anterior)
                self.salida.iniciar_generacion(
                    limpiar_parcial=anterior is not None)

            try:
                abierta = self.mic.iniciar(generacion)
                if abierta is False:
                    raise RuntimeError("el micrófono ya tiene una apertura activa")
            except Exception as exc:
                with self._salida_lock:
                    with self._estado_cv:
                        vigente = (
                            self._sesion_activa == generacion
                            and self._estado == EstadoApp.STARTING
                        )
                        if self._sesion_activa == generacion:
                            self._sesion_activa = None
                        self._modo_por_sesion.pop(generacion, None)
                        self._stops_listos.discard(generacion)
                        self._errores_stop.discard(generacion)
                        if vigente:
                            self._estado = EstadoApp.ERROR
                            self._ultimo_error = f"micrófono: {exc}"
                        self._estado_cv.notify_all()
                try:
                    self.mic.detener(vaciar=True)
                except Exception as cleanup_exc:
                    print("[app] rollback de micrófono falló: "
                          f"{type(cleanup_exc).__name__}", file=sys.stderr)
                try:
                    self.mic.descartar_pendientes(generacion)
                except Exception as cleanup_exc:
                    print("[app] descarte de audio falló: "
                          f"{type(cleanup_exc).__name__}", file=sys.stderr)
                self.grabando.clear()
                if not vigente:
                    return False
                self._estado_visual_terminal_si_vigente(
                    "error", EstadoApp.ERROR, generacion)
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "audio_apertura_fallida", "audio", Severidad.BLOCKING,
                        "No se pudo abrir el micrófono seleccionado.",
                        self._detalle_error(exc)),
                )
                print(f"[app] no se pudo abrir el micrófono: {exc}", file=sys.stderr)
                return False

            with self._estado_cv:
                vigente = (
                    self._sesion_activa == generacion
                    and self._estado == EstadoApp.STARTING
                )
                if vigente:
                    self._estado = EstadoApp.RECORDING
                    self._estado_cv.notify_all()

            if not vigente:
                try:
                    self.mic.detener(vaciar=True)
                except Exception as cleanup_exc:
                    print(
                        "[app] cierre de micrófono cancelado falló: "
                        f"{type(cleanup_exc).__name__}",
                        file=sys.stderr,
                    )
                try:
                    self.mic.descartar_pendientes(generacion)
                except Exception as cleanup_exc:
                    print(
                        "[app] descarte cancelado falló: "
                        f"{type(cleanup_exc).__name__}",
                        file=sys.stderr,
                    )
                self.grabando.clear()
                return False

            self.grabando.set()
            if (self.resolucion_entrada is not None
                    and self.resolucion_entrada.usando_fallback):
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.FALLBACK,
                    problema=ProblemaOperativo(
                        "audio_fallback", "audio", Severidad.WARNING,
                        "El micrófono guardado no está disponible; se usa el predeterminado.",
                        self.resolucion_entrada.motivo),
                )
            else:
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.READY)
            self._ui_iniciar_captura(generacion)
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

            if generacion is not None:
                # La barrera visual se instala antes de habilitar al worker a
                # completar STOP: así listening->processing nunca pasa por
                # hidden y una finalización rápida no puede resucitar estado.
                self._ui_cerrar_captura(generacion)

            with self._estado_cv:
                cancelada = (
                    self._estado != EstadoApp.STOPPING
                    or self._sesion_activa != generacion
                )
                if generacion is not None and not cancelada:
                    self._stops_listos.add(generacion)
                    if error is not None:
                        self._errores_stop.add(generacion)
                        self._ultimo_error = f"micrófono: {error}"
                self._estado_cv.notify_all()
            if cancelada:
                return error is None
            if error is not None:
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "audio_cierre_fallido", "audio", Severidad.BLOCKING,
                        "No se pudo cerrar correctamente el micrófono.",
                        self._detalle_error(error)),
                )
            print(f"[app] ◌ deteniendo (sesión {generacion})")
            return error is None

    def cancelar_grabacion(self) -> bool:
        """Invalida la sesión sin finalizar ni emitir audio pendiente."""
        generacion = None
        revision = None

        # La barrera de salida es lo primero: cualquier escritura que ya
        # comenzó termina antes de esta sección; ninguna nueva puede empezar
        # después de invalidar la generación.
        with self._salida_lock:
            with self._estado_cv:
                if self._estado in (
                    EstadoApp.SHUTTING_DOWN,
                    EstadoApp.CLOSED,
                ):
                    return True
                revision = self._generacion

                if self._estado in (
                    EstadoApp.IDLE,
                    EstadoApp.ERROR,
                ):
                    ya_detenido = True
                    self._pausa_pendiente = False
                else:
                    ya_detenido = False
                    generacion = self._sesion_activa
                    self._sesion_activa = None

                    if generacion is not None:
                        self._modo_por_sesion.pop(
                            generacion,
                            None,
                        )
                        self._stops_listos.discard(
                            generacion
                        )
                        self._errores_stop.discard(
                            generacion
                        )

                    self._estado = EstadoApp.IDLE
                    self._pausa_pendiente = False
                    self._ultimo_error = ""
                    self.grabando.clear()
                    self._estado_cv.notify_all()

                    if generacion is not None:
                        self._ui_invalidar_generacion(generacion)

            if not ya_detenido:
                self.salida.cancelar_generacion()

        # También elimina continuo/doble-tap/timers pendientes.
        gesto = getattr(
            self,
            "gesto_dictado",
            None,
        )
        if gesto is not None:
            gesto.reiniciar()

        if ya_detenido:
            return True

        error = None

        # START/STOP físicos siguen serializados. Si un START estaba dentro
        # de mic.iniciar(), esperamos su retorno después de haber invalidado
        # ya la generación. Bajo esta barrera comprobamos ownership otra vez:
        # un START posterior puede haber abierto una captura nueva mientras
        # este CANCEL esperaba el lock, y el cierre viejo no debe tocarla.
        with self._transicion_lock:
            with self._estado_cv:
                puede_cerrar_mic = (
                    self._sesion_activa is None
                    or self._sesion_activa == generacion
                )

            if puede_cerrar_mic:
                try:
                    self.mic.detener(vaciar=True)
                except Exception as exc:
                    error = exc
                    print(
                        "[app] falló el cierre por cancelación: "
                        f"{exc}",
                        file=sys.stderr,
                    )

            if generacion is not None:
                try:
                    self.mic.descartar_pendientes(
                        generacion
                    )
                except Exception as exc:
                    if error is None:
                        error = exc
                    print(
                        "[app] falló el descarte por cancelación: "
                        f"{exc}",
                        file=sys.stderr,
                    )

        if error is not None:
            with self._estado_cv:
                error_vigente = (
                    self._estado == EstadoApp.IDLE
                    and self._sesion_activa is None
                )
                if error_vigente:
                    self._estado = EstadoApp.ERROR
                    self._ultimo_error = (
                        f"micrófono: {error}"
                    )
                    self._estado_cv.notify_all()

            if error_vigente:
                self._estado_visual_terminal_si_vigente(
                    "error", EstadoApp.ERROR, revision)
                self.estado_operativo.fijar_componente(
                    "audio", EstadoComponente.ERROR,
                    problema=ProblemaOperativo(
                        "audio_cierre_fallido", "audio", Severidad.BLOCKING,
                        "No se pudo cerrar correctamente el micrófono.",
                        self._detalle_error(error)),
                )
            return False

        self._estado_visual_terminal_si_vigente(
            "idle", EstadoApp.IDLE, revision)

        if generacion is not None:
            print(
                f"[app] × cancelado "
                f"(sesión {generacion})"
            )

        return True

    def cambiar_modo(self, modo: str) -> bool:
        if modo not in Config.MODOS:
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

    def salir(self, *, esperar: bool = True):
        gesto = getattr(self, "gesto_dictado", None)
        if gesto is not None:
            gesto.cerrar()

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
                self.estado_operativo.actualizar_runtime(
                    EstadoRuntime.STOPPING)

            if propietario:
                with self._salida_lock:
                    with self._estado_cv:
                        generacion = self._sesion_activa
                        self._sesion_activa = None
                        self._modo_por_sesion.clear()
                        self._stops_listos.clear()
                        self._errores_stop.clear()
                        self._estado_cv.notify_all()
                    if generacion is not None:
                        self._ui_invalidar_generacion(generacion)
                try:
                    self.mic.detener(vaciar=True)
                except BaseException as exc:
                    self._registrar_error_shutdown("mic", exc)

        if not propietario:
            if esperar:
                espera.wait()
            return

        hilo = self._trabajador_hilo
        if hilo is threading.current_thread() or not esperar:
            # Si una acción del propio pipeline pide salir, otro hilo espera
            # su retorno antes de cerrar los recursos que todavía posee. El
            # control usa la misma reserva sin bloquear su handler.
            threading.Thread(
                target=self._finalizar_shutdown, args=(hilo,),
                name="shutdown-finalizer", daemon=True).start()
            return
        self._finalizar_shutdown(hilo)

    def _finalizar_shutdown(self, hilo):
        try:
            if hilo is not None:
                try:
                    hilo.join()
                except BaseException as exc:
                    self._registrar_error_shutdown("worker", exc)

            recursos = (
                ("tray", self.tray.detener),
                ("control", self.control.detener),
                ("hotkeys", self.atajos.detener),
            )
            for nombre, cerrar in recursos:
                try:
                    cerrar()
                except BaseException as exc:
                    self._registrar_error_shutdown(nombre, exc)
            for nombre, exc in self.salida.cerrar():
                self._registrar_error_shutdown(nombre, exc)
            try:
                self.ui.cerrar()
            except BaseException as exc:
                self._registrar_error_shutdown("ui", exc)
        finally:
            with self._estado_cv:
                if self._shutdown_errores:
                    self._ultimo_error = "shutdown: " + ",".join(
                        self._shutdown_errores)
                self._estado = EstadoApp.CLOSED
                self._estado_cv.notify_all()
            self.estado_operativo.actualizar_runtime(EstadoRuntime.STOPPED)
            self._shutdown_completo.set()

    def _registrar_error_shutdown(self, etapa: str, exc: BaseException):
        detalle = f"{etapa}:{type(exc).__name__}"
        with self._estado_cv:
            self._shutdown_errores.append(detalle)
        print(f"[app] cleanup falló etapa={etapa} "
              f"tipo={type(exc).__name__}", file=sys.stderr)

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
        finally:
            self._cancelar_contexto_texto()

    def _iniciar_contexto_texto(self):
        iniciar = getattr(getattr(self, "proc", None), "iniciar_unidad", None)
        if iniciar:
            iniciar()

    def _finalizar_contexto_texto(self):
        finalizar = getattr(
            getattr(self, "proc", None), "finalizar_unidad", None)
        if finalizar:
            finalizar()

    def _cancelar_contexto_texto(self):
        cancelar = getattr(
            getattr(self, "proc", None), "cancelar_unidad", None)
        if cancelar:
            cancelar()

    def _reiniciar_streaming(self):
        self.streaming.reiniciar()
        self._cancelar_contexto_texto()

    def _bucle_trabajador(self):
        generacion = None
        segmentador = None
        modo_unidad = None
        modo_entre_unidades = None
        secuencia_esperada = None
        vad_fallos_vistos = 0

        while not self.saliendo.is_set():
            if generacion is not None and not self._sesion_procesable(generacion):
                self._reiniciar_streaming()
                self._descartar_stop(generacion)
                generacion = segmentador = modo_unidad = modo_entre_unidades = None
                secuencia_esperada = None
                vad_fallos_vistos = 0

            item = self.mic.leer_frame(timeout=0.05)
            if item is not None:
                if not self._esperar_sesion_lista(item.generacion):
                    continue
                if generacion != item.generacion:
                    self._reiniciar_streaming()
                    generacion = item.generacion
                    segmentador = self._crear_segmentador()
                    modo_entre_unidades = self._modo_de(generacion)
                    modo_unidad = None
                    secuencia_esperada = 1
                    vad_fallos_vistos = 0

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
                        self._reiniciar_streaming()
                        modo_entre_unidades = nuevo
                for evento in segmentador.procesar(item.audio):
                    if evento.tipo == "inicio_voz":
                        modo_unidad = modo_entre_unidades
                        self._iniciar_contexto_texto()
                        self.salida.iniciar_unidad(generacion)
                        self._evento_vad(True, generacion)
                    elif evento.tipo == "frame_voz" and modo_unidad == "streaming":
                        self.streaming.aceptar_audio(evento.audio)
                        self._paso_streaming(generacion)
                    elif evento.tipo == "frase":
                        self._evento_vad(False, generacion)
                        self._cerrar_unidad(evento.audio, modo_unidad, generacion)
                        modo_unidad = None
                        modo_entre_unidades = self._modo_de(generacion)
                vad_fallos_vistos = self._actualizar_salud_vad(
                    segmentador, vad_fallos_vistos, generacion)
                continue

            if generacion is not None and self._stop_listo(generacion):
                for evento in segmentador.finalizar():
                    if evento.tipo == "frase":
                        self._evento_vad(False, generacion)
                        self._cerrar_unidad(evento.audio, modo_unidad, generacion)
                self._completar_stop(generacion)
                self._reiniciar_streaming()
                generacion = segmentador = modo_unidad = modo_entre_unidades = None
                secuencia_esperada = None
                vad_fallos_vistos = 0
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
        self._reiniciar_streaming()
        with self._salida_lock:
            if self._puede_emit(generacion):
                self.salida.enviar_parcial("")
                self.salida.cancelar_unidad()
        return None, self._modo_de(generacion)

    def _cerrar_unidad(self, audio, modo: Optional[str], generacion: int):
        self._ui_iniciar_trabajo(generacion)
        try:
            if modo == "streaming":
                self._vaciar_streaming(generacion)
            else:
                self._atender_frase(audio, generacion)
        finally:
            try:
                self._finalizar_contexto_texto()
            finally:
                try:
                    self.salida.finalizar_unidad()
                finally:
                    self._ui_finalizar_trabajo(generacion)

    def _atender_frase(self, audio, generacion: int):
        t0 = time.time()
        try:
            crudo = self.frases.transcribir(audio)
        except Exception as exc:
            self._registrar_fallo_etapa("stt", exc, generacion)
            crudo = ""
        else:
            self._registrar_recuperacion_etapa("stt")
        dt = time.time() - t0
        if crudo:
            print(f"[app] transcripción lista en {dt:.2f}s")
            procesado = self.proc.procesar_frase(crudo)
            self._emitir(procesado, generacion)

    def _paso_streaming(self, generacion: int):
        antes = self._snapshot_streaming()
        try:
            trozo = self.streaming.procesar()
        except Exception as exc:
            if not self._actualizar_salud_streaming(
                    antes, generacion, permitir_recuperacion=False):
                self._registrar_fallo_etapa("stt", exc, generacion)
            return
        self._actualizar_salud_streaming(antes, generacion)
        if trozo:
            texto = self.proc.procesar_fragmento(trozo)
            if texto:
                self._escribir_en_todas(texto, generacion)
        parcial = self.streaming.hipotesis_pendiente()
        with self._salida_lock:
            if self._puede_emit(generacion):
                self.salida.enviar_parcial(parcial)

    def _vaciar_streaming(self, generacion: int):
        antes = self._snapshot_streaming()
        try:
            cola = self.streaming.finalizar()
        except Exception as exc:
            if not self._actualizar_salud_streaming(
                    antes, generacion, permitir_recuperacion=False):
                self._registrar_fallo_etapa("stt", exc, generacion)
            cola = ""
        else:
            self._actualizar_salud_streaming(antes, generacion)
        with self._salida_lock:
            if not self._puede_emit(generacion):
                print(f"[app] resultado stale descartado (sesión {generacion})")
                return
            self.salida.enviar_parcial("")
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
        resultado = self.salida.entregar_fragmento(texto)
        self._registrar_resultado_salida(resultado)
        return resultado

    def _registrar_resultado_salida(self, resultado):
        estado_operativo = getattr(self, "estado_operativo", None)
        if estado_operativo is None:
            return
        estado_inyector = getattr(resultado, "inyector", None)
        if estado_inyector == EstadoEntrega.FAILED:
            estado_operativo.fijar_componente(
                "output", EstadoComponente.ERROR,
                problema=ProblemaOperativo(
                    "inyector_fallido", "output", Severidad.BLOCKING,
                    "No se pudo entregar el texto dictado.",
                ),
            )
        elif estado_inyector == EstadoEntrega.COPIED:
            estado_operativo.fijar_componente(
                "output", EstadoComponente.DEGRADED,
                problema=ProblemaOperativo(
                    "inyector_clipboard", "output", Severidad.WARNING,
                    "El texto quedó en el portapapeles para pegarlo manualmente.",
                ),
            )
        elif estado_inyector == EstadoEntrega.INSERTED:
            estado_operativo.fijar_componente(
                "output", EstadoComponente.READY)

        estado_guionar = getattr(resultado, "guionar", None)
        guionar_activo = getattr(getattr(self, "cfg", None), "guionar", False)
        if guionar_activo and estado_guionar == EstadoEntrega.FAILED:
            estado_operativo.reportar_problema(ProblemaOperativo(
                "guionar_no_disponible", "guionar", Severidad.WARNING,
                "GuionAR no está disponible; el dictado seguirá en la salida principal.",
            ))
        elif estado_guionar == EstadoEntrega.MIRRORED:
            estado_operativo.resolver_problema("guionar_no_disponible")

    # ------------------------------------------------------------ emisión

    def _emitir(self, p, generacion: int):
        if p.comando == "detener":
            self.detener_grabacion(generacion_esperada=generacion)
            return
        with self._salida_lock:
            if not self._puede_emit(generacion):
                print(f"[app] resultado stale descartado (sesión {generacion})")
                return self.salida.omitir()
            if p.comando == "nueva_linea":
                if not self.cfg.comando_enviar:
                    return self.salida.omitir()
                cantidad = p.carga.count("\n") if p.carga else 1
                self.salida.nueva_linea(cantidad)
                return
            if p.comando == "borrar_ultima":
                if not self.salida.borrar_ultima_oracion():
                    print("[app] no hay nada para borrar")
                return
            if p.comando == "enviar":
                if not self.cfg.comando_enviar:
                    return self.salida.omitir()
                self.salida.presionar_enter()
                return
            if p.texto:
                resultado = self.salida.entregar_texto(p.texto)
                self._registrar_resultado_salida(resultado)
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

    def _esperar_sesion_lista(self, generacion: int) -> bool:
        """No deja que STARTING produzca efectos irreversibles.

        El callback puede seguir encolando. El worker conserva como máximo el
        frame ya leído y duerme sobre la condición hasta que start se resuelva.
        """
        with self._estado_cv:
            self._estado_cv.wait_for(
                lambda: (self.saliendo.is_set()
                         or self._sesion_activa != generacion
                         or self._estado != EstadoApp.STARTING)
            )
            return (self._sesion_activa == generacion
                    and self._estado in (EstadoApp.RECORDING,
                                         EstadoApp.STOPPING))

    def _registrar_fallo_etapa(self, etapa: str, error,
                               generacion: int, cantidad: int = 1):
        if not hasattr(self, "_estado_cv"):
            return
        tipo_error = error if isinstance(error, str) else type(error).__name__
        with self._estado_cv:
            if etapa == "stt":
                anterior = self._stt_health
                self._stt_failures += cantidad
                self._last_stt_error_type = tipo_error
                self._stt_health = "degraded"
                contador = self._stt_failures
            else:
                anterior = self._vad_health
                self._vad_failures += cantidad
                self._last_vad_error_type = tipo_error
                self._vad_health = "degraded"
                contador = self._vad_failures
        if anterior != "degraded":
            print(f"[app] etapa={etapa} degradada "
                  f"tipo={tipo_error} contador={contador} "
                  f"generacion={generacion}", file=sys.stderr)
            self.estado_operativo.reportar_problema(ProblemaOperativo(
                f"{etapa}_degradado", etapa, Severidad.WARNING,
                "El reconocimiento tuvo un fallo recuperable."
                if etapa == "stt"
                else "La detección de voz tuvo un fallo recuperable.",
                str(tipo_error),
            ))

    def _registrar_recuperacion_etapa(self, etapa: str):
        if not hasattr(self, "_estado_cv"):
            return
        with self._estado_cv:
            if etapa == "stt" and self._stt_failures:
                self._stt_health = "recovered"
                self.estado_operativo.resolver_problema("stt_degradado")
            elif etapa == "vad" and self._vad_failures:
                self._vad_health = "recovered"
                self.estado_operativo.resolver_problema("vad_degradado")

    def _actualizar_salud_vad(self, segmentador, vistos: int,
                              generacion: int) -> int:
        fallos = getattr(segmentador, "vad_failures", None)
        if fallos is None:
            return vistos
        if fallos > vistos:
            tipo = getattr(segmentador, "last_vad_error_type", None) or "Exception"
            self._registrar_fallo_etapa(
                "vad", tipo, generacion, cantidad=fallos - vistos)
        elif getattr(segmentador, "vad_last_ok", False):
            self._registrar_recuperacion_etapa("vad")
        return fallos

    def _snapshot_streaming(self):
        return (getattr(self.streaming, "decodificaciones", None),
                getattr(self.streaming, "fallos", None))

    def _actualizar_salud_streaming(
            self, antes, generacion: int,
            permitir_recuperacion: bool = True) -> bool:
        dec_antes, fallos_antes = antes
        dec_despues = getattr(self.streaming, "decodificaciones", None)
        fallos_despues = getattr(self.streaming, "fallos", None)
        if fallos_antes is not None and fallos_despues is not None \
                and fallos_despues > fallos_antes:
            tipo = getattr(self.streaming, "last_error_type", None) or "Exception"
            self._registrar_fallo_etapa(
                "stt", tipo, generacion,
                cantidad=fallos_despues - fallos_antes)
            return True
        if permitir_recuperacion \
                and dec_antes is not None and dec_despues is not None \
                and dec_despues > dec_antes:
            self._registrar_recuperacion_etapa("stt")
            return True
        return False

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
            self._pausa_pendiente = False
            self._estado_cv.notify_all()
        self._estado_visual_terminal_si_vigente(
            "error" if con_error else "idle",
            EstadoApp.ERROR if con_error else EstadoApp.IDLE,
            generacion,
        )
        print(f"[app] ○ detenido (sesión {generacion})")

    def _evento_vad(self, hablando: bool, generacion: int):
        with self._salida_lock:
            if not self._puede_emit(generacion):
                return
            self.salida.evento_vad(hablando)

    def _estado_visual_terminal_si_vigente(
            self, estado: str, estado_app: EstadoApp, generacion: int) -> bool:
        """Publica un terminal solo si ninguna generación lo reemplazó."""
        with self._salida_lock:
            with self._estado_cv:
                vigente = (
                    self._generacion == generacion
                    and self._sesion_activa is None
                    and self._estado == estado_app
                )
            if vigente:
                self._ui_completar_generacion(
                    generacion, con_error=estado == "error")
            return vigente

    def _registrar_fallo_worker(self, exc: Exception):
        print(f"[app] fallo inesperado del worker: {type(exc).__name__}",
              file=sys.stderr)
        generacion = None
        with self._transicion_lock:
            with self._salida_lock:
                with self._estado_cv:
                    generacion = self._sesion_activa or self._generacion
                    if self._estado not in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                        self._estado = EstadoApp.ERROR
                        self._pausa_pendiente = False
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
        self._estado_visual_terminal_si_vigente(
            "error", EstadoApp.ERROR, generacion)
        self._fallo_operativo(
            "worker_fallido", "runtime",
            "El proceso de dictado dejó de funcionar.", exc)
        self.estado_operativo.actualizar_runtime(EstadoRuntime.ERROR)

    # ------------------------------------------------------------ control

    def _respuesta_estado(self) -> str:
        with self._estado_cv:
            estado = self._estado
            error = self._ultimo_error
            salud_etapas = (
                f"stt={self._stt_health} stt_failures={self._stt_failures} "
                f"last_stt_error_type={self._last_stt_error_type or 'none'} "
                f"vad={self._vad_health} vad_failures={self._vad_failures} "
                f"last_vad_error_type={self._last_vad_error_type or 'none'}"
            )
        if estado == EstadoApp.ERROR:
            base = f"error detalle={error or 'desconocido'}"
        elif estado == EstadoApp.RECORDING:
            base = "grabando"
        elif estado == EstadoApp.STOPPING:
            base = "deteniendo"
        elif estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
            base = "cerrando" if estado == EstadoApp.SHUTTING_DOWN else "cerrado"
            if error:
                base += f" detalle={error}"
        else:
            base = "inactivo"
        respuesta = (base + f" modo={self._modo_solicitado} "
                     f"reescritura={self.cfg.rewrite_mode} {salud_etapas}")
        obtener_estado = getattr(self.mic, "estado_captura", None)
        if obtener_estado is None:
            return respuesta
        captura = obtener_estado()
        device_overflows = getattr(captura, "device_overflows", 0)
        parciales_descartadas = getattr(
            captura, "muestras_parciales_descartadas", 0)
        if captura.degradada:
            salud = "degradado"
        elif captura.frames_descartados or device_overflows:
            salud = "recuperado-con-perdida"
        else:
            salud = "saludable"
        return (respuesta + f" audio={salud} drops={captura.frames_descartados} "
                f"device_overflows={device_overflows} "
                f"partial_samples_discarded={parciales_descartadas} "
                f"discontinuidades={captura.discontinuidades} "
                f"cola={captura.queue_depth}/{self.mic.capacidad} "
                f"backlog_ms={captura.backlog_ms:.1f}")

    _TOKEN_SUSPENSION_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")

    def _parametros_suspension_atajo(self, partes):
        if len(partes) != 4 or partes[1] != "v1":
            return None
        token = partes[2]
        if self._TOKEN_SUSPENSION_RE.fullmatch(token) is None:
            return None
        try:
            duracion_ms = int(partes[3])
        except ValueError:
            return None
        if not 1000 <= duracion_ms <= 10000:
            return None
        return token, duracion_ms / 1000.0

    def _sincronizar_suspension_atajo(self) -> None:
        snapshot = self.estado_operativo.snapshot()
        if snapshot.hotkey != EstadoComponente.SUSPENDED:
            return
        consultar = getattr(self.atajos, "esta_suspendido", None)
        if not callable(consultar) or not consultar():
            self.estado_operativo.fijar_componente(
                "hotkey", EstadoComponente.READY)

    def _comando_suspension_atajo(self, op: str, partes: list[str]) -> str:
        parametros = self._parametros_suspension_atajo(partes)
        if parametros is None:
            return "ERR protocolo hotkey v1 inválido"
        token, duracion_s = parametros
        with self._estado_cv:
            if self._estado != EstadoApp.IDLE:
                return "ERR hotkey ocupado"
        estado_hotkey = self.estado_operativo.snapshot().hotkey
        if estado_hotkey not in {
                EstadoComponente.READY, EstadoComponente.SUSPENDED}:
            return "ERR hotkey no disponible"
        metodo = (
            getattr(self.atajos, "suspender", None)
            if op == "hotkey-suspender"
            else getattr(self.atajos, "renovar_suspension", None)
        )
        if not callable(metodo) or not metodo(token, duracion_s):
            return "ERR lease hotkey rechazado"
        self.estado_operativo.fijar_componente(
            "hotkey", EstadoComponente.SUSPENDED)
        return "OK hotkey suspendido v1"

    def _comando_restaurar_atajo(self, partes: list[str]) -> str:
        if (
            len(partes) != 3
            or partes[1] != "v1"
            or self._TOKEN_SUSPENSION_RE.fullmatch(partes[2]) is None
        ):
            return "ERR protocolo hotkey v1 inválido"
        restaurar = getattr(self.atajos, "restaurar", None)
        if not callable(restaurar) or not restaurar(partes[2]):
            self._sincronizar_suspension_atajo()
            return "ERR lease hotkey desconocido"
        self.estado_operativo.fijar_componente(
            "hotkey", EstadoComponente.READY)
        return "OK hotkey restaurado v1"

    def _atender_comando(self, cmd: str) -> str:
        partes = normalizar_comando(cmd)
        if not partes:
            return "ERR vacío"
        op = partes[0]
        self._sincronizar_suspension_atajo()
        if op in {"hotkey-suspender", "hotkey-renovar"}:
            return self._comando_suspension_atajo(op, partes)
        if op == "hotkey-restaurar":
            return self._comando_restaurar_atajo(partes)
        if op == "alternar":
            return self._respuesta_control_lifecycle(self.alternar())
        if op == "iniciar":
            return self._respuesta_control_lifecycle(self.iniciar_grabacion())
        if op == "detener":
            return self._respuesta_control_lifecycle(self.detener_grabacion())
        if op == "cancelar":
            return self._respuesta_control_lifecycle(self.cancelar_grabacion())
        if op == "pausar":
            return "OK pausado" if self.pausar() else "ERR aplicación cerrada"
        if op == "reanudar":
            return "OK reanudado" if self.reanudar() else "ERR aplicación cerrada"
        if op == "estado":
            return self._respuesta_estado()
        if op == "estado-operativo":
            return serializar_estado(self.estado_operativo.snapshot())
        if op == "estado-tray":
            with self._estado_cv:
                estado = self._estado.value
                pausa_pendiente = self._pausa_pendiente
            return serializar_estado_tray(resumir_estado_tray(
                estado, self.estado_operativo.snapshot(),
                pausa_pendiente=pausa_pendiente))
        if op == "abrir-configuracion":
            with self._estado_cv:
                estado = self._estado
            foco = "allow" if foco_settings_seguro(estado) else "defer"
            return json.dumps({
                "schema_version": 1,
                "runtime": estado.value,
                "focus": foco,
            }, separators=(",", ":"))
        if op == "modo" and len(partes) > 1 and partes[1] in Config.MODOS:
            return (f"OK modo={partes[1]}" if self.cambiar_modo(partes[1])
                    else "ERR aplicación cerrada")
        if op == "reescritura" and len(partes) > 1 \
                and partes[1] in Config.REESCRITURAS:
            with self._estado_cv:
                if self._estado in (EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED):
                    return "ERR aplicación cerrada"
            self.cfg.rewrite_mode = partes[1]
            self.proc.rewrite_mode = partes[1]
            return f"OK reescritura={partes[1]}"
        if op == "salir":
            self.salir(esperar=False)
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
