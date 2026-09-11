"""Captura de micrófono y segmentación por actividad de voz (VAD).

Diseño: el callback de sounddevice SOLO encola frames crudos (nunca bloquea).
Un worker (en app.py) consume frames y los pasa por Segmentador, una máquina
de estados pura, totalmente testeable sin hardware de audio.
"""

import collections
import queue
import sys
import threading
from dataclasses import dataclass, replace
from typing import Iterator, Optional

import numpy as np

try:
    import webrtcvad
    _HAY_WEBRTCVAD = True
except ImportError:
    _HAY_WEBRTCVAD = False


# ---------------------------------------------------------------- backends VAD

class _VADEnergia:
    """VAD de respaldo: umbral de energía adaptativo. Solo si falta webrtcvad."""

    def __init__(self, sample_rate: int):
        self.piso_ruido = 300.0  # unidades RMS int16, se adapta

    def is_speech(self, frame_bytes: bytes, sample_rate: int) -> bool:
        pcm = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(pcm * pcm) + 1e-9))
        es_voz = rms > self.piso_ruido * 2.5
        if not es_voz:
            # sigue lentamente el piso de ruido
            self.piso_ruido = 0.98 * self.piso_ruido + 0.02 * max(rms, 50.0)
        return es_voz


def crear_vad(agresividad: int, sample_rate: int):
    if _HAY_WEBRTCVAD:
        return webrtcvad.Vad(agresividad)
    print("[audio] webrtcvad no disponible, usando VAD de energía", file=sys.stderr)
    return _VADEnergia(sample_rate)


# ---------------------------------------------------------------- Segmentador

@dataclass
class EventoSegmento:
    tipo: str                            # "inicio_voz" | "frame_voz" | "frase"
    audio: Optional[np.ndarray] = None   # float32 mono para "frase" / "frame_voz"


@dataclass(frozen=True)
class FrameAudio:
    """Frame copiado por PortAudio, ligado a la apertura que lo produjo."""

    generacion: int
    audio: bytes
    secuencia: int = 0
    discontinuidad_antes: bool = False


@dataclass(frozen=True)
class EstadoCaptura:
    """Snapshot operativo de la generación de captura actual."""

    generacion: Optional[int]
    frames_capturados: int
    frames_entregados: int
    frames_descartados: int
    discontinuidades: int
    queue_depth: int
    max_queue_depth: int
    backlog_ms: float
    degradada: bool
    ultima_discontinuidad: Optional[int]


