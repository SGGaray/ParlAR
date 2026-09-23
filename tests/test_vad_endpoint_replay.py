import importlib.util
import unittest
from pathlib import Path

from parlar.config import Config


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyze_vad_endpoint.py"

spec = importlib.util.spec_from_file_location(
    "analyze_vad_endpoint",
    SCRIPT,
)
modulo = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(modulo)


class VADGuion:
    def __init__(self, guion):
        self.guion = iter(guion)

    def is_speech(self, _frame, _sample_rate):
        return next(self.guion, False)


def fabrica_vad(guion):
    def crear(_agresividad, _sample_rate):
        return VADGuion(guion)

    return crear


def pcm_para_frames(cfg, cantidad):
    muestras = (
        cfg.sample_rate
        * cfg.frame_ms
        // 1000
    )
    frame = b"\x00\x00" * muestras
    return frame * cantidad


class PruebasEndpointReplay(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(
            frame_ms=20,
            preroll_ms=40,
            min_speech_ms=40,
            max_utterance_s=30.0,
        )
        self.cfg.validate()

    def replay(self, guion, silencio):
        return modulo.replay_pcm(
            pcm_para_frames(
                self.cfg,
                len(guion),
            ),
            self.cfg,
            silencio,
            vad_factory=fabrica_vad(guion),
        )

    def test_bajar_a_400_gana_200_ms_sin_split_final(self):
        guion = (
            [False] * 2
            + [True] * 6
            + [False] * 30
        )

        base = self.replay(guion, 600)
        rapido = self.replay(guion, 400)

        self.assertEqual(base["frases"], 1)
        self.assertEqual(rapido["frases"], 1)
        self.assertEqual(
            base["primer_cierre_ms"]
            - rapido["primer_cierre_ms"],
            200,
        )
        self.assertEqual(
            rapido[
                "reinicios_despues_primer_cierre"
            ],
            0,
        )

    def test_pausa_interna_separa_400_pero_no_500_ni_600(self):
        guion = (
            [False] * 2
            + [True] * 5
            + [False] * 22
            + [True] * 5
            + [False] * 30
        )

        base = self.replay(guion, 600)
        medio = self.replay(guion, 500)
        rapido = self.replay(guion, 400)

        self.assertEqual(base["frases"], 1)
        self.assertEqual(medio["frases"], 1)

        self.assertEqual(rapido["frases"], 2)
        self.assertEqual(
            rapido[
                "reinicios_despues_primer_cierre"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
