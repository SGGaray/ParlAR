import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "record_stt_corpus.py"

spec = importlib.util.spec_from_file_location(
    "record_stt_corpus",
    SCRIPT,
)
modulo = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(modulo)


class PruebasDuracionCorpus(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            preroll_ms=300,
            silence_ms=600,
        )

    def test_referencia_de_12_palabras_exige_2_4_segundos(self):
        texto = (
            "La evidencia de la prueba quedó vinculada "
            "al cambio correspondiente en GitHub."
        )

        minima = modulo.duracion_minima_referencia(
            texto,
            self.cfg,
        )

        self.assertAlmostEqual(minima, 2.4)

    def test_captura_invalida_detectada_seria_demasiado_corta(self):
        texto = (
            "La evidencia de la prueba quedó vinculada "
            "al cambio correspondiente en GitHub."
        )

        minima = modulo.duracion_minima_referencia(
            texto,
            self.cfg,
        )

        self.assertLess(1.04, minima)

    def test_frase_normal_real_no_quedaria_rechazada(self):
        texto = (
            "Hoy tengo que revisar unas cosas "
            "antes de salir de casa."
        )

        minima = modulo.duracion_minima_referencia(
            texto,
            self.cfg,
        )

        self.assertGreater(3.70, minima)


if __name__ == "__main__":
    unittest.main()
