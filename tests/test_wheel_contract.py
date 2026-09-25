"""Regresiones del contrato CI para el wheel instalable."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.check_wheel_contract import ContractError, validar_wheel


ROOT = Path(__file__).resolve().parents[1]


class ContratoWheel(unittest.TestCase):
    def _crear_wheel(self, directorio, *, omitir=(), version_metadata="1.0.1"):
        wheel = directorio / "parlar-1.0.1-py3-none-any.whl"
        dist_info = "parlar-1.0.1.dist-info"
        miembros = {
            str(ruta.relative_to(ROOT)): ruta.read_bytes()
            for ruta in (ROOT / "parlar").glob("*.py")
        }
        miembros.update({
            f"{dist_info}/METADATA": (
                "Metadata-Version: 2.4\n"
                "Name: ParlAR\n"
                f"Version: {version_metadata}\n"
                "Requires-Python: >=3.12\n"
                "License-Expression: MIT\n"
                "License-File: LICENSE\n"
            ).encode(),
            f"{dist_info}/entry_points.txt": (
                "[console_scripts]\n"
                "parlar = parlar.__main__:main\n"
                "parlarctl = parlar.control:parlarctl_main\n"
            ).encode(),
            f"{dist_info}/licenses/LICENSE":
                (ROOT / "LICENSE").read_bytes(),
        })
        with zipfile.ZipFile(wheel, "w") as archivo:
            for nombre, contenido in miembros.items():
                if nombre not in omitir:
                    archivo.writestr(nombre, contenido)
        return wheel

    def test_acepta_wheel_con_contrato_completo(self):
        with tempfile.TemporaryDirectory() as tmp:
            directorio = Path(tmp)
            wheel = self._crear_wheel(directorio)

            self.assertEqual(
                validar_wheel(directorio, ROOT / "pyproject.toml"),
                wheel,
            )

    def test_rechaza_modulo_critico_ausente(self):
        with tempfile.TemporaryDirectory() as tmp:
            directorio = Path(tmp)
            self._crear_wheel(directorio, omitir={"parlar/__main__.py"})

            with self.assertRaisesRegex(ContractError, "parlar/__main__.py"):
                validar_wheel(directorio, ROOT / "pyproject.toml")

    def test_rechaza_metadata_incoherente(self):
        with tempfile.TemporaryDirectory() as tmp:
            directorio = Path(tmp)
            self._crear_wheel(directorio, version_metadata="9.9.9")

            with self.assertRaisesRegex(ContractError, "Version"):
                validar_wheel(directorio, ROOT / "pyproject.toml")

    def test_rechaza_license_ausente(self):
        with tempfile.TemporaryDirectory() as tmp:
            directorio = Path(tmp)
            self._crear_wheel(
                directorio,
                omitir={"parlar-1.0.1.dist-info/licenses/LICENSE"},
            )

            with self.assertRaisesRegex(ContractError, "LICENSE"):
                validar_wheel(directorio, ROOT / "pyproject.toml")

    def test_rechaza_entrypoints_ausentes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directorio = Path(tmp)
            self._crear_wheel(
                directorio,
                omitir={"parlar-1.0.1.dist-info/entry_points.txt"},
            )

            with self.assertRaisesRegex(ContractError, "entry_points.txt"):
                validar_wheel(directorio, ROOT / "pyproject.toml")


class ContratoWorkflow(unittest.TestCase):
    def test_ci_cubre_gate_y_wheel_instalado(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text()

        self.assertIn("contents: read", workflow)
        self.assertIn('python-version: ["3.12", "3.14"]', workflow)
        self.assertIn("setuptools==84.0.0 wheel==0.48.0", workflow)
        self.assertIn("python -m pip wheel", workflow)
        self.assertIn("git show -s --format=%ct HEAD", workflow)
        self.assertIn('wheel "$WHEEL_DIR" pyproject.toml', workflow)
        self.assertIn(
            'installed "$GITHUB_WORKSPACE/pyproject.toml"',
            workflow,
        )
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("packages: write", workflow)
        self.assertNotIn("id-token: write", workflow)


if __name__ == "__main__":
    unittest.main()
