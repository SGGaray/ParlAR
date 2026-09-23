#!/usr/bin/env python3

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

from parlar.config import Config


def cargar_jsonl(ruta: Path) -> list[dict]:
    items = []

    for numero, linea in enumerate(
        ruta.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not linea.strip():
            continue

        item = json.loads(linea)

        for campo in ("id", "text"):
            if not item.get(campo):
                raise ValueError(
                    f"{ruta}:{numero}: falta {campo!r}"
                )

        items.append(item)

    return items


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


def grabar_raw(
    cfg: Config,
    duracion_s: float,
    silencio_inicial_s: float,
) -> tuple[bytes, bool]:
    import sounddevice as sd

    total = int(round(
        duracion_s * cfg.sample_rate
    ))
    previo = int(round(
        silencio_inicial_s * cfg.sample_rate
    ))

    bloque = cfg.frame_samples
    capturado = 0
    aviso = False
    overflow = False
    partes = []

    with sd.RawInputStream(
        samplerate=cfg.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=bloque,
    ) as stream:
        while capturado < total:
            restante = total - capturado
            cantidad = min(bloque, restante)

            datos, hubo_overflow = stream.read(cantidad)

            partes.append(bytes(datos))
            capturado += cantidad
            overflow = overflow or bool(hubo_overflow)

            if not aviso and capturado >= previo:
                print(
                    ">>> HABLÁ AHORA <<<",
                    flush=True,
                )
                aviso = True

    return b"".join(partes), overflow


def guardar_wav(
    ruta: Path,
    pcm: bytes,
    sample_rate: int,
) -> None:
    ruta.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with wave.open(str(ruta), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def guardar_manifest(
    ruta: Path,
    entradas: list[dict],
) -> None:
    temporal = ruta.with_suffix(
        ruta.suffix + ".tmp"
    )

    with temporal.open(
        "w",
        encoding="utf-8",
    ) as archivo:
        for entrada in entradas:
            archivo.write(
                json.dumps(
                    entrada,
                    ensure_ascii=False,
                )
                + "\n"
            )

    temporal.replace(ruta)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompts",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=12.0,
    )
    parser.add_argument(
        "--lead-silence",
        type=float,
        default=1.0,
    )
    parser.add_argument("--device")
    args = parser.parse_args()

    if args.duration <= 0:
        raise SystemExit(
            "--duration debe ser mayor que cero"
        )

    if not 0 <= args.lead_silence < args.duration:
        raise SystemExit(
            "--lead-silence fuera de rango"
        )

    cfg = Config.load()
    configurar_dispositivo(args.device)

    prompts = cargar_jsonl(args.prompts)

    if args.manifest.exists():
        corpus = cargar_jsonl(
            args.manifest
        )
    else:
        corpus = []

    existentes = {
        item["id"]
        for item in corpus
    }

    pendientes = [
        item
        for item in prompts
        if item["id"] not in existentes
    ]

    print(
        f"pendientes: {len(pendientes)}"
    )

    for indice, prompt in enumerate(
        pendientes,
        start=1,
    ):
        ident = prompt["id"]

        print()
        print(
            f"[{indice}/{len(pendientes)}] "
            f"{ident}"
        )
        print()
        print(
            f'Decí: "{prompt["text"]}"'
        )
        print()
        print(
            "La grabación dura "
            f"{args.duration:.1f}s."
        )
        print(
            "Esperá a que aparezca "
            "'HABLÁ AHORA'."
        )
        print(
            "Cuando termines, quedate "
            "en silencio hasta que cierre."
        )

        respuesta = input(
            "ENTER para grabar o "
            "'q' para salir: "
        ).strip().lower()

        if respuesta == "q":
            return 0

        print(
            "Grabando silencio inicial...",
            flush=True,
        )

        pcm, overflow = grabar_raw(
            cfg,
            args.duration,
            args.lead_silence,
        )

        if overflow:
            print(
                "[audio] overflow detectado; "
                "toma rechazada",
                file=sys.stderr,
            )
            return 1

        ruta_audio = (
            args.manifest.parent
            / "audio"
            / f"{ident}.wav"
        )

        guardar_wav(
            ruta_audio,
            pcm,
            cfg.sample_rate,
        )

        corpus.append({
            "id": ident,
            "audio": f"audio/{ident}.wav",
            "text": prompt["text"],
            "tags": prompt.get("tags", []),
        })

        args.manifest.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        guardar_manifest(
            args.manifest,
            corpus,
        )

        print(
            f"Guardado: {ruta_audio}"
        )

    print()
    print("Corpus RAW completado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
