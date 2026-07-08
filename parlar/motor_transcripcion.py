"""Motor de transcripción sobre faster-whisper (backend CTranslate2).

Dos estrategias:

- TranscriptorFrase: decodificación única de una frase cerrada por VAD.
  Precisa, simple; latencia = umbral de silencio + inferencia.

- TranscriptorStreaming: política LocalAgreement-2 (Liao et al.,
  whisper_streaming). La ventana de audio creciente se re-decodifica con una
  cadencia; solo se confirman las palabras en las que dos hipótesis
  consecutivas coinciden. El audio confirmado se recorta para que el costo de
  decodificación quede acotado en sesiones largas. Como solo se emiten
  palabras estables, el texto inyectado nunca necesita retractarse.

El límite de módulo acá es deliberado: si la latencia algún día exige
whisper.cpp, solo cambia este archivo.
"""

import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


def _elegir_dispositivo(device: str, compute_type: str):
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


class MotorWhisper:
    """Dueño del modelo cargado; compartido por ambas estrategias."""

    def __init__(self, model_size: str, device: str, compute_type: str,
                 language: str = "", beam_size: int = 5):
        from faster_whisper import WhisperModel
        device, compute_type = _elegir_dispositivo(device, compute_type)
        print(f"[stt] cargando faster-whisper '{model_size}' en {device} ({compute_type})...")
        t0 = time.time()
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        print(f"[stt] modelo listo en {time.time() - t0:.1f}s")
        self.language = language or None
        self.beam_size = beam_size

    def decodificar(self, audio: np.ndarray, *, word_timestamps: bool = False,
                    beam_size: Optional[int] = None):
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=beam_size or self.beam_size,
            word_timestamps=word_timestamps,
            vad_filter=False,          # ya hacemos VAD aguas arriba
            condition_on_previous_text=False,
        )
        return list(segments)


# Umbral heurístico: un segmento se descarta como alucinación probable solo
# si el modelo señala BAJA confianza de que haya voz (no_speech_prob alto)
# Y BAJA confianza en el texto que generó igual (avg_logprob muy negativo).
# Exigir las dos condiciones evita descartar voz real y susurrada.
_UMBRAL_NO_SPEECH = 0.6
_UMBRAL_LOGPROB = -1.0

# Frases que Whisper "alucina" típicamente sobre silencio o ruido de fondo
# (artefacto conocido del entrenamiento en subtítulos de YouTube)
_ALUCINACIONES_CONOCIDAS = re.compile(
    r"subt[ií]tulos.*amara\.org|www\.youtube\.com|suscr[ií]bete|"
    r"subscribe to|like and subscribe|gracias por ver el v[ií]deo",
    re.IGNORECASE,
)


class TranscriptorFrase:
    def __init__(self, motor: MotorWhisper):
        self.motor = motor

    def transcribir(self, audio: np.ndarray) -> str:
        if audio.size < 1600:  # < 0.1s
            return ""
        segments = self.motor.decodificar(audio)
        partes = []
        for s in segments:
            if s.no_speech_prob > _UMBRAL_NO_SPEECH and s.avg_logprob < _UMBRAL_LOGPROB:
                continue  # probable alucinación: silencio con baja confianza
            partes.append(s.text.strip())
        texto = " ".join(partes).strip()
        if _ALUCINACIONES_CONOCIDAS.search(texto):
            return ""
        return texto


@dataclass
class _Palabra:
    texto: str
    fin: float  # segundos, relativo al inicio del buffer actual


_re_norm = re.compile(r"[^\w']+", re.UNICODE)


def _norm(w: str) -> str:
    return _re_norm.sub("", w).lower()


class TranscriptorStreaming:
    """Transcripción incremental LocalAgreement-2 sobre un buffer creciente."""

    def __init__(self, motor: MotorWhisper, sample_rate: int = 16000,
                 interval_s: float = 1.0, trim_s: float = 12.0):
        self.motor = motor
        self.sr = sample_rate
        self.interval_s = interval_s
        self.trim_s = trim_s
        self.reiniciar()

    def reiniciar(self):
        self.buffer = np.zeros(0, dtype=np.float32)
        self.palabras_prev: List[_Palabra] = []
        self.n_confirmadas = 0
        self._ultimo_len_decodificado = 0

    def aceptar_audio(self, trozo: np.ndarray):
        self.buffer = np.concatenate([self.buffer, trozo.astype(np.float32)])

    def _decodificar_palabras(self, audio: np.ndarray) -> List[_Palabra]:
        segments = self.motor.decodificar(audio, word_timestamps=True, beam_size=1)
        palabras: List[_Palabra] = []
        for seg in segments:
            for w in (seg.words or []):
                palabras.append(_Palabra(texto=w.word, fin=w.end))
        return palabras

    def procesar(self) -> str:
        """Decodifica si llegó suficiente audio nuevo; devuelve el texto recién
        confirmado ('' si no hay)."""
        muestras_nuevas = self.buffer.size - self._ultimo_len_decodificado
        if muestras_nuevas < int(self.interval_s * self.sr) or self.buffer.size < self.sr // 2:
            return ""
        self._ultimo_len_decodificado = self.buffer.size

        palabras = self._decodificar_palabras(self.buffer)

        # prefijo común más largo entre hipótesis consecutivas
        k = 0
        while (k < len(palabras) and k < len(self.palabras_prev)
               and _norm(palabras[k].texto) == _norm(self.palabras_prev[k].texto)):
            k += 1
        self.palabras_prev = palabras

        nuevas = palabras[self.n_confirmadas:k] if k > self.n_confirmadas else []
        if k > self.n_confirmadas:
            self.n_confirmadas = k
        salida = "".join(p.texto for p in nuevas)

        # recorta audio confirmado para acotar el costo en sesiones largas
        if self.buffer.size > int(self.trim_s * self.sr) and self.n_confirmadas > 0:
            t_corte = palabras[self.n_confirmadas - 1].fin
            corte = min(int(t_corte * self.sr), self.buffer.size)
            if corte > 0:
                self.buffer = self.buffer[corte:]
                self._ultimo_len_decodificado = self.buffer.size
                self.palabras_prev = []
                self.n_confirmadas = 0

        return salida

    def hipotesis_pendiente(self) -> str:
        """Texto decodificado pero aún no confirmado por LocalAgreement.
        Solo lectura; útil como vista previa (p. ej. teleprompter)."""
        return "".join(p.texto for p in self.palabras_prev[self.n_confirmadas:]).strip()

    def finalizar(self) -> str:
        """Vaciado: decodifica lo que queda y devuelve el texto más allá de
        las palabras ya confirmadas."""
        if self.buffer.size < 1600:
            self.reiniciar()
            return ""
        try:
            palabras = self._decodificar_palabras(self.buffer)
            cola = palabras[self.n_confirmadas:]
            salida = "".join(p.texto for p in cola)
        except Exception as e:
            print(f"[stt] finalizar falló: {e}", file=sys.stderr)
            salida = ""
        self.reiniciar()
        return salida
