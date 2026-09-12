"""Regresiones de cotas por frame y descarte explícito al detener captura."""

import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App, EstadoApp
from parlar.capturador_audio import CapturadorMic, Segmentador
from parlar.config import Config


class VADSoloVoz:
    def is_speech(self, frame, sample_rate):
        return True


def _frame(frame_samples, valor):
    return np.full(frame_samples, valor, dtype=np.int16).tobytes()


class PruebasCotaSegmentacion(unittest.TestCase):
    def _probar_continuidad(self, frame_ms, preroll_ms, max_s, cantidad):
        frame_samples = 16000 * frame_ms // 1000
        seg = Segmentador(
            16000, frame_ms, VADSoloVoz(), silence_ms=600,
            preroll_ms=preroll_ms, max_utterance_s=max_s,
            min_speech_ms=preroll_ms,
        )
        eventos = []
        originales = []
        for valor in range(1, cantidad + 1):
            crudo = _frame(frame_samples, valor)
            originales.append(np.frombuffer(crudo, dtype=np.int16))
            eventos.extend(seg.procesar(crudo))
        frases = [evento.audio for evento in eventos if evento.tipo == "frase"]
        self.assertGreaterEqual(len(frases), 2)
        max_muestras = int(max_s * 16000)
        for frase in frases:
            self.assertLessEqual(frase.size, max_muestras)
        esperado = np.concatenate(originales).astype(np.float32) / 32768.0
        np.testing.assert_array_equal(np.concatenate(frases), esperado)
        return [frase.size for frase in frases]

    def test_preroll_igual_o_un_frame_menor_respeta_cota_y_continuidad(self):
        for frame_ms in (10, 20, 30):
            max_ms = frame_ms * 10
            for preroll_ms in (max_ms, max_ms - frame_ms):
                with self.subTest(frame_ms=frame_ms, preroll_ms=preroll_ms):
                    tamanos = self._probar_continuidad(
                        frame_ms, preroll_ms, max_ms / 1000.0, 20)
                    self.assertEqual(tamanos, [
                        16000 * max_ms // 1000,
                        16000 * max_ms // 1000,
                    ])

    def test_max_no_alineado_se_redondea_hacia_abajo_a_frames_completos(self):
        tamanos = self._probar_continuidad(
            frame_ms=30, preroll_ms=180, max_s=0.2, cantidad=12)
        self.assertEqual(tamanos, [2880, 2880])

    def test_defaults_cierran_en_30_segundos_sin_perder_frame_siguiente(self):
        cfg = Config()
        seg = Segmentador(
            cfg.sample_rate, cfg.frame_ms, VADSoloVoz(), cfg.silence_ms,
            cfg.preroll_ms, cfg.max_utterance_s, cfg.min_speech_ms,
        )
        eventos = []
        originales = []
        for valor in range(1, 1511):
            crudo = _frame(cfg.frame_samples, valor)
            originales.append(np.frombuffer(crudo, dtype=np.int16))
            eventos.extend(seg.procesar(crudo))
        eventos.extend(seg.finalizar())
        frases = [evento.audio for evento in eventos if evento.tipo == "frase"]
        self.assertEqual([frase.size for frase in frases], [480000, 3200])
        esperado = np.concatenate(originales).astype(np.float32) / 32768.0
        np.testing.assert_array_equal(np.concatenate(frases), esperado)


class StreamFalso:
    def __init__(self):
        self.stops = 0
        self.closes = 0

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


