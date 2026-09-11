"""Regresiones del vaciado de tails streaming y su frontera append-only."""

import unittest

import numpy as np

from parlar.motor_transcripcion import TranscriptorStreaming, _Palabra
from tests.test_streaming_alignment import MotorGuionado


def crear(hipotesis, *, sample_rate=16000):
    motor = MotorGuionado(hipotesis)
    streaming = TranscriptorStreaming(
        motor, sample_rate=sample_rate, interval_s=0.1, trim_s=1.5)
    return streaming, motor


class PruebasUmbralFinal(unittest.TestCase):
    def test_tails_1599_1600_1601_sin_trim(self):
        casos = (
            (1599, 0, ""),
            (1600, 1, " tail"),
            (1601, 1, " tail"),
        )
        for muestras, llamadas, esperado in casos:
            with self.subTest(muestras=muestras):
                st, motor = crear([["tail"]])
                st.buffer = np.zeros(muestras, dtype=np.float32)
                self.assertEqual(st.finalizar(), esperado)
                self.assertEqual(motor.llamadas, llamadas)

    def test_tails_1599_1600_1601_con_trim(self):
        for muestras in (1599, 1600, 1601):
            with self.subTest(muestras=muestras):
                st, motor = crear([["tail"]])
                st.buffer = np.zeros(muestras, dtype=np.float32)
                st._hubo_trim = True
                self.assertEqual(st.finalizar(), " tail")
                self.assertEqual(motor.llamadas, 1)

    def test_final_vacio_no_decodifica_aunque_haya_trim(self):
        st, motor = crear([["inventado"]])
        st._hubo_trim = True
        self.assertEqual(st.finalizar(), "")
        self.assertEqual(motor.llamadas, 0)

    def test_buffer_dos_segundos_trim_193_decodifica_tail_1120(self):
        st, motor = crear([["tail"]])
        st.buffer = np.zeros(32000, dtype=np.float32)
        confirmada = _Palabra(" previo", 1.93, 0.0)
        st.palabras_confirmadas = [confirmada]
        st._recortar_si_seguro([confirmada], True)

        self.assertEqual(st.buffer.size, 1120)
        self.assertTrue(st._hubo_trim)
        self.assertEqual(st.finalizar(), " tail")
        self.assertEqual(motor.llamadas, 1)


class PruebasAgreementFinal(unittest.TestCase):
    def finalizar(self, confirmadas, final):
        st, _ = crear([final])
        st.buffer = np.zeros(1600, dtype=np.float32)
        st.palabras_confirmadas = [
            _Palabra(" " + texto, 0.1 * (i + 1))
            for i, texto in enumerate(confirmadas)
        ]
        return st.finalizar()

    def test_final_igual_al_prefijo_no_duplica(self):
        self.assertEqual(self.finalizar(["hola"], ["hola"]), "")

    def test_final_agrega_solo_token_nuevo(self):
        self.assertEqual(
            self.finalizar(["hola"], ["hola", "mundo"]), " mundo")

    def test_final_contradictorio_no_retracta_ni_inventa(self):
        self.assertEqual(self.finalizar(["enciende"], ["apaga"]), "")

    def test_repeticion_legitima_conserva_segunda_instancia(self):
        self.assertEqual(
            self.finalizar(["very"], ["very", "very"]), " very")


if __name__ == "__main__":
    unittest.main(verbosity=2)
