#!/usr/bin/env python3
"""Compara umbrales de cierre VAD sobre WAVs ya capturados."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parlar.capturador_audio import Segmentador, crear_vad
from parlar.config import Config


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

        for campo in ("id", "audio"):
            if not item.get(campo):
                raise ValueError(
                    f"{ruta}:{numero}: falta campo {campo!r}"
                )

        items.append(item)

    return items


def cargar_exclusiones(ruta: Path | None) -> set[str]:
    if ruta is None:
        return set()

    data = json.loads(ruta.read_text(encoding="utf-8"))
    excluidos = data.get("excluded", [])

    if not isinstance(excluidos, list):
        raise ValueError(
            f"{ruta}: 'excluded' debe ser una lista"
        )

    return {
        item["id"]
        for item in excluidos
        if isinstance(item, dict) and item.get("id")
    }


def frame_bytes(cfg: Config) -> int:
    muestras = cfg.sample_rate * cfg.frame_ms // 1000
    return muestras * 2


def leer_wav(ruta: Path, cfg: Config) -> bytes:
    with wave.open(str(ruta), "rb") as wav:
        if wav.getnchannels() != 1:
            raise ValueError(f"{ruta}: debe ser mono")
        if wav.getsampwidth() != 2:
            raise ValueError(f"{ruta}: debe ser PCM16")
        if wav.getframerate() != cfg.sample_rate:
            raise ValueError(
                f"{ruta}: sample rate "
                f"{wav.getframerate()} != {cfg.sample_rate}"
            )
        if wav.getcomptype() != "NONE":
            raise ValueError(f"{ruta}: WAV comprimido no admitido")

        crudo = wav.readframes(wav.getnframes())

    fb = frame_bytes(cfg)
    if len(crudo) % fb:
        raise ValueError(
            f"{ruta}: longitud no alineada a frames "
            f"de {cfg.frame_ms} ms"
        )

    return crudo


def replay_pcm(
    crudo: bytes,
    cfg: Config,
    silence_ms: int,
    vad_factory=crear_vad,
) -> dict:
    if silence_ms <= 0:
        raise ValueError("silence_ms debe ser mayor que cero")

    if silence_ms % cfg.frame_ms:
        raise ValueError(
            f"silence_ms={silence_ms} no es múltiplo "
            f"de frame_ms={cfg.frame_ms}"
        )

    vad = vad_factory(
        cfg.vad_aggressiveness,
        cfg.sample_rate,
    )

    segmentador = Segmentador(
        cfg.sample_rate,
        cfg.frame_ms,
        vad,
        silence_ms,
        cfg.preroll_ms,
        cfg.max_utterance_s,
        cfg.min_speech_ms,
    )

    fb = frame_bytes(cfg)
    inicios = []
    cierres = []
    duraciones = []

    for indice in range(0, len(crudo), fb):
        numero_frame = indice // fb + 1
        tiempo_ms = numero_frame * cfg.frame_ms
        frame = crudo[indice:indice + fb]

        for evento in segmentador.procesar(frame):
            if evento.tipo == "inicio_voz":
                inicios.append(tiempo_ms)

            elif evento.tipo == "frase":
                cierres.append(tiempo_ms)
                duraciones.append(
                    round(
                        len(evento.audio)
                        / cfg.sample_rate
                        * 1000.0,
                        3,
                    )
                )

    primer_cierre = cierres[0] if cierres else None

    reinicios = 0
    if primer_cierre is not None:
        reinicios = sum(
            1
            for inicio in inicios
            if inicio > primer_cierre
        )

    return {
        "silence_ms": silence_ms,
        "frases": len(cierres),
        "inicios_voz_ms": inicios,
        "cierres_ms": cierres,
        "primer_cierre_ms": primer_cierre,
        "duraciones_segmentos_ms": duraciones,
        "reinicios_despues_primer_cierre": reinicios,
        "voz_abierta_al_final": bool(segmentador.en_voz),
        "vad_failures": segmentador.vad_failures,
    }


def percentil_mediana(valores: list[float]) -> float | None:
    if not valores:
        return None
    return float(statistics.median(valores))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay offline del Segmentador de producción "
            "para comparar endpointing VAD."
        )
    )
    parser.add_argument(
        "--manifest",
        action="append",
        type=Path,
        required=True,
        help="manifest JSONL; opción repetible",
    )
    parser.add_argument(
        "--silence-ms",
        nargs="+",
        type=int,
        default=[600, 500, 400],
    )
    parser.add_argument(
        "--baseline-ms",
        type=int,
        default=600,
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
    )
    parser.add_argument(
        "--output",
        type=Path,
    )
    args = parser.parse_args()

    silencios = list(dict.fromkeys(args.silence_ms))

    if args.baseline_ms not in silencios:
        raise SystemExit(
            "--baseline-ms debe estar incluido en --silence-ms"
        )

    cfg = Config()

    for valor in silencios:
        if valor % cfg.frame_ms:
            raise SystemExit(
                f"{valor} ms no es múltiplo "
                f"de frame_ms={cfg.frame_ms}"
            )

    excluidos = cargar_exclusiones(args.exclusions)

    entradas = []
    ids = set()

    for manifest in args.manifest:
        for item in cargar_jsonl(manifest):
            ident = item["id"]

            if ident in ids:
                raise SystemExit(
                    f"id duplicado entre manifests: {ident}"
                )
            ids.add(ident)

            if ident in excluidos:
                continue

            entradas.append(
                (
                    manifest,
                    item,
                )
            )

    resultados = []

    for manifest, item in entradas:
        ident = item["id"]
        ruta_audio = manifest.parent / item["audio"]
        crudo = leer_wav(ruta_audio, cfg)

        por_umbral = {}

        for silencio in silencios:
            por_umbral[str(silencio)] = replay_pcm(
                crudo,
                cfg,
                silencio,
            )

        baseline = por_umbral[str(args.baseline_ms)]

        baseline_valido = (
            baseline["frases"] == 1
            and baseline[
                "reinicios_despues_primer_cierre"
            ] == 0
            and not baseline["voz_abierta_al_final"]
            and baseline["vad_failures"] == 0
        )

        comparaciones = {}

        for silencio in silencios:
            actual = por_umbral[str(silencio)]

            split_potencial = (
                actual["frases"] > baseline["frases"]
                or actual[
                    "reinicios_despues_primer_cierre"
                ]
                > baseline[
                    "reinicios_despues_primer_cierre"
                ]
            )

            ganancia = None
            if (
                baseline["primer_cierre_ms"] is not None
                and actual["primer_cierre_ms"] is not None
            ):
                ganancia = (
                    baseline["primer_cierre_ms"]
                    - actual["primer_cierre_ms"]
                )

            comparaciones[str(silencio)] = {
                "split_potencial": split_potencial,
                "ganancia_primer_cierre_ms": ganancia,
            }

        resultados.append({
            "id": ident,
            "manifest": str(manifest),
            "audio": str(ruta_audio),
            "baseline_valido": baseline_valido,
            "umbrales": por_umbral,
            "comparaciones": comparaciones,
        })

    resumen = {}

    for silencio in silencios:
        clave = str(silencio)

        splits = [
            r
            for r in resultados
            if r["comparaciones"][clave][
                "split_potencial"
            ]
        ]

        ganancias = [
            r["comparaciones"][clave][
                "ganancia_primer_cierre_ms"
            ]
            for r in resultados
            if (
                r["baseline_valido"]
                and not r["comparaciones"][clave][
                    "split_potencial"
                ]
                and r["comparaciones"][clave][
                    "ganancia_primer_cierre_ms"
                ] is not None
            )
        ]

        resumen[clave] = {
            "muestras": len(resultados),
            "splits_potenciales": len(splits),
            "ids_split": [r["id"] for r in splits],
            "ganancia_media_ms": (
                round(statistics.mean(ganancias), 3)
                if ganancias
                else None
            ),
            "ganancia_mediana_ms":
                percentil_mediana(ganancias),
        }

    salida = {
        "config": {
            "sample_rate": cfg.sample_rate,
            "frame_ms": cfg.frame_ms,
            "vad_aggressiveness":
                cfg.vad_aggressiveness,
            "preroll_ms": cfg.preroll_ms,
            "min_speech_ms": cfg.min_speech_ms,
            "max_utterance_s": cfg.max_utterance_s,
            "baseline_ms": args.baseline_ms,
            "silence_ms": silencios,
        },
        "manifests": [
            str(ruta)
            for ruta in args.manifest
        ],
        "excluded_ids": sorted(excluidos),
        "samples": len(resultados),
        "baseline_anomalies": [
            r["id"]
            for r in resultados
            if not r["baseline_valido"]
        ],
        "summary": resumen,
        "results": resultados,
    }

    print(
        f"muestras incluidas: {len(resultados)}"
    )
    print(
        f"excluidas: {len(excluidos & ids)}"
    )
    print(
        "anomalías baseline: "
        f"{len(salida['baseline_anomalies'])}"
    )

    if salida["baseline_anomalies"]:
        print(
            "  "
            + ", ".join(
                salida["baseline_anomalies"]
            )
        )

    for silencio in silencios:
        dato = resumen[str(silencio)]
        print()
        print(f"{silencio} ms")
        print(
            f"  splits potenciales: "
            f"{dato['splits_potenciales']}"
        )
        print(
            f"  ganancia media: "
            f"{dato['ganancia_media_ms']} ms"
        )
        print(
            f"  ganancia mediana: "
            f"{dato['ganancia_mediana_ms']} ms"
        )

        for ident in dato["ids_split"]:
            caso = next(
                r
                for r in resultados
                if r["id"] == ident
            )
            actual = caso["umbrales"][
                str(silencio)
            ]
            print(
                f"    {ident}: "
                f"frases={actual['frases']} "
                f"reinicios="
                f"{actual['reinicios_despues_primer_cierre']} "
                f"cierres={actual['cierres_ms']}"
            )

    if args.output is not None:
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        args.output.write_text(
            json.dumps(
                salida,
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print()
        print(f"resultado: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
