"""Contrato aislado de la prueba temporal de micrófono."""

import builtins
import subprocess
import sys
import unittest
from unittest import mock

import numpy as np

from parlar.audio_test import (
    ErrorPruebaAudio,
    EstadoPruebaAudio,
    PruebaMicrofono,
    calcular_nivel_rms,
)


class StreamFalso:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.kwargs = kwargs
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self.error_start = None
        self.error_stop = None
        self.error_close = None

    def start(self):
        self.starts += 1
        if self.error_start is not None:
            raise self.error_start

    def stop(self):
        self.stops += 1
        if self.error_stop is not None:
            raise self.error_stop

    def close(self):
        self.closes += 1
        if self.error_close is not None:
            raise self.error_close

    def emitir(self, muestras, status=None):
        self.callback(muestras, len(muestras), None, status)


class SounddeviceFalso:
    def __init__(self, fabrica=None):
        self.streams = []
        self._fabrica = fabrica or StreamFalso

    def InputStream(self, **kwargs):
        stream = self._fabrica(**kwargs)
        self.streams.append(stream)
        return stream


class PruebasNivelAudio(unittest.TestCase):
    def test_silencio_es_cero_y_senal_es_positiva(self):
        self.assertEqual(calcular_nivel_rms(np.zeros(32)), 0.0)
        self.assertAlmostEqual(
            calcular_nivel_rms(np.full(32, 0.5, dtype=np.float32)),
            0.5,
        )

    def test_clamp_y_no_finitos_nunca_escapan_del_rango(self):
        nivel = calcular_nivel_rms(
            np.array([2.0, -2.0, np.nan, np.inf, -np.inf]))
        self.assertGreaterEqual(nivel, 0.0)
        self.assertLessEqual(nivel, 1.0)
        self.assertTrue(np.isfinite(nivel))
        self.assertEqual(calcular_nivel_rms(np.full(8, 20.0)), 1.0)


