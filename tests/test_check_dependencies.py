"""Inventario y auditoría de dependencias (scripts/check_dependencies.py).

Sin red: pip-audit se reemplaza por un ejecutable falso que responde con el
mismo formato JSON. Verifica la clasificación por inventario, que el
resultado quede visible (resumen y anotaciones) y los códigos de salida.
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_dependencies as cd  # noqa: E402

PIP_AUDIT_FALSO = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    modo = os.environ.get("FALSO_MODO", "ok")
    if modo == "roto":
        print("network unreachable", file=sys.stderr)
        sys.exit(2)
    requisitos = args[args.index("-r") + 1]
    salida = args[args.index("-o") + 1]
    avisos = {
        ("anyio", "4.14.1"): [{"id": "PYSEC-1", "aliases": ["CVE-1"],
                               "fix_versions": ["4.14.2"]}],
        ("setuptools", "84.0.0"): [{"id": "GHSA-x", "aliases": [],
                                    "fix_versions": ["84.1.0"]}],
    }
    dependencias = []
    encontrados = False
    for linea in open(requisitos, encoding="utf-8"):
        nombre, version = linea.strip().split("==")
        if nombre == "local-only":
            dependencias.append({"name": nombre, "skip_reason": "no en PyPI"})
            continue
        vulns = avisos.get((nombre, version), [])
        if vulns:
            vulns = vulns + vulns  # pip-audit puede repetir un mismo aviso
            encontrados = True
        dependencias.append({"name": nombre, "version": version,
                             "vulns": vulns})
    with open(salida, "w", encoding="utf-8") as archivo:
        json.dump({"dependencies": dependencias, "fixes": []}, archivo)
    sys.exit(1 if encontrados else 0)
""")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.constraints = self.tmp / "constraints.txt"
        self.constraints.write_text(
            "# comentario\nanyio==4.14.1\nCertifi==2026.6.17\n"
            "webrtcvad-wheels==2.0.14\nnvidia-cublas-cu12==12.9.2.10\n",
            encoding="utf-8")
        self.pyproject = self.tmp / "pyproject.toml"
        self.pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "ParlAR"
            [project.optional-dependencies]
            vad = ["webrtcvad-wheels==2.0.14"]
            cuda = ["nvidia-cublas-cu12==12.9.2.10", "sin-pin>=1"]
        """), encoding="utf-8")
        self.resumen = self.tmp / "summary.md"
        entorno = {"GITHUB_STEP_SUMMARY": str(self.resumen),
                   "GITHUB_ACTIONS": "true"}
        parche = mock.patch.dict(os.environ, entorno)
        parche.start()
        self.addCleanup(parche.stop)

    def ejecutar(self, *argv):
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida), \
                contextlib.redirect_stderr(io.StringIO()):
            codigo = cd.main(list(argv))
        return codigo, salida.getvalue()


class Lectura(unittest.TestCase):
    def test_constraints_exige_version_exacta(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "c.txt"
            ruta.write_text("numpy>=1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "versión exacta"):
                cd.leer_constraints(ruta)

    def test_constraints_reales_normalizados(self):
        pins = cd.leer_constraints(ROOT / "constraints.txt")
        self.assertIn("typing-extensions", pins)
        self.assertIn("huggingface-hub", pins)

    def test_extras_del_pyproject_real(self):
        extras = cd.nombres_extras(ROOT / "pyproject.toml")
        self.assertIn("webrtcvad-wheels", extras)
        self.assertIn("nvidia-cudnn-cu12", extras)

    def test_pin_de_build(self):
        self.assertEqual(cd.leer_pin("Setuptools==84.0.0"),
                         ("setuptools", "84.0.0"))
        with self.assertRaises(ValueError):
            cd.leer_pin("setuptools>=84")

    def test_instalados_en_otro_interprete(self):
        # ``-I`` excluye el cwd: el egg-info del checkout sólo aparece en
        # el listado en proceso.
        externo = cd.instalados(sys.executable)
        local = cd.instalados()
        externo.pop(cd.PROYECTO, None)
        local.pop(cd.PROYECTO, None)
        self.assertEqual(externo, local)


class Entorno(Base):
    def test_falla_si_una_version_difiere(self):
        instalado = {"anyio": "4.14.1", "certifi": "2026.10.23",
                     "pip": "26.2.2", "parlar": "1.1.0"}
        with mock.patch.object(cd, "instalados", return_value=instalado):
            codigo, salida = self.ejecutar(
                "entorno", "--constraints", str(self.constraints))
        self.assertEqual(codigo, 1)
        self.assertIn("| certifi | 2026.10.23 | 2026.6.17 |", salida)
        self.assertIn("::error title=constraints.txt::certifi", salida)
        self.assertIn("`pip==26.2.2`", salida)
        self.assertNotIn("parlar==", salida)
        self.assertIn("Distintas de constraints.txt: 1",
                      self.resumen.read_text(encoding="utf-8"))

    def test_informativo_avisa_sin_fallar(self):
        instalado = {"anyio": "4.15.0"}
        with mock.patch.object(cd, "instalados", return_value=instalado):
            codigo, salida = self.ejecutar(
                "entorno", "--constraints", str(self.constraints),
                "--informativo")
        self.assertEqual(codigo, 0)
        self.assertIn("::warning title=constraints.txt::anyio", salida)

    def test_coincidencia_exacta(self):
        instalado = {"anyio": "4.14.1", "certifi": "2026.6.17"}
        with mock.patch.object(cd, "instalados", return_value=instalado):
            codigo, _ = self.ejecutar(
                "entorno", "--constraints", str(self.constraints))
        self.assertEqual(codigo, 0)


class Auditar(Base):
    def setUp(self):
        super().setUp()
        self.pip_audit = self.tmp / "pip-audit"
        self.pip_audit.write_text(
            f"#!{sys.executable}\n" + PIP_AUDIT_FALSO, encoding="utf-8")
        self.pip_audit.chmod(self.pip_audit.stat().st_mode | stat.S_IXUSR)
        self.salida = self.tmp / "reporte"
        self.runtime = {"anyio": "4.14.1", "pip": "26.2.2",
                        "parlar": "1.1.0", "local-only": "0"}

    def auditar(self, *extra, modo="ok"):
        with mock.patch.object(cd, "instalados", return_value=self.runtime), \
                mock.patch.dict(os.environ, {"FALSO_MODO": modo}):
            return self.ejecutar(
                "auditar", "--constraints", str(self.constraints),
                "--pyproject", str(self.pyproject),
                "--runtime-python", "/falso/python",
                "--build", "setuptools==84.0.0", "--build", "wheel==0.48.0",
                "--pip-audit", str(self.pip_audit),
                "--salida", str(self.salida), *extra)

    def test_informativo_expone_hallazgos_sin_fallar(self):
        codigo, salida = self.auditar()
        self.assertEqual(codigo, 0)
        resumen = self.resumen.read_text(encoding="utf-8")
        self.assertIn("pip-audit (informativo)", resumen)
        self.assertIn("| anyio | 4.14.1 | PYSEC-1 | CVE-1 | 4.14.2 | "
                      "constraints, runtime |", resumen)
        self.assertIn("| setuptools | 84.0.0 | GHSA-x | - | 84.1.0 | build |",
                      resumen)
        self.assertIn("no implica que el aviso sea explotable", resumen)
        self.assertIn("local-only: no en PyPI", resumen)
        self.assertIn("::warning title=pip-audit: anyio 4.14.1::1 aviso "
                      "(PYSEC-1)", salida)
        self.assertIn("::warning title=pip-audit: setuptools 84.0.0::", salida)

    def test_reporte_json_clasifica_inventarios(self):
        self.auditar()
        reporte = json.loads((self.salida / "reporte.json").read_text(
            encoding="utf-8"))
        self.assertEqual(set(reporte["inventarios"]),
                         {"constraints", "runtime", "extras", "build"})
        self.assertNotIn("parlar", reporte["inventarios"]["runtime"]["paquetes"])
        self.assertEqual(reporte["inventarios"]["extras"]["paquetes"],
                         {"nvidia-cublas-cu12": "12.9.2.10",
                          "webrtcvad-wheels": "2.0.14"})
        hallazgos = {(h["paquete"], h["aviso"]): h["inventarios"]
                     for h in reporte["hallazgos"]}
        # Un aviso repetido por pip-audit se registra una sola vez.
        self.assertEqual(len(reporte["hallazgos"]), 2)
        self.assertEqual(hallazgos[("anyio", "PYSEC-1")],
                         ["constraints", "runtime"])
        self.assertEqual(hallazgos[("setuptools", "GHSA-x")], ["build"])

    def test_estricto_falla_con_hallazgos(self):
        codigo, salida = self.auditar("--estricto")
        self.assertEqual(codigo, 1)
        self.assertIn("::error title=pip-audit: anyio 4.14.1::", salida)

    def test_error_de_ejecucion_es_visible_y_falla(self):
        codigo, salida = self.auditar(modo="roto")
        self.assertEqual(codigo, 2)
        resumen = self.resumen.read_text(encoding="utf-8")
        self.assertIn("error: pip-audit terminó con 2", resumen)
        self.assertIn("network unreachable", resumen)
        self.assertIn("::error title=pip-audit::no se pudo auditar", salida)

    def test_sin_hallazgos(self):
        self.constraints.write_text("certifi==2026.10.23\n", encoding="utf-8")
        self.runtime = {"certifi": "2026.10.23"}
        with mock.patch.object(cd, "instalados", return_value=self.runtime):
            codigo, _ = self.ejecutar(
                "auditar", "--constraints", str(self.constraints),
                "--pyproject", str(self.pyproject),
                "--runtime-python", "/falso/python",
                "--pip-audit", str(self.pip_audit),
                "--salida", str(self.salida), "--estricto")
        self.assertEqual(codigo, 0)
        self.assertIn("Sin avisos conocidos",
                      self.resumen.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
