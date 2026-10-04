"""Regresión del wheelhouse con intérpretes manylinux de igual basename.

En manylinux todos los CPython se llaman ``/opt/python/cp3XY-cp3XY/bin/python``.
El staging debe separarse por CPython y nunca mezclar wheels de otro ABI. Sin
red ni compilador: venv, pip y auditwheel se simulan, pero el ABI de cada
wheel lo decide el intérprete que realmente lo "compila".
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_wheelhouse as bw  # noqa: E402


class CompiladorFalso:
    """Simula subprocess.run para venv, pip wheel y auditwheel repair."""

    def __init__(self, base: Path, abi_forzado: str | None = None):
        self.base = base
        self.abi_forzado = abi_forzado
        self.venv_de = {}  # venv -> tag del intérprete que lo creó
        self.llamadas = []

    @staticmethod
    def tag_interprete(python: str) -> str:
        return Path(python).parts[-3]  # /fake/cp312/bin/python -> cp312

    def __call__(self, comando, **kwargs):
        comando = [str(c) for c in comando]
        self.llamadas.append(comando)
        if comando[1:3] == ["-c", "import sys; print(sys.version_info[0], "
                                  "sys.version_info[1])"]:
            minor = self.tag_interprete(comando[0])[3:]
            return subprocess.CompletedProcess(comando, 0, f"3 {minor}\n", "")
        if comando[1:3] == ["-m", "venv"]:
            venv = Path(comando[3])
            assert venv not in self.venv_de, f"venv reutilizado: {venv}"
            venv.mkdir()
            self.venv_de[venv] = self.tag_interprete(comando[0])
            return subprocess.CompletedProcess(comando, 0)
        if comando[1:4] == ["-m", "pip", "wheel"]:
            venv = Path(comando[0]).parents[1]
            tag = self.abi_forzado or self.venv_de[venv]
            destino = Path(comando[comando.index("--wheel-dir") + 1])
            nombre, version = comando[-1].split("==")
            nombre = nombre.replace("-", "_")
            (destino / f"{nombre}-{version}-{tag}-{tag}-linux_x86_64.whl"
             ).write_bytes(tag.encode())
            return subprocess.CompletedProcess(comando, 0)
        if comando[1] == "repair":
            destino = Path(comando[comando.index("--wheel-dir") + 1])
            destino.mkdir()
            crudo = Path(comando[-1])
            reparado = crudo.name.replace(
                "linux_x86_64", "manylinux_2_28_x86_64")
            (destino / reparado).write_bytes(crudo.read_bytes())
            return subprocess.CompletedProcess(comando, 0)
        raise AssertionError(f"comando inesperado: {comando}")


class WheelhouseMultiCPython(unittest.TestCase):
    PYTHONS = ["/fake/cp312/bin/python", "/fake/cp313/bin/python",
               "/fake/cp314/bin/python"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.constraints = self.base / "constraints.txt"
        self.constraints.write_text(
            "evdev==1.9.3\nwebrtcvad-wheels==2.0.14\nnumpy==2.5.1\n")
        self.salida = self.base / "wheelhouse"

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def requiere(nombre, _version, minor):
        # evdev nunca tiene wheel; webrtcvad sólo falta en cp314.
        return nombre == "evdev" or (nombre == "webrtcvad-wheels" and minor == 14)

    def construir(self, falso, pythons=None):
        with mock.patch.object(bw.subprocess, "run", side_effect=falso), \
                mock.patch.object(bw, "requiere_compilacion", self.requiere), \
                mock.patch.object(bw.tempfile, "TemporaryDirectory",
                                  return_value=_DirFijo(self.base / "staging")), \
                mock.patch.dict(bw.os.environ), mock.patch("sys.stdout"):
            return bw.main([
                "--out", str(self.salida), "--auditwheel", "/fake/auditwheel",
                "--constraints", str(self.constraints),
                *(arg for p in (pythons or self.PYTHONS) for arg in ("--python", p))])

    def test_basename_compartido_produce_staging_separado(self):
        falso = CompiladorFalso(self.base)
        self.assertEqual(self.construir(falso), 0)
        staging = sorted(p.name for p in (self.base / "staging").iterdir())
        self.assertEqual(staging, [
            "crudo-evdev-cp312", "crudo-evdev-cp313", "crudo-evdev-cp314",
            "crudo-webrtcvad-wheels-cp314",
            "reparado-evdev-cp312", "reparado-evdev-cp313",
            "reparado-evdev-cp314", "reparado-webrtcvad-wheels-cp314",
            "venv-cp312", "venv-cp313", "venv-cp314",
        ])
        # Cada venv lo creó su propio intérprete (cp314 lo reutiliza).
        self.assertEqual(
            {p.name: tag for p, tag in falso.venv_de.items()},
            {"venv-cp312": "cp312", "venv-cp313": "cp313", "venv-cp314": "cp314"})

    def test_mismo_paquete_en_varios_cpython_sin_mezclar_abi(self):
        self.assertEqual(self.construir(CompiladorFalso(self.base)), 0)
        ruedas = sorted(p.name for p in self.salida.glob("*.whl"))
        self.assertEqual(ruedas, [
            f"evdev-1.9.3-{t}-{t}-manylinux_2_28_x86_64.whl"
            for t in ("cp312", "cp313", "cp314")
        ] + ["webrtcvad_wheels-2.0.14-cp314-cp314-manylinux_2_28_x86_64.whl"])
        for rueda in self.salida.glob("*.whl"):
            tag = re.search(r"-(cp3\d+)-", rueda.name)[1]
            self.assertEqual(rueda.read_bytes(), tag.encode(), rueda.name)

    def test_manifiesto_separa_por_version(self):
        self.construir(CompiladorFalso(self.base))
        manifiesto = json.loads((self.salida / "wheelhouse.json").read_text())
        self.assertEqual(manifiesto, {
            "3.12": ["evdev-1.9.3-cp312-cp312-manylinux_2_28_x86_64.whl"],
            "3.13": ["evdev-1.9.3-cp313-cp313-manylinux_2_28_x86_64.whl"],
            "3.14": ["evdev-1.9.3-cp314-cp314-manylinux_2_28_x86_64.whl",
                     "webrtcvad_wheels-2.0.14-cp314-cp314-manylinux_2_28_x86_64.whl"],
        })

    def test_wheel_de_otro_abi_se_rechaza(self):
        with self.assertRaisesRegex(SystemExit, "no es un wheel cp313-cp313"):
            self.construir(CompiladorFalso(self.base, abi_forzado="cp312"),
                           pythons=["/fake/cp313/bin/python"])
        self.assertEqual(list(self.salida.glob("*.whl")), [])

    def test_cpython_repetido_se_rechaza(self):
        with self.assertRaisesRegex(SystemExit, "cp312 repetido"):
            self.construir(CompiladorFalso(self.base), pythons=[
                "/fake/cp312/bin/python", "/otro/cp312/bin/python"])


class _DirFijo:
    """TemporaryDirectory conservado para inspeccionar el staging."""

    def __init__(self, ruta: Path):
        self.ruta = ruta

    def __enter__(self):
        self.ruta.mkdir()
        return str(self.ruta)

    def __exit__(self, *_exc):
        return False


if __name__ == "__main__":
    unittest.main()
