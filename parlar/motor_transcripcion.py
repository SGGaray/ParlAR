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
import time
import unicodedata
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


# Umbral heurístico: la baja confianza acústica no basta por sí sola para
# borrar texto. Se combina con un patrón conocido y se decide por segmento.
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
            texto_segmento = s.text.strip()
            baja_confianza = (
                s.no_speech_prob > _UMBRAL_NO_SPEECH
                and s.avg_logprob < _UMBRAL_LOGPROB
            )
            sospechoso = bool(_ALUCINACIONES_CONOCIDAS.search(texto_segmento))
            if baja_confianza and sospechoso:
                continue
            partes.append(texto_segmento)
        return " ".join(partes).strip()


@dataclass
class _Palabra:
    texto: str
    fin: float  # segundos, relativo al inicio del buffer actual
    inicio: Optional[float] = None


_PUNTUACION_COSMETICA = " \t\r\n.,;:!?¿¡…\"“”«»'‘’()[]{}"


def _norm(w: str) -> str:
    """Identidad lexical para agreement, sin modificar el texto emitido.

    Sólo ignora mayúsculas y puntuación de frase en los bordes. Símbolos con
    valor lexical como ``+``, ``#``, ``/``, ``_`` o puntos internos se
    conservan, por lo que C++, C#, HTTP/2, foo.bar y foo_bar no colisionan.
    """
    original = unicodedata.normalize("NFC", w).strip().lower()
    sin_bordes = original.strip(_PUNTUACION_COSMETICA)
    return sin_bordes or original


def _mismo_token(a: _Palabra, b: _Palabra) -> bool:
    return _norm(a.texto) == _norm(b.texto)


def _es_prefijo(prefijo: List[_Palabra], palabras: List[_Palabra]) -> bool:
    return len(prefijo) <= len(palabras) and all(
        _mismo_token(esperada, actual)
        for esperada, actual in zip(prefijo, palabras)
    )


def _largo_prefijo_comun(a: List[_Palabra], b: List[_Palabra]) -> int:
    k = 0
    while k < len(a) and k < len(b) and _mismo_token(a[k], b[k]):
        k += 1
    return k


def _fin_subsecuencia(aguja: List[_Palabra], pajar: List[_Palabra]):
    """Índice posterior al match ordenado de ``aguja``; None si no existe."""
    if not aguja:
        return 0
    i = 0
    for j, palabra in enumerate(pajar):
        if _mismo_token(aguja[i], palabra):
            i += 1
            if i == len(aguja):
                return j + 1
    return None


