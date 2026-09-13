"""Regresiones de SIGTERM en la frontera ejecutable del daemon."""

import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parlar import __main__ as entrada
from parlar.app import App, EstadoApp
from parlar.config import Config
from parlar.control import ServidorControl
from tests.test_lifecycle import (
    FabricaSegmentador,
    FrasesFalsas,
    MicFalso,
    ProcesadorFalso,
    SalidaFalsa,
    StreamingFalso,
)


def _fallar_sigterm_sin_entry_point(_numero, _frame):
    raise AssertionError("SIGTERM sin handler del entry point")


class AppEntradaFalsa:
    def __init__(self):
        self.ejecuciones = 0
        self.cleanups = 0

    def ejecutar(self):
        self.ejecuciones += 1
        try:
            signal.raise_signal(signal.SIGTERM)
        except KeyboardInterrupt:
            pass
        finally:
            self.cleanups += 1


class RecursoContado:
    _listener = None

    def __init__(self):
        self.inicios = 0
        self.cierres = 0

    def iniciar(self):
        self.inicios += 1

    def detener(self):
        self.cierres += 1


class SalidaContada(SalidaFalsa):
    def __init__(self):
        super().__init__()
        self.cierres = 0

    def cerrar(self):
        self.cierres += 1
        super().cerrar()


class MicContado(MicFalso):
    def __init__(self):
        super().__init__()
        self.cierres = 0

    def detener(self, *, vaciar=True):
        self.cierres += 1
        return super().detener(vaciar=vaciar)


class UISigterm:
    def __init__(self):
        self.ejecuciones = 0
        self.cierres = 0

    def ejecutar(self):
        self.ejecuciones += 1
        signal.raise_signal(signal.SIGTERM)

    def fijar_estado(self, _estado):
        pass

    def cerrar(self):
        self.cierres += 1


class PruebasSenalesEntryPoint(unittest.TestCase):
    def test_importar_modulo_no_instala_handler(self):
        codigo = (
            "import signal\n"
            "anterior = signal.getsignal(signal.SIGTERM)\n"
            "import parlar.__main__\n"
            "assert signal.getsignal(signal.SIGTERM) == anterior\n"
        )
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)

    def test_sigterm_entra_por_main_limpia_una_vez_y_sale_normal(self):
        app = AppEntradaFalsa()
        handler_original = signal.signal(
            signal.SIGTERM, _fallar_sigterm_sin_entry_point)
        try:
            with (
                mock.patch.object(
                    entrada.Config, "load", return_value=Config()),
                mock.patch.object(sys, "argv", ["parlar"]),
                mock.patch("parlar.app.App", return_value=app),
            ):
                resultado = entrada.main()
            self.assertIsNone(resultado)
            self.assertEqual(app.ejecuciones, 1)
            self.assertEqual(app.cleanups, 1)
            self.assertIs(
                signal.getsignal(signal.SIGTERM),
                _fallar_sigterm_sin_entry_point,
            )
        finally:
            signal.signal(signal.SIGTERM, handler_original)

    def test_keyboard_interrupt_existente_sigue_siendo_shutdown_normal(self):
        class AppSigint(AppEntradaFalsa):
            def ejecutar(self):
                self.ejecuciones += 1
                try:
                    raise KeyboardInterrupt
                except KeyboardInterrupt:
                    pass
                finally:
                    self.cleanups += 1

        app = AppSigint()
        handler_sigint = signal.getsignal(signal.SIGINT)
        resultado = entrada._ejecutar_con_sigterm(app)
        self.assertIsNone(resultado)
        self.assertEqual(app.cleanups, 1)
        self.assertEqual(signal.getsignal(signal.SIGINT), handler_sigint)

    def test_sigterm_real_cierra_app_y_elimina_socket_propio(self):
        with tempfile.TemporaryDirectory(prefix="parlar-sigterm-") as tmp:
            ruta = Path(tmp) / "control.sock"
            control = ServidorControl(lambda _comando: "OK", ruta)
            atajos = RecursoContado()
            mic = MicContado()
            ui = UISigterm()
            salidas = [SalidaContada() for _ in range(3)]
            app = App(
                Config(overlay=False, notify=False),
                frases=FrasesFalsas(),
                streaming=StreamingFalso(),
                proc=ProcesadorFalso(),
                inyector=salidas[0],
                guionar=salidas[1],
                sesion=salidas[2],
                mic=mic,
                ui=ui,
                control=control,
                atajos=atajos,
                vad_factory=lambda *_: object(),
                segmentador_factory=FabricaSegmentador(),
            )
            try:
                handler_original = signal.signal(
                    signal.SIGTERM, _fallar_sigterm_sin_entry_point)
                try:
                    with mock.patch.object(
                            control, "detener", wraps=control.detener) as detener:
                        resultado = entrada._ejecutar_con_sigterm(app)
                finally:
                    signal.signal(signal.SIGTERM, handler_original)
                self.assertIsNone(resultado)
                self.assertEqual(app.estado, EstadoApp.CLOSED)
                self.assertFalse(ruta.exists())
                self.assertEqual(detener.call_count, 1)
                self.assertEqual(atajos.cierres, 1)
                self.assertEqual(mic.cierres, 1)
                self.assertEqual(ui.cierres, 1)
                self.assertEqual([salida.cierres for salida in salidas],
                                 [1, 1, 1])
                self.assertFalse(app._trabajador_hilo.is_alive())
            finally:
                app.salir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