class PruebasLifecycleAudio(unittest.TestCase):
    def test_start_abre_un_stream_y_publica_snapshot_inmutable(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo, sample_rate=48000)

        self.assertTrue(prueba.iniciar(7))

        self.assertEqual(len(modulo.streams), 1)
        stream = modulo.streams[0]
        self.assertEqual(stream.starts, 1)
        self.assertEqual(stream.kwargs["device"], 7)
        self.assertEqual(stream.kwargs["samplerate"], 48000)
        self.assertEqual(stream.kwargs["channels"], 1)
        self.assertEqual(stream.kwargs["dtype"], "float32")
        self.assertEqual(
            prueba.estado(), EstadoPruebaAudio(0.0, True, None))
        prueba.detener()

    def test_callback_publica_nivel_sin_conservar_audio(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)
        prueba.iniciar()

        modulo.streams[0].emitir(np.full((16, 1), 0.25, dtype=np.float32))

        self.assertAlmostEqual(prueba.estado().nivel, 0.25)
        self.assertFalse(hasattr(prueba, "audio"))
        prueba.detener()

    def test_start_duplicado_no_abre_otro_stream(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)

        self.assertTrue(prueba.iniciar())
        self.assertFalse(prueba.iniciar(9))

        self.assertEqual(len(modulo.streams), 1)
        prueba.detener()

    def test_stop_cierra_y_stop_doble_es_seguro(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)
        prueba.iniciar()

        self.assertTrue(prueba.detener())
        self.assertFalse(prueba.detener())

        stream = modulo.streams[0]
        self.assertEqual(stream.stops, 1)
        self.assertEqual(stream.closes, 1)
        self.assertFalse(prueba.estado().activo)

    def test_cancelar_cierra_el_stream(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)
        prueba.iniciar()

        self.assertTrue(prueba.cancelar())

        self.assertEqual(modulo.streams[0].closes, 1)
        self.assertFalse(prueba.estado().activo)

    def test_error_al_abrir_deja_estado_consistente(self):
        modulo = mock.Mock()
        modulo.InputStream.side_effect = OSError("PortAudio caído")
        prueba = PruebaMicrofono(modulo)

        with self.assertRaisesRegex(ErrorPruebaAudio, "PortAudio caído"):
            prueba.iniciar()

        estado = prueba.estado()
        self.assertFalse(estado.activo)
        self.assertIn("PortAudio caído", estado.error)
        self.assertFalse(prueba.detener())

    def test_error_de_start_cierra_el_stream(self):
        def fabrica(**kwargs):
            stream = StreamFalso(**kwargs)
            stream.error_start = OSError("start falló")
            return stream

        modulo = SounddeviceFalso(fabrica)
        prueba = PruebaMicrofono(modulo)

        with self.assertRaisesRegex(ErrorPruebaAudio, "start falló"):
            prueba.iniciar()

        stream = modulo.streams[0]
        self.assertEqual(stream.stops, 0)
        self.assertEqual(stream.closes, 1)
        self.assertFalse(prueba.estado().activo)

    def test_error_del_callback_queda_representado_y_cierra(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)
        prueba.iniciar()

        with mock.patch(
                "parlar.audio_test.calcular_nivel_rms",
                side_effect=ValueError("muestra inválida")):
            modulo.streams[0].emitir(np.ones(4, dtype=np.float32))
        prueba.detener()

        estado = prueba.estado()
        self.assertFalse(estado.activo)
        self.assertIn("muestra inválida", estado.error)
        self.assertEqual(modulo.streams[0].stops, 1)
        self.assertEqual(modulo.streams[0].closes, 1)

    def test_fallo_de_stop_intenta_close_y_es_explicito(self):
        def fabrica(**kwargs):
            stream = StreamFalso(**kwargs)
            stream.error_stop = OSError("stop falló")
            return stream

        modulo = SounddeviceFalso(fabrica)
        prueba = PruebaMicrofono(modulo)
        prueba.iniciar()

        with self.assertRaisesRegex(ErrorPruebaAudio, "stop falló"):
            prueba.detener()

        self.assertEqual(modulo.streams[0].closes, 1)
        self.assertFalse(prueba.estado().activo)
        self.assertFalse(prueba.detener())

    def test_callback_viejo_no_modifica_una_prueba_nueva(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)

        prueba.iniciar()
        stream_viejo = modulo.streams[0]
        prueba.detener()

        prueba.iniciar()
        stream_actual = modulo.streams[1]
        stream_actual.emitir(
            np.full((16, 1), 0.25, dtype=np.float32)
        )
        esperado = prueba.estado()

        stream_viejo.emitir(
            np.full((16, 1), 0.90, dtype=np.float32)
        )
        stream_viejo.emitir(
            np.ones((4, 1), dtype=np.float32),
            status="callback tardío",
        )

        self.assertEqual(prueba.estado(), esperado)
        self.assertTrue(prueba.estado().activo)

        prueba.detener()

    def test_fallo_de_close_es_explicito_y_deja_estado_consistente(self):
        def fabrica(**kwargs):
            stream = StreamFalso(**kwargs)
            stream.error_close = OSError("close falló")
            return stream

        modulo = SounddeviceFalso(fabrica)
        prueba = PruebaMicrofono(modulo)
        prueba.iniciar()

        with self.assertRaisesRegex(ErrorPruebaAudio, "close falló"):
            prueba.detener()

        stream = modulo.streams[0]
        self.assertEqual(stream.stops, 1)
        self.assertEqual(stream.closes, 1)
        self.assertFalse(prueba.estado().activo)
        self.assertIn("close falló", prueba.estado().error)
        self.assertFalse(prueba.detener())

    def test_prueba_no_escribe_archivos(self):
        modulo = SounddeviceFalso()
        prueba = PruebaMicrofono(modulo)
        with mock.patch.object(
                builtins, "open", side_effect=AssertionError("no escribir")):
            prueba.iniciar()
            modulo.streams[0].emitir(np.ones(4, dtype=np.float32))
            prueba.detener()

    def test_import_no_carga_app_whisper_ni_sounddevice(self):
        codigo = """
import sys
import parlar.audio_test
prohibidos = {'parlar.app', 'sounddevice', 'faster_whisper', 'ctranslate2'}
assert prohibidos.isdisjoint(sys.modules), prohibidos & set(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


if __name__ == "__main__":
    unittest.main()
