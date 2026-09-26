"""Regresiones de la selección de entrada en el runtime productivo."""

import contextlib
import io
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from parlar import __main__ as entrada
from parlar.app import App, resolver_entrada_productiva
from parlar.capturador_audio import CapturadorMic
from parlar.config import Config
from parlar.settings_backend import (
    DispositivoEntrada,
    ErrorDispositivosAudio,
    ErrorInicializacionAudio,
    crear_identidad_entrada,
)


def _dispositivo(indice, nombre, *, predeterminado=False, host_api="ALSA"):
    return DispositivoEntrada(
        indice=indice,
        nombre=nombre,
        canales_entrada=1,
        predeterminado=predeterminado,
        host_api=host_api,
        identidad=crear_identidad_entrada(nombre, host_api),
    )


def _crear_app(cfg, inventario, constructor_mic):
    salida = SimpleNamespace(
        inyector=object(),
        guionar=object(),
        sesion=object(),
        ultima_entrega=None,
        necesita_espacio=False,
    )
    with (
        mock.patch("parlar.app.CapturadorMic", constructor_mic),
        mock.patch("parlar.app.crear_coordinador_salida", return_value=salida),
    ):
        return App(
            cfg,
            motor=object(),
            frases=object(),
            streaming=object(),
            proc=object(),
            ui=object(),
            control=object(),
            atajos=object(),
            listar_entradas=lambda: inventario,
        )


class PruebasResolucionProductiva(unittest.TestCase):
    def test_default_usa_el_marcado_en_inventario_y_no_el_indice_cero(self):
        inventario = (
            _dispositivo(0, "Monitor"),
            _dispositivo(7, "Mic USB", predeterminado=True),
        )
        constructor = mock.Mock(return_value=object())

        app = _crear_app(Config(), inventario, constructor)

        constructor.assert_called_once_with(16000, 320, input_device=7)
        self.assertEqual(app.resolucion_entrada.indice, 7)

    def test_identidad_persistida_resuelve_su_indice_actual(self):
        mic = _dispositivo(12, "Mic estable")
        cfg = Config(audio_input_device=mic.identidad)
        constructor = mock.Mock(return_value=object())

        _crear_app(cfg, (mic,), constructor)

        constructor.assert_called_once_with(16000, 320, input_device=12)

    def test_identidad_estable_puede_cambiar_de_indice_entre_inicios(self):
        primero = _dispositivo(3, "Mic estable")
        segundo = _dispositivo(19, "Mic estable")

        resolucion_1 = resolver_entrada_productiva(
            primero.identidad, listar_entradas=lambda: (primero,))
        resolucion_2 = resolver_entrada_productiva(
            primero.identidad, listar_entradas=lambda: (segundo,))

        self.assertEqual((resolucion_1.indice, resolucion_2.indice), (3, 19))

    def test_ausente_hace_fallback_observable_sin_mutar_config(self):
        predeterminado = _dispositivo(6, "Mic integrado", predeterminado=True)
        seleccion = crear_identidad_entrada("Mic desconectado", "ALSA")
        cfg = Config(audio_input_device=seleccion)
        constructor = mock.Mock(return_value=object())
        errores = io.StringIO()

        with contextlib.redirect_stderr(errores):
            app = _crear_app(cfg, (predeterminado,), constructor)

        self.assertEqual(cfg.audio_input_device, seleccion)
        self.assertTrue(app.resolucion_entrada.usando_fallback)
        self.assertEqual(app.resolucion_entrada.motivo, "seleccion_ausente")
        self.assertIn("predeterminada", errores.getvalue())
        self.assertIn("seleccion_ausente", errores.getvalue())
        constructor.assert_called_once_with(16000, 320, input_device=6)

    def test_identidad_ambigua_hace_fallback_al_default_unico(self):
        duplicado_a = _dispositivo(2, "Mic duplicado")
        duplicado_b = _dispositivo(8, "Mic duplicado")
        predeterminado = _dispositivo(5, "Mic default", predeterminado=True)

        resolucion = resolver_entrada_productiva(
            duplicado_a.identidad,
            listar_entradas=lambda: (
                duplicado_a, duplicado_b, predeterminado),
        )

        self.assertEqual(resolucion.indice, 5)
        self.assertTrue(resolucion.usando_fallback)
        self.assertEqual(resolucion.motivo, "seleccion_ambigua")

    def test_inventario_vacio_falla_controlado(self):
        constructor = mock.Mock(return_value=object())
        with self.assertRaisesRegex(
                ErrorInicializacionAudio, "inventario_vacio"):
            _crear_app(Config(), (), constructor)
        constructor.assert_not_called()

    def test_default_no_disponible_falla_controlado(self):
        with self.assertRaisesRegex(
                ErrorInicializacionAudio, "default_no_disponible"):
            resolver_entrada_productiva(
                "default", listar_entradas=lambda: (_dispositivo(4, "Mic"),))

    def test_seleccion_ausente_sin_default_falla_controlado(self):
        seleccion = crear_identidad_entrada("Ausente", "ALSA")
        with self.assertRaisesRegex(
                ErrorInicializacionAudio,
                "seleccion_ausente_default_no_disponible"):
            resolver_entrada_productiva(
                seleccion, listar_entradas=lambda: (_dispositivo(4, "Otro"),))

    def test_seleccion_ambigua_sin_default_falla_controlado(self):
        duplicado_a = _dispositivo(2, "Duplicado")
        duplicado_b = _dispositivo(9, "Duplicado")
        with self.assertRaisesRegex(
                ErrorInicializacionAudio,
                "seleccion_ambigua_default_no_disponible"):
            resolver_entrada_productiva(
                duplicado_a.identidad,
                listar_entradas=lambda: (duplicado_a, duplicado_b),
            )

    def test_error_de_enumeracion_falla_controlado(self):
        def fallar():
            raise ErrorDispositivosAudio("PortAudio no responde")

        with self.assertRaisesRegex(
                ErrorInicializacionAudio, "PortAudio no responde"):
            resolver_entrada_productiva("default", listar_entradas=fallar)

    def test_device_whisper_no_interfiere_con_dispositivo_de_audio(self):
        mic = _dispositivo(11, "Mic", predeterminado=True)
        for dispositivo_whisper in ("auto", "cpu", "cuda"):
            with self.subTest(device=dispositivo_whisper):
                cfg = Config(device=dispositivo_whisper)
                constructor = mock.Mock(return_value=object())
                _crear_app(cfg, (mic,), constructor)
                constructor.assert_called_once_with(
                    16000, 320, input_device=11)


