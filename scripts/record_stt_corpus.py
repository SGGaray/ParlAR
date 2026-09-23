#!/usr/bin/env python3
"""Grabador guiado del corpus STT usando la segmentación real de ParlAR."""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from parlar.capturador_audio import CapturadorMic, Segmentador, crear_vad
from parlar.config import Config


SAMPLE_WIDTH = 2


def cargar_jsonl(ruta: Path) -> list[dict]:
    items = []

    for numero, linea in enumerate(
        ruta.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not linea.strip():
            continue

        try:
            item = json.loads(linea)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{ruta}:{numero}: JSON inválido: {exc}"
            ) from exc

        for campo in ("id", "text"):
            if not item.get(campo):
                raise ValueError(
                    f"{ruta}:{numero}: falta campo {campo!r}"
                )

        items.append(item)

    if not items:
        raise ValueError(f"{ruta}: no contiene prompts")

    return items


def cargar_corpus(ruta: Path) -> list[dict]:
    if not ruta.exists():
        return []
    return cargar_jsonl(ruta)


def guardar_corpus(ruta: Path, entradas: list[dict]) -> None:
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")

    with temporal.open("w", encoding="utf-8") as archivo:
        for entrada in entradas:
            archivo.write(
                json.dumps(entrada, ensure_ascii=False) + "\n"
            )

    temporal.replace(ruta)


def configurar_dispositivo(valor: str | None) -> None:
    if valor is None:
        return

    import sounddevice as sd

    try:
        entrada = int(valor)
    except ValueError:
        entrada = valor

    actual = sd.default.device
    try:
        salida = actual[1]
    except (TypeError, IndexError):
        salida = None

    sd.default.device = (entrada, salida)


def crear_segmentador_produccion(cfg: Config) -> Segmentador:
    vad = crear_vad(
        cfg.vad_aggressiveness,
        cfg.sample_rate,
    )

    return Segmentador(
        cfg.sample_rate,
        cfg.frame_ms,
        vad,
        cfg.silence_ms,
        cfg.preroll_ms,
        cfg.max_utterance_s,
        cfg.min_speech_ms,
    )


def grabar_frase(
    cfg: Config,
    *,
    timeout_s: float,
) -> tuple[np.ndarray, dict]:
    frame_samples = cfg.sample_rate * cfg.frame_ms // 1000

    mic = CapturadorMic(
        cfg.sample_rate,
        frame_samples,
    )

    segmentador = crear_segmentador_produccion(cfg)

    generacion = 1
    inicio = time.monotonic()
    voz_detectada = False
    audio_final = None
    metricas = None

    if not mic.iniciar(generacion):
        raise RuntimeError("no se pudo iniciar la captura")

    try:
        while True:
            if time.monotonic() - inicio > timeout_s:
                raise TimeoutError(
                    f"no se completó una frase en {timeout_s:.1f}s"
                )

            frame = mic.leer_frame(timeout=0.1)
            if frame is None:
                continue

            if frame.discontinuidad_antes:
                raise RuntimeError(
                    "la captura tuvo una discontinuidad; repetí la toma"
                )

            for evento in segmentador.procesar(frame.audio):
                if evento.tipo == "inicio_voz" and not voz_detectada:
                    voz_detectada = True
                    print(
                        "[audio] voz detectada",
                        flush=True,
                    )

                if evento.tipo != "frase":
                    continue

                if evento.audio is None or evento.audio.size == 0:
                    raise RuntimeError(
                        "el segmentador produjo una frase vacía"
                    )

                estado = mic.estado_captura()

                if estado.device_overflows:
                    raise RuntimeError(
                        "la captura tuvo overflow del dispositivo; "
                        "repetí la toma"
                    )

                if estado.frames_descartados:
                    raise RuntimeError(
                        "la cola de audio descartó frames; repetí la toma"
                    )

                if estado.discontinuidades:
                    raise RuntimeError(
                        "la captura registró discontinuidades; "
                        "repetí la toma"
                    )

                if segmentador.vad_failures:
                    raise RuntimeError(
                        "el VAD falló durante la toma; repetí la toma"
                    )

                audio_final = evento.audio.copy()
                metricas = {
                    "vad_backend": type(segmentador.vad).__name__,
                    "vad_failures": segmentador.vad_failures,
                    "device_overflows": estado.device_overflows,
                    "frames_dropped": estado.frames_descartados,
                    "discontinuities": estado.discontinuidades,
                }
                break

            if audio_final is not None:
                break

    finally:
        mic.detener(vaciar=True)

    assert audio_final is not None
    assert metricas is not None

    return audio_final, metricas


