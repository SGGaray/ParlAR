"""Regresiones de SIGTERM en la frontera ejecutable del daemon."""

import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from parlar import __main__ as entrada
from parlar.app import App, EstadoApp
from parlar.config import Config
from parlar.control import ServidorControl
from parlar.indicador import BucleSinUI
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
        self.solicitudes = 0
        self.shutdown_solicitado = threading.Event()

    def ejecutar(self):
        self.ejecuciones += 1
        signal.raise_signal(signal.SIGTERM)
        if not self.shutdown_solicitado.wait(2):
            raise AssertionError("SIGTERM no produjo solicitud de shutdown")

    def salir(self, *, esperar=True):
        self.solicitudes += 1
        self.shutdown_solicitado.set()


class AppTclFalsa(AppEntradaFalsa):
    def __init__(self):
        super().__init__()
        self._lifecycle_lock = threading.Lock()
        self.callback_completo = threading.Event()
        self.codigo_tcl = None
        self.shutdown_dentro_callback = False

    def ejecutar(self):
        import tkinter

        self.ejecuciones += 1
        interprete = tkinter.Tcl()

        def callback():
            with self._lifecycle_lock:
                signal.raise_signal(signal.SIGTERM)
                self.callback_completo.set()
            return ""

        interprete.createcommand("parlar_callback", callback)
        self.codigo_tcl = int(interprete.eval("catch {parlar_callback}"))
        if not self.shutdown_solicitado.wait(2):
            raise AssertionError("Tcl consumió la solicitud de shutdown")

    def salir(self, *, esperar=True):
        with self._lifecycle_lock:
            self.shutdown_dentro_callback = not self.callback_completo.is_set()
            super().salir(esperar=esperar)


class AppNormalFalsa(AppEntradaFalsa):
    def ejecutar(self):
        self.ejecuciones += 1
        return "normal"


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


class UITclSigterm:
    def __init__(self, repeticiones=1):
        self.ejecuciones = 0
        self.cierres = 0
        self.repeticiones = repeticiones
        self.app = None
        self.callback_completo = threading.Event()
        self.shutdown_observado = False
        self.cerrada = threading.Event()

    def ejecutar(self):
        import tkinter

        self.ejecuciones += 1
        interprete = tkinter.Tcl()

        def callback():
            for _ in range(self.repeticiones):
                signal.raise_signal(signal.SIGTERM)
            self.callback_completo.set()
            return ""

        interprete.createcommand("parlar_callback", callback)
        interprete.eval("catch {parlar_callback}")
        self.shutdown_observado = self.app.saliendo.wait(2)
        self.cerrada.wait(2)

    def fijar_estado(self, _estado):
        pass

    def cerrar(self):
        self.cierres += 1
        self.cerrada.set()


class BucleSinUISigterm(BucleSinUI):
    def ejecutar(self):
        signal.raise_signal(signal.SIGTERM)
        super().ejecutar()


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
            self.assertEqual(app.solicitudes, 1)
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
                    self.salir()

        app = AppSigint()
        handler_sigint = signal.getsignal(signal.SIGINT)
        resultado = entrada._ejecutar_con_sigterm(app)
        self.assertIsNone(resultado)
        self.assertEqual(app.solicitudes, 1)
        self.assertEqual(signal.getsignal(signal.SIGINT), handler_sigint)

    def test_callback_tcl_no_puede_consumir_la_solicitud(self):
        app = AppTclFalsa()
        entrada._ejecutar_con_sigterm(app)
        self.assertEqual(app.codigo_tcl, 0)
        self.assertTrue(app.callback_completo.is_set())
        self.assertEqual(app.solicitudes, 1)
        self.assertFalse(app.shutdown_dentro_callback)

    def test_sigterm_repetido_solicita_shutdown_una_vez(self):
        class AppRepetida(AppEntradaFalsa):
            def ejecutar(self):
                self.ejecuciones += 1
                signal.raise_signal(signal.SIGTERM)
                signal.raise_signal(signal.SIGTERM)
                if not self.shutdown_solicitado.wait(2):
                    raise AssertionError("shutdown no solicitado")

        app = AppRepetida()
        entrada._ejecutar_con_sigterm(app)
        self.assertEqual(app.solicitudes, 1)

    def test_salida_normal_termina_watcher_sin_solicitar_shutdown(self):
        app = AppNormalFalsa()
        hilos_antes = set(threading.enumerate())
        self.assertEqual(entrada._ejecutar_con_sigterm(app), "normal")
        hilos_nuevos = set(threading.enumerate()) - hilos_antes
        self.assertEqual(app.solicitudes, 0)
        self.assertFalse(any(
            hilo.name == "sigterm-watcher" and hilo.is_alive()
            for hilo in hilos_nuevos
        ))

    def test_estados_lifecycle_aceptan_sigterm_desde_callback_tcl(self):
        for estado in (
            EstadoApp.IDLE,
            EstadoApp.STARTING,
            EstadoApp.RECORDING,
            EstadoApp.STOPPING,
        ):
            with self.subTest(estado=estado):
                atajos = RecursoContado()
                mic = MicContado()
                ui = UITclSigterm()
                salidas = [SalidaContada() for _ in range(3)]
                app = App(
                    Config(overlay=False, notify=False),
                    frases=FrasesFalsas(), streaming=StreamingFalso(),
                    proc=ProcesadorFalso(), inyector=salidas[0],
                    guionar=salidas[1], sesion=salidas[2], mic=mic, ui=ui,
                    control=RecursoContado(), atajos=atajos,
                    vad_factory=lambda *_: object(),
                    segmentador_factory=FabricaSegmentador(),
                )
                ui.app = app
                with app._estado_cv:
                    app._estado = estado
                entrada._ejecutar_con_sigterm(app)
                self.assertTrue(ui.shutdown_observado)
                self.assertEqual(app.estado, EstadoApp.CLOSED)
                self.assertEqual(mic.cierres, 1)
                self.assertEqual(atajos.cierres, 1)
                self.assertEqual(ui.cierres, 1)
                self.assertEqual(
                    [salida.cierres for salida in salidas], [1, 1, 1])

    def test_sigterm_cierra_bucle_sin_ui(self):
        ui = BucleSinUISigterm()
        app = App(
            Config(overlay=False, notify=False),
            frases=FrasesFalsas(), streaming=StreamingFalso(),
            proc=ProcesadorFalso(), inyector=SalidaContada(),
            guionar=SalidaContada(), sesion=SalidaContada(), mic=MicContado(),
            ui=ui, control=RecursoContado(), atajos=RecursoContado(),
            vad_factory=lambda *_: object(),
            segmentador_factory=FabricaSegmentador(),
        )
        entrada._ejecutar_con_sigterm(app)
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        self.assertTrue(ui._salir)

    def test_sigterm_real_cierra_app_y_elimina_socket_propio(self):
        with tempfile.TemporaryDirectory(prefix="parlar-sigterm-") as tmp:
            ruta = Path(tmp) / "control.sock"
            control = ServidorControl(lambda _comando: "OK", ruta)
            atajos = RecursoContado()
            mic = MicContado()
            ui = UITclSigterm(repeticiones=2)
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
            ui.app = app
            with app._estado_cv:
                app._estado = EstadoApp.STARTING
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
                self.assertTrue(ui.shutdown_observado)
                self.assertTrue(ui.callback_completo.is_set())
                self.assertEqual(app.estado, EstadoApp.CLOSED)
                self.assertFalse(ruta.exists())
                cliente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    with self.assertRaises(OSError):
                        cliente.connect(str(ruta))
                finally:
                    cliente.close()
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
