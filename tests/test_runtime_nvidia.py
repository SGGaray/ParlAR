import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parlar import runtime_nvidia


class PruebasRuntimeNvidia(unittest.TestCase):
    def test_cpu_no_reejecuta(self):
        with mock.patch.object(runtime_nvidia.os, "execve") as ejecutar:
            self.assertFalse(runtime_nvidia.preparar_runtime_nvidia("cpu"))
        ejecutar.assert_not_called()

    def test_sin_bibliotecas_no_reejecuta(self):
        with (
            mock.patch.object(
                runtime_nvidia,
                "_directorios_runtime_nvidia",
                return_value=[],
            ),
            mock.patch.object(runtime_nvidia.os, "execve") as ejecutar,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(runtime_nvidia.preparar_runtime_nvidia("auto"))

        ejecutar.assert_not_called()

    def test_descubre_librerias_nvidia_en_site_packages(self):
        with tempfile.TemporaryDirectory() as temporal:
            base = Path(temporal)
            lib = base / "nvidia" / "cublas" / "lib"
            lib.mkdir(parents=True)
            (lib / "libcublas.so.12").touch()

            with mock.patch.object(
                runtime_nvidia,
                "_bases_site_packages",
                return_value=[base],
            ):
                encontrados = runtime_nvidia._directorios_runtime_nvidia()

        self.assertEqual(encontrados, [lib.resolve()])

    def test_auto_reejecuta_y_preserva_ruta_existente(self):
        cublas = Path("/venv/site-packages/nvidia/cublas/lib")
        cudnn = Path("/venv/site-packages/nvidia/cudnn/lib")

        with (
            mock.patch.object(
                runtime_nvidia,
                "_directorios_runtime_nvidia",
                return_value=[cublas, cudnn],
            ),
            mock.patch.dict(
                os.environ,
                {"LD_LIBRARY_PATH": "/sistema/lib"},
                clear=True,
            ),
            mock.patch.object(sys, "argv", ["parlar", "--sin-indicador"]),
            mock.patch.object(
                runtime_nvidia.os,
                "execve",
                side_effect=RuntimeError("exec interceptado"),
            ) as ejecutar,
        ):
            with self.assertRaisesRegex(RuntimeError, "exec interceptado"):
                runtime_nvidia.preparar_runtime_nvidia("auto")

        ejecutable, argv, entorno = ejecutar.call_args.args

        self.assertEqual(ejecutable, sys.executable)
        self.assertEqual(
            argv,
            [sys.executable, "-m", "parlar", "--sin-indicador"],
        )
        self.assertEqual(
            entorno["LD_LIBRARY_PATH"],
            os.pathsep.join([
                str(cublas),
                str(cudnn),
                "/sistema/lib",
            ]),
        )
        self.assertEqual(
            entorno[runtime_nvidia._MARCA_BOOTSTRAP],
            "1",
        )

    def test_cuda_explicito_tambien_prepara_runtime(self):
        lib = Path("/venv/site-packages/nvidia/cublas/lib")

        with (
            mock.patch.object(
                runtime_nvidia,
                "_directorios_runtime_nvidia",
                return_value=[lib],
            ),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                runtime_nvidia.os,
                "execve",
                side_effect=RuntimeError("exec interceptado"),
            ) as ejecutar,
        ):
            with self.assertRaisesRegex(RuntimeError, "exec interceptado"):
                runtime_nvidia.preparar_runtime_nvidia("cuda")

        ejecutar.assert_called_once()

    def test_marca_impide_loop_de_reexec(self):
        with (
            mock.patch.dict(
                os.environ,
                {runtime_nvidia._MARCA_BOOTSTRAP: "1"},
                clear=True,
            ),
            mock.patch.object(runtime_nvidia.os, "execve") as ejecutar,
        ):
            self.assertFalse(runtime_nvidia.preparar_runtime_nvidia("auto"))

        ejecutar.assert_not_called()

    def test_rutas_ya_presentes_no_reejecutan(self):
        lib = Path("/venv/site-packages/nvidia/cublas/lib")

        with (
            mock.patch.object(
                runtime_nvidia,
                "_directorios_runtime_nvidia",
                return_value=[lib],
            ),
            mock.patch.dict(
                os.environ,
                {"LD_LIBRARY_PATH": str(lib)},
                clear=True,
            ),
            mock.patch.object(runtime_nvidia.os, "execve") as ejecutar,
        ):
            self.assertFalse(runtime_nvidia.preparar_runtime_nvidia("auto"))

        ejecutar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
