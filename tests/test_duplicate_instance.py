"""UX del entry point frente a una segunda instancia."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from parlar import __main__ as entrada
from parlar.config import Config
from parlar.control import (
    GuardiaInstancia,
    InstanciaActivaError,
)


class AppFalsa:
    def __init__(self, error=None):
        self.error = error
        self.ejecuciones = 0

    def ejecutar(self):
        self.ejecuciones += 1
        if self.error is not None:
            raise self.error

    def salir(self, *, esperar=True):
        del esperar


class PruebasSegundaInstancia(unittest.TestCase):
    def test_segunda_instancia_se_rechaza_antes_del_runtime_y_app(self):
        constructor = mock.Mock(
            side_effect=AssertionError("App no debe construirse"))
        modulo_app = SimpleNamespace(App=constructor)
        stderr = io.StringIO()

        with (
            mock.patch.object(entrada.Config, "load", return_value=Config()),
            mock.patch.object(sys, "argv", ["parlar"]),
            mock.patch.object(
                GuardiaInstancia,
                "adquirir_para_entry_point",
                side_effect=InstanciaActivaError("detalle interno"),
            ),
            mock.patch.object(
                entrada, "preparar_runtime_nvidia", return_value=False,
            ) as preparar_runtime,
            mock.patch.dict(sys.modules, {"parlar.app": modulo_app}),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as salida,
        ):
            entrada.main()

        self.assertEqual(salida.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "ParlAR ya está ejecutándose.\n")
        self.assertNotIn("Traceback", stderr.getvalue())
        preparar_runtime.assert_not_called()
        constructor.assert_not_called()

    def test_python_m_rechaza_antes_de_cualquier_salida_pesada(self):
        with tempfile.TemporaryDirectory(prefix="parlar-cli-") as tmp:
            base = Path(tmp)
            raiz_repo = Path(__file__).resolve().parents[1]
            entorno = os.environ.copy()
            entorno["XDG_RUNTIME_DIR"] = str(base)
            entorno["XDG_CONFIG_HOME"] = str(base / "config")
            entorno["PYTHONPATH"] = os.pathsep.join(filter(None, (
                str(raiz_repo), entorno.get("PYTHONPATH", ""))))
            poseedor = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    (
                        "from parlar.control import GuardiaInstancia; "
                        "g=GuardiaInstancia(); g.adquirir(); "
                        "print('listo', flush=True); input(); g.liberar()"
                    ),
                ],
                cwd=raiz_repo,
                env=entorno,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(poseedor.stdout.readline().strip(), "listo")
                resultado = subprocess.run(
                    [sys.executable, "-B", "-m", "parlar"],
                    cwd=raiz_repo,
                    env=entorno,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            finally:
                if poseedor.poll() is None:
                    poseedor.stdin.write("\n")
                    poseedor.stdin.flush()
                _, error_poseedor = poseedor.communicate(timeout=10)

        self.assertEqual(poseedor.returncode, 0, error_poseedor)
        self.assertEqual(resultado.returncode, 1)
        self.assertEqual(resultado.stderr, "ParlAR ya está ejecutándose.\n")
        self.assertEqual(resultado.stdout, "")

    def test_error_inesperado_no_es_absorbido(self):
        app = AppFalsa(RuntimeError("fallo de inicio inesperado"))
        guardia = mock.Mock()
        guardia.preservar_en_reexec.return_value = contextlib.nullcontext()
        stderr = io.StringIO()

        with (
            mock.patch.object(entrada.Config, "load", return_value=Config()),
            mock.patch.object(sys, "argv", ["parlar"]),
            mock.patch.object(
                GuardiaInstancia,
                "adquirir_para_entry_point",
                return_value=guardia,
            ),
            mock.patch.object(
                entrada, "preparar_runtime_nvidia", return_value=False),
            mock.patch("parlar.app.App", return_value=app) as constructor,
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(RuntimeError, "fallo de inicio inesperado"),
        ):
            entrada.main()

        self.assertNotIn("ParlAR ya está ejecutándose", stderr.getvalue())
        self.assertEqual(app.ejecuciones, 1)
        constructor.assert_called_once_with(
            mock.ANY, guardia_instancia=guardia)
        guardia.liberar.assert_called_once_with()


class PruebasGuardiaInstancia(unittest.TestCase):
    def test_lock_permanece_tomado_durante_reexec_y_se_libera_al_final(self):
        with tempfile.TemporaryDirectory(prefix="parlar-guardia-") as tmp:
            ruta = Path(tmp) / "control.sock"
            primera = GuardiaInstancia(ruta)
            primera.adquirir()
            fd = primera.fileno()

            with primera.preservar_en_reexec():
                self.assertTrue(os.get_inheritable(fd))
                self.assertEqual(
                    os.environ[GuardiaInstancia.ENV_FD], str(fd))
                with self.assertRaises(InstanciaActivaError):
                    GuardiaInstancia(ruta).adquirir()

            self.assertFalse(os.get_inheritable(fd))
            self.assertNotIn(GuardiaInstancia.ENV_FD, os.environ)
            with self.assertRaises(InstanciaActivaError):
                GuardiaInstancia(ruta).adquirir()

            primera.liberar()
            siguiente = GuardiaInstancia(ruta)
            siguiente.adquirir()
            siguiente.liberar()

    def test_reexec_adopta_el_mismo_flock_sin_ventana(self):
        with tempfile.TemporaryDirectory(prefix="parlar-reexec-") as tmp:
            base = Path(tmp)
            ruta = base / "control.sock"
            probe = base / "probe.py"
            probe.write_text(
                """
import os
import sys
from pathlib import Path
from parlar.control import GuardiaInstancia, InstanciaActivaError

ruta = Path(sys.argv[1])
if os.environ.get("PARLAR_TEST_REEXEC") != "1":
    guardia = GuardiaInstancia(ruta)
    guardia.adquirir()
    with guardia.preservar_en_reexec():
        entorno = os.environ.copy()
        entorno["PARLAR_TEST_REEXEC"] = "1"
        os.execve(
            sys.executable,
            [sys.executable, "-B", sys.argv[0], str(ruta)],
            entorno,
        )
else:
    guardia = GuardiaInstancia.adquirir_para_entry_point(ruta)
    assert not os.get_inheritable(guardia.fileno())
    try:
        GuardiaInstancia(ruta).adquirir()
    except InstanciaActivaError:
        pass
    else:
        raise AssertionError("el flock se perdió durante execve")
    guardia.liberar()
    siguiente = GuardiaInstancia(ruta)
    siguiente.adquirir()
    siguiente.liberar()
""".lstrip(),
                encoding="utf-8",
            )
            raiz_repo = Path(__file__).resolve().parents[1]
            entorno = os.environ.copy()
            entorno["PYTHONPATH"] = os.pathsep.join(filter(None, (
                str(raiz_repo), entorno.get("PYTHONPATH", ""))))
            resultado = subprocess.run(
                [sys.executable, "-B", str(probe), str(ruta)],
                cwd=raiz_repo,
                env=entorno,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

        self.assertEqual(resultado.returncode, 0, resultado.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
