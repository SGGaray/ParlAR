#!/usr/bin/env python3
"""Benchmark reproducible de STT para ParlAR.

Mide la capa de transcripción de producción sobre WAVs conocidos.
No captura micrófono y no modifica configuración de ParlAR.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from parlar.motor_transcripcion import MotorWhisper, TranscriptorFrase


_SAMPLE_RATE = 16000


def normalizar_texto(texto: str) -> str:
    texto = unicodedata.normalize("NFC", texto).casefold()
    texto = re.sub(r"[^\wáéíóúüñ]+", " ", texto, flags=re.UNICODE)
    return " ".join(texto.split())


def distancia_levenshtein(a: list[str], b: list[str]) -> int:
    if len(a) < len(b):
        a, b = b, a

    anterior = list(range(len(b) + 1))

    for i, token_a in enumerate(a, start=1):
        actual = [i]
        for j, token_b in enumerate(b, start=1):
            costo = 0 if token_a == token_b else 1
            actual.append(
                min(
                    actual[-1] + 1,
                    anterior[j] + 1,
                    anterior[j - 1] + costo,
                )
            )
        anterior = actual

    return anterior[-1]


def calcular_wer(referencia: str, hipotesis: str) -> float:
    ref = normalizar_texto(referencia).split()
    hyp = normalizar_texto(hipotesis).split()

    if not ref:
        return 0.0 if not hyp else 1.0

    return distancia_levenshtein(ref, hyp) / len(ref)


def cargar_wav(ruta: Path) -> np.ndarray:
    with wave.open(str(ruta), "rb") as wav:
        canales = wav.getnchannels()
        ancho = wav.getsampwidth()
        frecuencia = wav.getframerate()

        if canales != 1:
            raise ValueError(f"{ruta}: se esperaba mono; canales={canales}")
        if ancho != 2:
            raise ValueError(
                f"{ruta}: se esperaba PCM int16; sample_width={ancho}"
            )
        if frecuencia != _SAMPLE_RATE:
            raise ValueError(
                f"{ruta}: se esperaban {_SAMPLE_RATE} Hz; encontrados {frecuencia}"
            )

        crudo = wav.readframes(wav.getnframes())

    return np.frombuffer(crudo, dtype="<i2").astype(np.float32) / 32768.0


def cargar_manifest(ruta: Path) -> list[dict]:
    casos = []

    for numero, linea in enumerate(
        ruta.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not linea.strip():
            continue

        try:
            caso = json.loads(linea)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{ruta}:{numero}: JSON inválido: {exc}"
            ) from exc

        for campo in ("id", "audio", "text"):
            if not caso.get(campo):
                raise ValueError(
                    f"{ruta}:{numero}: falta campo requerido {campo!r}"
                )

        casos.append(caso)

    if not casos:
        raise ValueError(f"{ruta}: corpus vacío")

    return casos


def percentil(valores: list[float], q: float) -> float:
    if not valores:
        return 0.0

    ordenados = sorted(valores)
    indice = round((len(ordenados) - 1) * q)
    return ordenados[indice]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--language", default="es")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    casos = cargar_manifest(args.manifest)

    motor = MotorWhisper(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        beam_size=args.beam_size,
    )
    transcriptor = TranscriptorFrase(motor)

    # Warm-up no medido: la primera inferencia CUDA puede inicializar
    # kernels y no debe contaminar la latencia de la primera muestra.
    primer_audio = cargar_wav(args.manifest.parent / casos[0]["audio"])
    transcriptor.transcribir(primer_audio)

    resultados = []
    total_palabras = 0
    total_errores = 0

    for caso in casos:
        ruta_audio = args.manifest.parent / caso["audio"]
        audio = cargar_wav(ruta_audio)

        duracion_s = len(audio) / _SAMPLE_RATE

        t0 = time.perf_counter()
        hipotesis = transcriptor.transcribir(audio)
        latencia_s = time.perf_counter() - t0

        ref_tokens = normalizar_texto(caso["text"]).split()
        hyp_tokens = normalizar_texto(hipotesis).split()
        errores = distancia_levenshtein(ref_tokens, hyp_tokens)

        total_palabras += len(ref_tokens)
        total_errores += errores

        wer = errores / len(ref_tokens) if ref_tokens else (
            0.0 if not hyp_tokens else 1.0
        )

        rtf = latencia_s / duracion_s if duracion_s else 0.0

        resultado = {
            "id": caso["id"],
            "audio": caso["audio"],
            "tags": caso.get("tags", []),
            "reference": caso["text"],
            "hypothesis": hipotesis,
            "words": len(ref_tokens),
            "errors": errores,
            "wer": wer,
            "duration_s": duracion_s,
            "latency_s": latencia_s,
            "rtf": rtf,
        }
        resultados.append(resultado)

        print(
            f"{caso['id']}: "
            f"WER={wer:.3f} "
            f"lat={latencia_s:.3f}s "
            f"RTF={rtf:.3f}"
        )
        print(f"  REF: {caso['text']}")
        print(f"  HYP: {hipotesis}")

    latencias = [r["latency_s"] for r in resultados]
    rtfs = [r["rtf"] for r in resultados]

    resumen = {
        "model": args.model,
        "device": args.device,
        "compute_type": args.compute_type,
        "beam_size": args.beam_size,
        "language": args.language,
        "samples": len(resultados),
        "words": total_palabras,
        "errors": total_errores,
        "wer": total_errores / total_palabras if total_palabras else 0.0,
        "latency_mean_s": statistics.mean(latencias),
        "latency_p50_s": percentil(latencias, 0.50),
        "latency_p95_s": percentil(latencias, 0.95),
        "rtf_mean": statistics.mean(rtfs),
    }

    print()
    print("=== RESUMEN ===")
    print(f"muestras:     {resumen['samples']}")
    print(f"palabras:     {resumen['words']}")
    print(f"errores:      {resumen['errors']}")
    print(f"WER:          {resumen['wer']:.3f}")
    print(f"lat media:    {resumen['latency_mean_s']:.3f}s")
    print(f"lat p50:      {resumen['latency_p50_s']:.3f}s")
    print(f"lat p95:      {resumen['latency_p95_s']:.3f}s")
    print(f"RTF medio:    {resumen['rtf_mean']:.3f}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "summary": resumen,
                    "results": resultados,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"resultado:    {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