class Segmentador:
    """Segmentación de frases con puerta VAD y pre-roll.

    Alimentá frames PCM de 16 bits de exactamente `frame_ms` vía procesar();
    devuelve EventoSegmento. Lógica pura, sin E/S, testeable.
    """

    def __init__(self, sample_rate: int, frame_ms: int, vad,
                 silence_ms: int, preroll_ms: int,
                 max_utterance_s: float, min_speech_ms: int):
        self.sr = sample_rate
        self.frame_ms = frame_ms
        self.vad = vad
        self.frames_silencio = max(1, silence_ms // frame_ms)
        self.min_frames_voz = max(1, min_speech_ms // frame_ms)
        self.max_frames = int(max_utterance_s * 1000 // frame_ms)
        self.preroll = collections.deque(maxlen=max(1, preroll_ms // frame_ms))
        self._reiniciar()

    def _reiniciar(self):
        self.en_voz = False
        self.racha_silencio = 0
        self.racha_voz = 0
        self.frames: list[bytes] = []

    @staticmethod
    def _a_float32(crudo: bytes) -> np.ndarray:
        return np.frombuffer(crudo, dtype=np.int16).astype(np.float32) / 32768.0

    def procesar(self, frame: bytes) -> Iterator[EventoSegmento]:
        try:
            con_voz = self.vad.is_speech(frame, self.sr)
        except Exception:
            con_voz = True  # falla abierto: mejor transcribir silencio que perder voz

        if not self.en_voz:
            self.preroll.append(frame)
            if con_voz:
                self.racha_voz += 1
                if self.racha_voz >= self.min_frames_voz:
                    # Voz confirmada: arranca la frase incluyendo el pre-roll.
                    self.en_voz = True
                    self.racha_silencio = 0
                    self.frames = list(self.preroll)
                    yield EventoSegmento("inicio_voz")
                    yield EventoSegmento(
                        "frame_voz",
                        audio=self._a_float32(b"".join(self.frames)),
                    )
            else:
                self.racha_voz = 0
            return

        # dentro de voz
        self.frames.append(frame)
        yield EventoSegmento("frame_voz", audio=self._a_float32(frame))

        if con_voz:
            self.racha_silencio = 0
        else:
            self.racha_silencio += 1

        termino = self.racha_silencio >= self.frames_silencio
        muy_larga = len(self.frames) >= self.max_frames
        if termino or muy_larga:
            audio = self._a_float32(b"".join(self.frames))
            self._reiniciar()
            self.preroll.clear()
            yield EventoSegmento("frase", audio=audio)

    def finalizar(self) -> Iterator[EventoSegmento]:
        """Cierra una frase confirmada sin exigir silencio adicional.

        Se usa al detener una sesión: el micrófono ya no acepta audio nuevo,
        pero la voz previamente aceptada no debe perderse.
        """
        if not self.en_voz or not self.frames:
            self._reiniciar()
            self.preroll.clear()
            return
        audio = self._a_float32(b"".join(self.frames))
        self._reiniciar()
        self.preroll.clear()
        yield EventoSegmento("frase", audio=audio)

    def discontinuidad(self) -> Iterator[EventoSegmento]:
        """Cierra la unidad contigua previa al gap y limpia todo contexto.

        Si todavía no había voz confirmada, sólo invalida pre-roll y rachas.
        Nunca inventa silencio ni une muestras de ambos lados del hueco.
        """
        yield from self.finalizar()


# ---------------------------------------------------------------- CapturadorMic

class CapturadorMic:
    """Captura continua del micrófono en una cola acotada de frames tamaño VAD."""

    def __init__(self, sample_rate: int, frame_samples: int, max_cola: int = 500):
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.capacidad = max_cola
        self.q: "queue.Queue[FrameAudio]" = queue.Queue(maxsize=max_cola)
        self._stream = None
        self._resto = b""
        self._estado = "idle"
        self._generacion_aceptada: Optional[int] = None
        self._generacion_metricas: Optional[int] = None
        self._lock = threading.Condition()
        self._callback_lock = threading.Lock()
        self._secuencia = 0
        self._ultima_secuencia_entregada = 0
        self._frames_capturados = 0
        self._frames_entregados = 0
        self._frames_descartados = 0
        self._discontinuidades = 0
        self._max_queue_depth = 0
        self._gap_pendiente = False
        self._ultima_discontinuidad: Optional[int] = None

    def _preparar_generacion(self, generacion: int):
        """Inicializa cola y contadores. Requiere ``_callback_lock``."""
        self._vaciar_cola()
        self._resto = b""
        self._generacion_aceptada = generacion
        self._generacion_metricas = generacion
        self._secuencia = 0
        self._ultima_secuencia_entregada = 0
        self._frames_capturados = 0
        self._frames_entregados = 0
        self._frames_descartados = 0
        self._discontinuidades = 0
        self._max_queue_depth = 0
        self._gap_pendiente = False
        self._ultima_discontinuidad = None

    def _callback(self, generacion: int, indata, frames, time_info, status):
        if status:
            print(f"[audio] estado del stream: {status}", file=sys.stderr)
        # El lock es breve y exclusivo del callback/buffer. No cubre ninguna
        # operación de PortAudio ni inferencia. Al detener, primero se invalida
        # la generación y después se toma este lock para drenar con seguridad.
        with self._callback_lock:
            if generacion != self._generacion_aceptada:
                return
            crudo = self._resto + bytes(indata)
            fb = self.frame_samples * 2
            n = len(crudo) // fb
            for i in range(n):
                self._secuencia += 1
                self._frames_capturados += 1
                trozo = FrameAudio(
                    generacion,
                    crudo[i * fb:(i + 1) * fb],
                    secuencia=self._secuencia,
                )
                try:
                    self.q.put_nowait(trozo)
                except queue.Full:
                    try:  # conserva el audio más reciente sin bloquear callback
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        self._frames_descartados += 1
                        self._gap_pendiente = True
                    self.q.put_nowait(trozo)
                self._max_queue_depth = max(
                    self._max_queue_depth, self.q.qsize())
            self._resto = crudo[n * fb:]

    def iniciar(self, generacion: int) -> bool:
        """Abre una única captura para ``generacion``.

        Devuelve ``False`` si otra apertura ya está activa o en progreso. La
        creación/start del stream ocurre fuera del lock para no bloquear un
        callback de PortAudio que pueda arrancar durante ``start()``.
        """
        with self._lock:
            if self._estado != "idle":
                return False
            self._estado = "starting"

        stream = None
        import sounddevice as sd  # import diferido para testear sin hardware
        try:
            with self._callback_lock:
                self._preparar_generacion(generacion)
            stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.frame_samples,
                callback=lambda indata, frames, time_info, status: self._callback(
                    generacion, indata, frames, time_info, status),
            )
            stream.start()
        except BaseException:
            with self._callback_lock:
                self._generacion_aceptada = None
                self._generacion_metricas = None
                self._resto = b""
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            with self._lock:
                self._estado = "idle"
                self._lock.notify_all()
            raise

        with self._lock:
            self._stream = stream
            self._estado = "recording"
            self._lock.notify_all()
        return True

    def detener(self, *, vaciar: bool = True):
        """Detiene y cierra el stream; ``close`` se intenta aunque falle stop."""
        with self._lock:
            while self._estado in ("starting", "stopping"):
                self._lock.wait()
            if self._estado == "idle":
                if vaciar:
                    with self._callback_lock:
                        self._vaciar_cola()
                        self._resto = b""
                return
            stream = self._stream
            self._stream = None
            self._estado = "stopping"

        # Ningún callback nuevo puede aceptar datos desde este punto. Un
        # callback que ya tenía el lock termina antes del drenaje opcional.
        with self._callback_lock:
            self._generacion_aceptada = None

        error = None
        try:
            if stream is not None:
                stream.stop()
        except BaseException as exc:
            error = exc
        finally:
            try:
                if stream is not None:
                    stream.close()
            except BaseException as exc:
                if error is None:
                    error = exc

        with self._callback_lock:
            self._resto = b""
            if vaciar:
                self._vaciar_cola()
        with self._lock:
            self._estado = "idle"
            self._lock.notify_all()
        if error is not None:
            raise error

    def descartar_pendientes(self, generacion: Optional[int] = None):
        """Descarta frames de una generación invalidada (o todos)."""
        with self._callback_lock:
            if generacion is None:
                self._vaciar_cola()
                return
            conservar = []
            while True:
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                if item.generacion != generacion:
                    conservar.append(item)
            for item in conservar:
                self.q.put_nowait(item)

    def _vaciar_cola(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def leer_frame(self, timeout: float = 0.1) -> Optional[FrameAudio]:
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._callback_lock:
            if item.generacion != self._generacion_metricas:
                return item
            self._frames_entregados += 1
            discontinuidad = item.discontinuidad_antes
            if item.secuencia > 0:
                esperado = self._ultima_secuencia_entregada + 1
                discontinuidad = discontinuidad or item.secuencia != esperado
                self._ultima_secuencia_entregada = item.secuencia
            if discontinuidad:
                self._discontinuidades += 1
                self._ultima_discontinuidad = item.secuencia or None
                self._gap_pendiente = False
                return replace(item, discontinuidad_antes=True)
            return item

    def estado_captura(self) -> EstadoCaptura:
        """Devuelve métricas consistentes sin audio ni contenido transcripto."""
        with self._callback_lock:
            profundidad = self.q.qsize()
            frame_ms = self.frame_samples * 1000.0 / self.sample_rate
            return EstadoCaptura(
                generacion=self._generacion_metricas,
                frames_capturados=self._frames_capturados,
                frames_entregados=self._frames_entregados,
                frames_descartados=self._frames_descartados,
                discontinuidades=self._discontinuidades,
                queue_depth=profundidad,
                max_queue_depth=self._max_queue_depth,
                backlog_ms=profundidad * frame_ms,
                degradada=self._gap_pendiente,
                ultima_discontinuidad=self._ultima_discontinuidad,
            )
