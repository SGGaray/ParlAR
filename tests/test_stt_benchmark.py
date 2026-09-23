import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_stt.py"

spec = importlib.util.spec_from_file_location("benchmark_stt", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(benchmark)


class PruebasBenchmarkSTT(unittest.TestCase):
    def test_normalizacion_conserva_palabras_espanolas(self):
        self.assertEqual(
            benchmark.normalizar_texto("¡Cómo ESTÁS, Sebastián!"),
            "cómo estás sebastián",
        )

    def test_wer_identico_es_cero(self):
        self.assertEqual(
            benchmark.calcular_wer(
                "Hola, ¿cómo estás?",
                "hola cómo estás",
            ),
            0.0,
        )

    def test_wer_sustitucion(self):
        self.assertAlmostEqual(
            benchmark.calcular_wer(
                "uno dos tres",
                "uno cuatro tres",
            ),
            1 / 3,
        )

    def test_cargar_wav_pcm16_mono_16k(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / "audio.wav"
            muestras = np.array([0, 32767, -32768], dtype="<i2")

            with wave.open(str(ruta), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(muestras.tobytes())

            audio = benchmark.cargar_wav(ruta)

        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.shape, (3,))
        self.assertAlmostEqual(float(audio[0]), 0.0)
        self.assertGreater(float(audio[1]), 0.99)
        self.assertEqual(float(audio[2]), -1.0)

    def test_manifest_requiere_campos(self):
        with tempfile.TemporaryDirectory() as temporal:
            ruta = Path(temporal) / "corpus.jsonl"
            ruta.write_text(
                json.dumps({"id": "x", "audio": "x.wav"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "text"):
                benchmark.cargar_manifest(ruta)


if __name__ == "__main__":
    unittest.main()