class PruebasRestoParcial(unittest.TestCase):
    @staticmethod
    def activar(mic=None, generacion=1):
        mic = mic or CapturadorMic(16000, 320)
        with mic._callback_lock:
            mic._preparar_generacion(generacion)
        mic._stream = StreamFalso()
        mic._estado = "recording"
        return mic

    @staticmethod
    def callback(mic, muestras, *, overflow=False):
        audio = np.arange(muestras, dtype=np.int16).tobytes()
        mic._callback(
            mic._generacion_aceptada, audio, muestras, None,
            SimpleNamespace(input_overflow=overflow),
        )

    def test_matriz_stop_conserva_frames_y_cuenta_solo_resto(self):
        for muestras, frames, resto in (
                (319, 0, 319), (320, 1, 0), (321, 1, 1),
                (639, 1, 319), (640, 2, 0), (641, 2, 1)):
            with self.subTest(muestras=muestras):
                mic = self.activar()
                self.callback(mic, muestras)
                mic.detener(vaciar=False)
                estado = mic.estado_captura()
                self.assertEqual(estado.frames_capturados, frames)
                self.assertEqual(estado.queue_depth, frames)
                self.assertEqual(estado.muestras_parciales_descartadas, resto)
                self.assertEqual(estado.frames_descartados, 0)
                self.assertFalse(estado.degradada)

    def test_callbacks_fragmentados_forman_un_frame_sin_descarte_parcial(self):
        mic = self.activar()
        for muestras in (100, 100, 120):
            self.callback(mic, muestras)
        mic.detener(vaciar=False)
        estado = mic.estado_captura()
        self.assertEqual(estado.frames_capturados, 1)
        self.assertEqual(estado.queue_depth, 1)
        self.assertEqual(estado.muestras_parciales_descartadas, 0)

    def test_overflow_invalida_resto_previo_y_stop_cuenta_solo_el_nuevo(self):
        mic = self.activar()
        self.callback(mic, 100)
        self.callback(mic, 220, overflow=True)
        mic.detener(vaciar=False)
        estado = mic.estado_captura()
        self.assertEqual(estado.device_overflows, 1)
        self.assertEqual(estado.muestras_parciales_descartadas, 220)
        self.assertTrue(estado.degradada)

    def test_nueva_generacion_reinicia_metrica_parcial(self):
        mic = self.activar(generacion=1)
        self.callback(mic, 321)
        mic.detener(vaciar=False)
        self.assertEqual(mic.estado_captura().muestras_parciales_descartadas, 1)

        self.activar(mic, generacion=2)
        self.assertEqual(mic.estado_captura().muestras_parciales_descartadas, 0)
        self.callback(mic, 319)
        mic.detener(vaciar=False)
        self.assertEqual(mic.estado_captura().muestras_parciales_descartadas, 319)

    def test_fallo_de_start_invalida_audio_y_no_publica_generacion(self):
        mic = CapturadorMic(16000, 320)

        class StreamQueFalla:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def start(self):
                self.callback(
                    np.arange(319, dtype=np.int16).tobytes(), 319, None,
                    SimpleNamespace(input_overflow=False),
                )
                raise OSError("start falló")

            def close(self):
                pass

        modulo = SimpleNamespace(RawInputStream=StreamQueFalla)
        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            with self.assertRaisesRegex(OSError, "start falló"):
                mic.iniciar(7)
        estado = mic.estado_captura()
        self.assertIsNone(estado.generacion)
        self.assertEqual(estado.queue_depth, 0)
        self.assertEqual(estado.muestras_parciales_descartadas, 0)

    def test_stop_con_vaciado_cuenta_resto_aunque_drene_frames(self):
        mic = self.activar()
        self.callback(mic, 321)
        mic.detener(vaciar=True)
        estado = mic.estado_captura()
        self.assertEqual(estado.queue_depth, 0)
        self.assertEqual(estado.muestras_parciales_descartadas, 1)

    def test_estado_publica_metrica_sin_degradar_salud(self):
        mic = self.activar()
        self.callback(mic, 319)
        mic.detener(vaciar=False)
        app = App.__new__(App)
        app._estado_cv = threading.Condition()
        app._estado = EstadoApp.IDLE
        app._ultimo_error = ""
        app._stt_health = "healthy"
        app._stt_failures = 0
        app._last_stt_error_type = None
        app._vad_health = "healthy"
        app._vad_failures = 0
        app._last_vad_error_type = None
        app._modo_solicitado = "utterance"
        app.cfg = Config()
        app.mic = mic
        respuesta = app._respuesta_estado()
        self.assertIn("audio=saludable", respuesta)
        self.assertIn("partial_samples_discarded=319", respuesta)


if __name__ == "__main__":
    unittest.main()