def escribir_wav(
    ruta: Path,
    audio: np.ndarray,
    sample_rate: int,
) -> float:
    if audio.ndim != 1 or audio.size == 0:
        raise ValueError("el audio debe ser mono y no vacío")

    pcm = np.rint(audio.astype(np.float64) * 32768.0)
    pcm = np.clip(pcm, -32768, 32767).astype("<i2")

    ruta.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(ruta), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())

    return len(pcm) / sample_rate


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Graba el corpus STT usando el mismo VAD y "
            "segmentador que ParlAR."
        )
    )

    parser.add_argument(
        "--prompts",
        type=Path,
        default=Path("benchmarks/stt/prompts.es-ar.jsonl"),
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("benchmarks/stt/corpus.jsonl"),
    )
    parser.add_argument(
        "--audio-dir",
        type=Path,
        default=Path("benchmarks/stt/audio"),
    )
    parser.add_argument("--device")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument(
        "--redo",
        metavar="ID",
        help="vuelve a grabar únicamente este ID",
    )

    args = parser.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0

    if args.timeout <= 0:
        parser.error("--timeout debe ser mayor que cero")

    prompts = cargar_jsonl(args.prompts)

    if args.redo:
        prompts = [
            prompt
            for prompt in prompts
            if prompt["id"] == args.redo
        ]
        if not prompts:
            parser.error(f"ID desconocido: {args.redo}")

    if args.limit is not None:
        if args.limit <= 0:
            parser.error("--limit debe ser mayor que cero")
        prompts = prompts[:args.limit]

    corpus = cargar_corpus(args.corpus)
    por_id = {
        entrada["id"]: entrada
        for entrada in corpus
    }

    if args.redo:
        por_id.pop(args.redo, None)

    pendientes = [
        prompt
        for prompt in prompts
        if prompt["id"] not in por_id
    ]

    if args.dry_run:
        print(f"prompts seleccionados: {len(prompts)}")
        print(
            f"ya grabados: "
            f"{len(prompts) - len(pendientes)}"
        )
        print(f"pendientes: {len(pendientes)}")

        for prompt in pendientes:
            print(
                f"{prompt['id']}: {prompt['text']}"
            )

        return 0

    if not pendientes:
        print("No hay grabaciones pendientes.")
        return 0

    cfg = Config.load()
    configurar_dispositivo(args.device)

    print(
        "[audio] "
        f"{cfg.sample_rate} Hz · "
        f"{cfg.frame_ms} ms/frame · "
        f"VAD={cfg.vad_aggressiveness} · "
        f"preroll={cfg.preroll_ms} ms · "
        f"silence={cfg.silence_ms} ms · "
        f"min_speech={cfg.min_speech_ms} ms"
    )

    total = len(pendientes)

    for indice, prompt in enumerate(
        pendientes,
        start=1,
    ):
        ident = prompt["id"]
        ruta_audio = args.audio_dir / f"{ident}.wav"

        print()
        print("=" * 72)
        print(f"[{indice:02d}/{total:02d}] {ident}")
        print()

        if prompt.get("instruction"):
            print(prompt["instruction"])
            print()

        print(f'Decí: "{prompt["text"]}"')
        print()

        respuesta = input(
            "ENTER para escuchar, "
            "'s' para saltar o "
            "'q' para salir: "
        ).strip().lower()

        if respuesta == "q":
            print("Grabación interrumpida.")
            return 0

        if respuesta == "s":
            print("Saltado.")
            continue

        print(
            "ESCUCHANDO — empezá a hablar; "
            f"se detiene tras {cfg.silence_ms} ms "
            "de silencio...",
            flush=True,
        )

        try:
            audio, metricas = grabar_frase(
                cfg,
                timeout_s=args.timeout,
            )
        except (RuntimeError, TimeoutError, OSError) as exc:
            print(
                f"[audio] toma rechazada: {exc}",
                file=sys.stderr,
            )
            print("No se guardó ningún cambio.")
            return 1

        duracion = escribir_wav(
            ruta_audio,
            audio,
            cfg.sample_rate,
        )

        entrada = {
            "id": ident,
            "audio": f"audio/{ident}.wav",
            "text": prompt["text"],
            "language": prompt.get(
                "language",
                "es",
            ),
            "tags": prompt.get("tags", []),
        }

        corpus = [
            existente
            for existente in corpus
            if existente["id"] != ident
        ]
        corpus.append(entrada)
        guardar_corpus(args.corpus, corpus)

        print(
            f"Guardado: {ruta_audio} "
            f"({duracion:.2f}s)"
        )
        print(
            "[audio] "
            f"vad={metricas['vad_backend']} "
            f"overflows={metricas['device_overflows']} "
            f"drops={metricas['frames_dropped']} "
            f"discontinuidades={metricas['discontinuities']}"
        )

    print()
    print("Corpus completado.")
    print(f"Manifest: {args.corpus}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
