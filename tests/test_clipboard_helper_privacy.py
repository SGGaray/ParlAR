"""Regresiones de privacidad para los helpers de clipboard."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from parlar.inyector_salida import Inyector


_RUNNER = textwrap.dedent("""
    import sys
    from parlar.inyector_salida import Inyector

    resultado = Inyector._copiar_con(
        [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],
        sys.argv[4],
    )
    print(f"METHOD_RESULT={str(resultado).lower()}")
""")

_HELPER = textwrap.dedent("""
    import pathlib
    import sys

    payload = sys.stdin.buffer.read()
    pathlib.Path(sys.argv[2]).write_bytes(payload)
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(payload)
    sys.stderr.buffer.flush()
    raise SystemExit(int(sys.argv[1]))
""")


class PruebasPrivacidadHelperClipboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="parlar-clipboard-")
        self.base = Path(self.tmp.name)
        self.helper = self.base / "helper.py"
        self.helper.write_text(_HELPER, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def ejecutar_helper(self, codigo, texto):
        recibido = self.base / f"recibido-{codigo}.bin"
        entorno = os.environ.copy()
        entorno["PYTHONPATH"] = os.getcwd()
        resultado = subprocess.run(
            [sys.executable, "-c", _RUNNER, str(self.helper),
             str(codigo), str(recibido), texto],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
            env=entorno,
        )
        return resultado, recibido.read_bytes()

    def test_success_unicode_llega_por_stdin_sin_filtrarse_en_logs(self):
        sentinel = "SEC001_éxito_á🙂"
        resultado, recibido = self.ejecutar_helper(0, sentinel)

        self.assertEqual(resultado.returncode, 0)
        self.assertEqual(recibido, sentinel.encode())
        self.assertIn("METHOD_RESULT=true", resultado.stdout)
        self.assertNotIn(sentinel, resultado.stdout)
        self.assertNotIn(sentinel, resultado.stderr)

    def test_failure_conserva_resultado_y_diagnostico_sin_payload(self):
        sentinel = "SEC001_fallo_ñ🙂"
        resultado, recibido = self.ejecutar_helper(7, sentinel)

        self.assertEqual(resultado.returncode, 0)
        self.assertEqual(recibido, sentinel.encode())
        self.assertIn("METHOD_RESULT=false", resultado.stdout)
        self.assertIn("CalledProcessError", resultado.stderr)
        self.assertNotIn(sentinel, resultado.stdout)
        self.assertNotIn(sentinel, resultado.stderr)

    def test_timeout_conserva_cota_y_no_registra_payload(self):
        sentinel = "SEC001_TIMEOUT_PRIVATE"
        error = io.StringIO()
        with mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["helper"], timeout=5,
                    output=sentinel.encode(), stderr=sentinel.encode()),
        ) as ejecutar, contextlib.redirect_stderr(error):
            self.assertFalse(Inyector._copiar_con(["helper"], sentinel))

        ejecutar.assert_called_once_with(
            ["helper"], input=sentinel.encode(), check=True, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertIn("TimeoutExpired", error.getvalue())
        self.assertNotIn(sentinel, error.getvalue())

    def test_helper_inexistente_preserva_fallo_sanitizado(self):
        sentinel = "SEC001_HELPER_MISSING_PRIVATE"
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            resultado = Inyector._copiar_con(
                [str(self.base / "no-existe")], sentinel)
        self.assertFalse(resultado)
        self.assertIn("FileNotFoundError", error.getvalue())
        self.assertNotIn(sentinel, error.getvalue())

    def test_seleccion_y_fallo_preservan_fallback_existente(self):
        inyector = Inyector(backend="clipboard", notify=False)
        with mock.patch("parlar.inyector_salida.detectar_sesion",
                        return_value="wayland"), \
                mock.patch("parlar.inyector_salida._cual",
                           side_effect=lambda nombre: nombre == "xclip"), \
                mock.patch.object(inyector, "_copiar_con",
                                  return_value=True) as copiar, \
                mock.patch.object(inyector, "_notificar"):
            self.assertTrue(inyector._portapapeles("texto"))
        copiar.assert_called_once_with(
            ["xclip", "-selection", "clipboard"], "texto")

        with mock.patch("parlar.inyector_salida.detectar_sesion",
                        return_value="wayland"), \
                mock.patch("parlar.inyector_salida._cual",
                           return_value=True), \
                mock.patch.object(inyector, "_copiar_con",
                                  return_value=False) as copiar:
            self.assertFalse(inyector._portapapeles("texto"))
        copiar.assert_called_once_with(["wl-copy"], "texto")


if __name__ == "__main__":
    unittest.main(verbosity=2)
