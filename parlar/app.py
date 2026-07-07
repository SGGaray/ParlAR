"""Orquestador del daemon ParlAR.

Hilos:
  principal   indicador tkinter (o bucle sin UI)
  trabajador  frames de audio -> VAD -> STT -> procesador de texto -> inyector
  control     servidor de socket unix
  atajos      listener pynput (solo X11)
  audio cb    callback de PortAudio (solo encola)
"""

import sys
import threading
import time

from .capturador_audio import CapturadorMic, Segmentador, crear_vad
from .config import Config
from .control import ServidorControl, normalizar_comando
from .daemon_atajos import DaemonAtajos
from .inyector_salida import Inyector
from .indicador import crear_ui
from .motor_transcripcion import TranscriptorStreaming, TranscriptorFrase, MotorWhisper
from .procesador_texto import ProcesadorTexto


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.grabando = threading.Event()
        self.saliendo = threading.Event()
        self._necesita_espacio = False  # espaciado inteligente entre frases inyectadas

        print("=" * 60)
        print("ParlAR: dictado local. Nada sale de esta máquina.")
        print("=" * 60)

        self.motor = MotorWhisper(cfg.model_size, cfg.device, cfg.compute_type,
                                  cfg.language, cfg.beam_size)
        self.frases = TranscriptorFrase(self.motor)
        self.streaming = TranscriptorStreaming(self.motor, cfg.sample_rate,
                                               cfg.stream_interval_s, cfg.stream_trim_s)
        self.proc = ProcesadorTexto(cfg.remove_fillers, cfg.voice_commands,
                                    cfg.rewrite_mode, cfg.ollama_model, cfg.ollama_url)
        self.inyector = Inyector(cfg.injector, cfg.type_delay_ms, cfg.notify)
        self.mic = CapturadorMic(cfg.sample_rate, cfg.frame_samples)
        self.ui = crear_ui(cfg.overlay, al_click=self.alternar)

        self.control = ServidorControl(self._atender_comando)
        self.atajos = DaemonAtajos(cfg.hotkey_toggle, cfg.hotkey_quit,
                                   al_alternar=self.alternar, al_salir=self.salir)

    # ------------------------------------------------------------ ciclo de vida

    def ejecutar(self):
        self.control.iniciar()
        self.atajos.iniciar()
        trabajador = threading.Thread(target=self._trabajador, name="trabajador", daemon=True)
        trabajador.start()
        pista = (f"atajo {self.cfg.hotkey_toggle}" if self.atajos._listener
                 else "`parlarctl alternar`")
        print(f"[app] listo. Modo: {self.cfg.mode}. Iniciá/detené con {pista}, "
              f"o con un click en el punto del indicador.")
        try:
            self.ui.ejecutar()  # bloquea el hilo principal
        except KeyboardInterrupt:
            pass
        finally:
            self.salir()
            trabajador.join(timeout=5)
            self.control.detener()
            self.atajos.detener()

    def alternar(self):
        if self.grabando.is_set():
            self.detener_grabacion()
        else:
            self.iniciar_grabacion()

    def iniciar_grabacion(self):
        if self.grabando.is_set():
            return
        try:
            self.mic.iniciar()
        except Exception as e:
            print(f"[app] no se pudo abrir el micrófono: {e}", file=sys.stderr)
            return
        self.streaming.reiniciar()
        self.inyector.reiniciar_registro()
        self._necesita_espacio = False
        self.grabando.set()
        self.ui.fijar_estado("recording")
        print("[app] ● grabando")

    def detener_grabacion(self):
        if not self.grabando.is_set():
            return
        self.grabando.clear()
        self.mic.detener()
        self.ui.fijar_estado("idle")
        print("[app] ○ detenido")

    def salir(self):
        self.detener_grabacion()
        self.saliendo.set()
        self.ui.cerrar()

    # ------------------------------------------------------------ trabajador

    def _trabajador(self):
        cfg = self.cfg
        segmentador = None
        while not self.saliendo.is_set():
            if not self.grabando.is_set():
                segmentador = None
                time.sleep(0.05)
                continue
            if segmentador is None:
                segmentador = Segmentador(
                    cfg.sample_rate, cfg.frame_ms,
                    crear_vad(cfg.vad_aggressiveness, cfg.sample_rate),
                    cfg.silence_ms, cfg.preroll_ms,
                    cfg.max_utterance_s, cfg.min_speech_ms,
                )
            frame = self.mic.leer_frame(timeout=0.1)
            if frame is None:
                # modo streaming: el silencio también puede disparar una pasada
                if cfg.mode == "streaming":
                    self._paso_streaming()
                continue
            for ev in segmentador.procesar(frame):
                if ev.tipo == "inicio_voz":
                    self.ui.fijar_estado("recording")
                elif ev.tipo == "frame_voz" and cfg.mode == "streaming":
                    self.streaming.aceptar_audio(ev.audio)
                    self._paso_streaming()
                elif ev.tipo == "frase":
                    if cfg.mode == "streaming":
                        self._vaciar_streaming()
                    else:
                        self._atender_frase(ev.audio)

        # drena al salir
        if cfg.mode == "streaming":
            self._vaciar_streaming()

    def _atender_frase(self, audio):
        self.ui.fijar_estado("transcribing")
        t0 = time.time()
        try:
            crudo = self.frases.transcribir(audio)
        except Exception as e:
            print(f"[app] la transcripción falló: {e}", file=sys.stderr)
            crudo = ""
        dt = time.time() - t0
        if crudo:
            print(f"[app] ({dt:.2f}s) {crudo!r}")
            self._emitir(self.proc.procesar_frase(crudo))
        self.ui.fijar_estado("recording" if self.grabando.is_set() else "idle")

    def _paso_streaming(self):
        try:
            trozo = self.streaming.procesar()
        except Exception as e:
            print(f"[app] falló la decodificación streaming: {e}", file=sys.stderr)
            return
        if trozo:
            texto = self.proc.procesar_fragmento(trozo)
            if texto:
                self.inyector.escribir_texto(texto, registrar=False)

    def _vaciar_streaming(self):
        cola = self.streaming.finalizar()
        if cola:
            texto = self.proc.procesar_fragmento(cola)
            if texto:
                self.inyector.escribir_texto(texto, registrar=False)

    # ------------------------------------------------------------ emisión

    def _emitir(self, p):
        if p.comando == "nueva_linea":
            cantidad = p.carga.count("\n") if p.carga else 1
            self.inyector.nueva_linea(cantidad)
            self._necesita_espacio = False
            return
        if p.comando == "borrar_ultima":
            if not self.inyector.borrar_ultima_oracion():
                print("[app] no hay nada para borrar")
            return
        if p.comando == "detener":
            self.detener_grabacion()
            return
        if p.comando == "enviar":
            self.inyector.presionar_enter()
            self._necesita_espacio = False
            return
        if p.texto:
            salida = (" " + p.texto) if self._necesita_espacio else p.texto
            if self.inyector.escribir_texto(salida):
                self._necesita_espacio = True

    # ------------------------------------------------------------ control

    def _atender_comando(self, cmd: str) -> str:
        partes = normalizar_comando(cmd)
        if not partes:
            return "ERR vacío"
        op = partes[0]
        if op == "alternar":
            self.alternar()
            return "OK grabando" if self.grabando.is_set() else "OK detenido"
        if op == "iniciar":
            self.iniciar_grabacion()
            return "OK grabando"
        if op == "detener":
            self.detener_grabacion()
            return "OK detenido"
        if op == "estado":
            return ("grabando" if self.grabando.is_set() else "inactivo") \
                + f" modo={self.cfg.mode} reescritura={self.cfg.rewrite_mode}"
        if op == "modo" and len(partes) > 1 and partes[1] in ("utterance", "streaming"):
            self.cfg.mode = partes[1]
            return f"OK modo={partes[1]}"
        if op == "reescritura" and len(partes) > 1 \
                and partes[1] in ("none", "formal", "concise", "email"):
            self.cfg.rewrite_mode = partes[1]
            self.proc.rewrite_mode = partes[1]
            return f"OK reescritura={partes[1]}"
        if op == "salir":
            threading.Thread(target=self.salir, daemon=True).start()
            return "OK chau"
        return "ERR comando desconocido"
