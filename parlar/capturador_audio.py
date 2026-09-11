"""Captura de micrófono y segmentación por actividad de voz (VAD).

Diseño: el callback de sounddevice SOLO encola frames crudos (nunca bloquea).
Un worker (en app.py) consume frames y los pasa por Segmentador, una máquina
de estados pura, totalmente testeable sin hardware de audio.
"""

import collections
import queue
import sys
import threading
from dataclasses import dataclass
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


# ---------------------------------------------------------------- CapturadorMic

class CapturadorMic:
    """Captura continua del micrófono en una cola acotada de frames tamaño VAD."""

    def __init__(self, sample_rate: int, frame_samples: int, max_cola: int = 500):
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.q: "queue.Queue[FrameAudio]" = queue.Queue(maxsize=max_cola)
        self._stream = None
        self._resto = b""
        self._estado = "idle"
        self._generacion_aceptada: Optional[int] = None
        self._lock = threading.Condition()
        self._callback_lock = threading.Lock()

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
                trozo = FrameAudio(generacion, crudo[i * fb:(i + 1) * fb])
                try:
                    self.q.put_nowait(trozo)
                except queue.Full:
                    try:  # descarta el más viejo para mantenerse en tiempo real
                        self.q.get_nowait()
                        self.q.put_nowait(trozo)
                    except queue.Empty:
                        pass
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
                self._vaciar_cola()
                self._resto = b""
                self._generacion_aceptada = generacion
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
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None