class TranscriptorStreaming:
    """LocalAgreement-2 con una frontera committed explícita y append-only.

    ``palabras_confirmadas`` representa texto ya emitido respaldado por el
    buffer actual. Nunca se usa su longitud sobre una hipótesis nueva sin
    demostrar primero que esa hipótesis conserva el mismo prefijo lexical.
    """

    def __init__(self, motor: MotorWhisper, sample_rate: int = 16000,
                 interval_s: float = 1.0, trim_s: float = 12.0):
        self.motor = motor
        self.sr = sample_rate
        self.interval_s = interval_s
        self.trim_s = trim_s
        self.divergencias = 0
        self.decodificaciones = 0
        self.fallos = 0
        self.last_error_type: Optional[str] = None
        self.reiniciar()

    def reiniciar(self):
        self.buffer = np.zeros(0, dtype=np.float32)
        self.palabras_prev: List[_Palabra] = []
        self.palabras_confirmadas: List[_Palabra] = []
        self._hipotesis_alineada = True
        self._ultimo_len_decodificado = 0
        self._hubo_trim = False

    def aceptar_audio(self, trozo: np.ndarray):
        self.buffer = np.concatenate([self.buffer, trozo.astype(np.float32)])

    def _decodificar_palabras(self, audio: np.ndarray) -> List[_Palabra]:
        try:
            segments = self.motor.decodificar(
                audio, word_timestamps=True, beam_size=1)
            palabras: List[_Palabra] = []
            for seg in segments:
                for w in (seg.words or []):
                    palabras.append(_Palabra(
                        texto=w.word,
                        fin=w.end,
                        inicio=getattr(w, "start", None),
                    ))
        except Exception as exc:
            self.fallos += 1
            self.last_error_type = type(exc).__name__
            raise
        self.decodificaciones += 1
        return palabras

    def procesar(self) -> str:
        """Decodifica si llegó suficiente audio nuevo; devuelve el texto recién
        confirmado ('' si no hay)."""
        muestras_nuevas = self.buffer.size - self._ultimo_len_decodificado
        if muestras_nuevas < int(self.interval_s * self.sr) or self.buffer.size < self.sr // 2:
            return ""
        self._ultimo_len_decodificado = self.buffer.size

        palabras = self._decodificar_palabras(self.buffer)

        actual_alineada = _es_prefijo(self.palabras_confirmadas, palabras)
        previa_alineada = _es_prefijo(
            self.palabras_confirmadas, self.palabras_prev)
        nuevas = []
        if actual_alineada and previa_alineada:
            k = _largo_prefijo_comun(palabras, self.palabras_prev)
            frontera = len(self.palabras_confirmadas)
            if k > frontera:
                nuevas = palabras[frontera:k]
                self.palabras_confirmadas.extend(nuevas)
        elif not actual_alineada:
            # Contador operativo únicamente; no incluye texto ni genera spam.
            self.divergencias += 1

        self.palabras_prev = palabras
        self._hipotesis_alineada = actual_alineada
        salida = "".join(p.texto for p in nuevas)

        self._recortar_si_seguro(palabras, actual_alineada)

        return salida

    def _recortar_si_seguro(self, palabras: List[_Palabra], alineada: bool):
        """Recorta sólo hasta un timestamp actualmente alineado y válido."""
        if (self.buffer.size <= int(self.trim_s * self.sr)
                or not alineada or not self.palabras_confirmadas):
            return
        n = len(self.palabras_confirmadas)
        if len(palabras) < n:
            return
        try:
            fines = [float(p.fin) for p in palabras[:n]]
        except (TypeError, ValueError):
            return
        if (any(not np.isfinite(fin) or fin <= 0 for fin in fines)
                or any(a > b for a, b in zip(fines, fines[1:]))):
            return
        corte = int(fines[-1] * self.sr)
        if corte <= 0 or corte > self.buffer.size:
            return
        if len(palabras) > n:
            inicio_pendiente = palabras[n].inicio
            try:
                inicio_pendiente = float(inicio_pendiente)
            except (TypeError, ValueError):
                return
            if (not np.isfinite(inicio_pendiente)
                    or inicio_pendiente < fines[-1]):
                return

        # El audio posterior al fin de la última palabra committed queda
        # intacto. Al cambiar el origen temporal se exige agreement fresco.
        self.buffer = self.buffer[corte:]
        self._ultimo_len_decodificado = self.buffer.size
        self.palabras_prev = []
        self.palabras_confirmadas = []
        self._hipotesis_alineada = True
        self._hubo_trim = True

    def hipotesis_pendiente(self) -> str:
        """Texto decodificado pero aún no confirmado por LocalAgreement.
        Solo lectura; útil como vista previa (p. ej. teleprompter)."""
        if not self._hipotesis_alineada:
            return ""
        frontera = len(self.palabras_confirmadas)
        return "".join(p.texto for p in self.palabras_prev[frontera:]).strip()

    def finalizar(self) -> str:
        """Vaciado: decodifica lo que queda y devuelve el texto más allá de
        las palabras ya confirmadas."""
        if not self.buffer.size or (self.buffer.size < 1600 and not self._hubo_trim):
            self.reiniciar()
            return ""
        try:
            palabras = self._decodificar_palabras(self.buffer)
            cola = self._resto_final(palabras)
            salida = "".join(p.texto for p in cola)
        except Exception:
            salida = ""
        self.reiniciar()
        return salida

    def _resto_final(self, palabras: List[_Palabra]) -> List[_Palabra]:
        """Obtiene sólo el sufijo que todavía puede agregarse sin retractar.

        Si el committed ya no es prefijo, busca primero todo el committed y
        luego sus sufijos, siempre en orden. Lo anterior al ancla no puede
        insertarse en un sink append-only y se congela deliberadamente.
        """
        if not self.palabras_confirmadas:
            return palabras
        if _es_prefijo(self.palabras_confirmadas, palabras):
            return palabras[len(self.palabras_confirmadas):]

        self.divergencias += 1
        for inicio in range(len(self.palabras_confirmadas)):
            fin = _fin_subsecuencia(
                self.palabras_confirmadas[inicio:], palabras)
            if fin is not None:
                return palabras[fin:]
        return []
