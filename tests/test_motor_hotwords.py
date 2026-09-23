import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from parlar.motor_transcripcion import MotorWhisper


class ModeloFalso:
    ultima_instancia = None

    def __init__(self, model_size, *, device, compute_type):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.llamadas = []
        type(self).ultima_instancia = self

    def transcribe(self, audio, **kwargs):
        self.llamadas.append(kwargs)
        return iter(()), object()


class PruebasHotwordsMotor(unittest.TestCase):
    def crear_motor(self, hotwords="", initial_prompt=""):
        modulo = types.SimpleNamespace(WhisperModel=ModeloFalso)

        with patch.dict(sys.modules, {"faster_whisper": modulo}):
            motor = MotorWhisper(
                "small",
                device="cpu",
                compute_type="int8",
                language="es",
                beam_size=5,
                hotwords=hotwords,
                initial_prompt=initial_prompt,
            )

        return motor

    def test_sin_hotwords_pasa_none_al_backend(self):
        motor = self.crear_motor()

        motor.decodificar(np.zeros(1600, dtype=np.float32))

        llamada = ModeloFalso.ultima_instancia.llamadas[-1]
        self.assertIsNone(motor.hotwords)
        self.assertIsNone(llamada["hotwords"])

    def test_hotwords_se_limpian_y_llegan_intactas_al_backend(self):
        motor = self.crear_motor(
            "  COBIT Govern CUDA cuDNN OWASP  "
        )

        motor.decodificar(np.zeros(1600, dtype=np.float32))

        llamada = ModeloFalso.ultima_instancia.llamadas[-1]
        esperado = "COBIT Govern CUDA cuDNN OWASP"

        self.assertEqual(motor.hotwords, esperado)
        self.assertEqual(llamada["hotwords"], esperado)

    def test_hotwords_no_cambian_los_parametros_de_decodificacion(self):
        motor = self.crear_motor("COBIT")

        motor.decodificar(
            np.zeros(1600, dtype=np.float32),
            word_timestamps=True,
            beam_size=3,
        )

        llamada = ModeloFalso.ultima_instancia.llamadas[-1]

        self.assertEqual(llamada["language"], "es")
        self.assertEqual(llamada["beam_size"], 3)
        self.assertTrue(llamada["word_timestamps"])
        self.assertFalse(llamada["vad_filter"])
        self.assertFalse(llamada["condition_on_previous_text"])
        self.assertEqual(llamada["hotwords"], "COBIT")



    def test_sin_initial_prompt_pasa_none_al_backend(self):
        motor = self.crear_motor()

        motor.decodificar(np.zeros(1600, dtype=np.float32))

        llamada = ModeloFalso.ultima_instancia.llamadas[-1]
        self.assertIsNone(motor.initial_prompt)
        self.assertIsNone(llamada["initial_prompt"])

    def test_initial_prompt_se_limpia_y_llega_al_backend(self):
        motor = self.crear_motor(
            initial_prompt="  COBIT. Govern. CUDA. cuDNN.  "
        )

        motor.decodificar(np.zeros(1600, dtype=np.float32))

        llamada = ModeloFalso.ultima_instancia.llamadas[-1]
        esperado = "COBIT. Govern. CUDA. cuDNN."

        self.assertEqual(motor.initial_prompt, esperado)
        self.assertEqual(llamada["initial_prompt"], esperado)

    def test_hotwords_e_initial_prompt_pueden_coexistir(self):
        motor = self.crear_motor(
            hotwords="COBIT CUDA",
            initial_prompt="Contexto técnico: COBIT y CUDA.",
        )

        motor.decodificar(np.zeros(1600, dtype=np.float32))

        llamada = ModeloFalso.ultima_instancia.llamadas[-1]

        self.assertEqual(llamada["hotwords"], "COBIT CUDA")
        self.assertEqual(
            llamada["initial_prompt"],
            "Contexto técnico: COBIT y CUDA.",
        )
if __name__ == "__main__":
    unittest.main()
