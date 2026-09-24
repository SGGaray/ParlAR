"""UX del entry point frente a una segunda instancia."""

import contextlib
import io
import sys
import unittest
from unittest import mock

from parlar import __main__ as entrada
from parlar.config import Config
from parlar.control import InstanciaActivaError


class AppFalsa:
    def __init__(self, error):
        self.error = error
        self.ejecuciones = 0

    def ejecutar(self):
        self.ejecuciones += 1
        raise self.error

    def salir(self, *, esperar=True):
        del esperar


class PruebasSegundaInstancia(unittest.TestCase):
    def _ejecutar_main(self, app):
        with (
            mock.patch.object(entrada.Config, "load", return_value=Config()),
            mock.patch.object(sys, "argv", ["parlar"]),
            mock.patch.object(
                entrada, "preparar_runtime_nvidia", return_value=False),
            mock.patch("parlar.app.App", return_value=app),
        ):
            entrada.main()

    def test_segunda_instancia_muestra_mensaje_limpio_y_sale_uno(self):
        app = AppFalsa(InstanciaActivaError("detalle interno"))
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as salida,
        ):
            self._ejecutar_main(app)

        self.assertEqual(salida.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "ParlAR ya está ejecutándose.\n")
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(app.ejecuciones, 1)

    def test_error_inesperado_no_es_absorbido(self):
        app = AppFalsa(RuntimeError("fallo de inicio inesperado"))
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(RuntimeError, "fallo de inicio inesperado"),
        ):
            self._ejecutar_main(app)

        self.assertNotIn("ParlAR ya está ejecutándose", stderr.getvalue())
        self.assertEqual(app.ejecuciones, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