class PruebasCapturadorConDispositivo(unittest.TestCase):
    def test_raw_input_stream_recibe_indice_y_conserva_argumentos(self):
        stream = mock.Mock()
        constructor = mock.Mock(return_value=stream)
        modulo = SimpleNamespace(RawInputStream=constructor)
        mic = CapturadorMic(16000, 320, input_device=23)

        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            self.assertTrue(mic.iniciar(1))
            mic.detener()

        argumentos = constructor.call_args.kwargs
        self.assertEqual(set(argumentos), {
            "samplerate", "channels", "dtype", "blocksize", "callback", "device",
        })
        self.assertEqual(argumentos["samplerate"], 16000)
        self.assertEqual(argumentos["channels"], 1)
        self.assertEqual(argumentos["dtype"], "int16")
        self.assertEqual(argumentos["blocksize"], 320)
        self.assertEqual(argumentos["device"], 23)
        self.assertTrue(callable(argumentos["callback"]))

    def test_compatibilidad_sin_indice_no_envia_device(self):
        stream = mock.Mock()
        constructor = mock.Mock(return_value=stream)
        modulo = SimpleNamespace(RawInputStream=constructor)
        mic = CapturadorMic(16000, 320)

        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            mic.iniciar(1)
            mic.detener()

        self.assertNotIn("device", constructor.call_args.kwargs)

    def test_doble_inicio_con_indice_abre_un_solo_stream(self):
        stream = mock.Mock()
        constructor = mock.Mock(return_value=stream)
        modulo = SimpleNamespace(RawInputStream=constructor)
        mic = CapturadorMic(16000, 320, input_device=14)

        with mock.patch.dict(sys.modules, {"sounddevice": modulo}):
            self.assertTrue(mic.iniciar(1))
            self.assertFalse(mic.iniciar(2))
            mic.detener()

        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.kwargs["device"], 14)


class PruebasErrorEntryPointAudio(unittest.TestCase):
    def test_error_audio_sale_sin_traceback_y_libera_guardia(self):
        guardia = mock.Mock()
        guardia.preservar_en_reexec.return_value = contextlib.nullcontext()
        error = ErrorInicializacionAudio("inventario_vacio")
        stderr = io.StringIO()

        with (
            mock.patch.object(entrada.Config, "load", return_value=Config()),
            mock.patch.object(sys, "argv", ["parlar"]),
            mock.patch.object(
                entrada.GuardiaInstancia,
                "adquirir_para_entry_point",
                return_value=guardia,
            ),
            mock.patch.object(
                entrada, "preparar_runtime_nvidia", return_value=False),
            mock.patch("parlar.app.App", side_effect=error),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as salida,
        ):
            entrada.main()

        self.assertEqual(salida.exception.code, 1)
        self.assertIn("[audio] no se pudo iniciar", stderr.getvalue())
        self.assertIn("inventario_vacio", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        guardia.liberar.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
