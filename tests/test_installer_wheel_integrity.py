"""Integridad y selección del wheel de ParlAR en una instalación desde release.

El instalador referencia el wheel incluido en el release por ruta y SHA-256.
Estas pruebas no usan red ni instalan nada: las de pip corren ``pip download``
contra un índice ``file://`` y ``--find-links`` locales con wheels sintéticos.
"""

import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import parlar_installer as pi  # noqa: E402

VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
WHEEL = f"parlar-{VERSION}-py3-none-any.whl"


def sha256(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def crear_wheel(directorio: Path, nombre_archivo: str, nombre: str,
                version: str, resumen: str, requisitos: str = "") -> Path:
    """Wheel mínimo y válido para pip: sólo metadata, sin código."""
    directorio.mkdir(parents=True, exist_ok=True)
    dist = f"{nombre.replace('-', '_')}-{version}.dist-info"
    archivos = {
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\nName: {nombre}\nVersion: {version}\n"
            f"Summary: {resumen}\n{requisitos}"),
        f"{dist}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: prueba\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"),
    }
    registro = ""
    for ruta, contenido in archivos.items():
        resumen_hash = base64.urlsafe_b64encode(
            hashlib.sha256(contenido.encode()).digest()).rstrip(b"=").decode()
        registro += f"{ruta},sha256={resumen_hash},{len(contenido)}\n"
    archivos[f"{dist}/RECORD"] = registro + f"{dist}/RECORD,,\n"
    destino = directorio / nombre_archivo
    with zipfile.ZipFile(destino, "w") as archivo:
        for ruta, contenido in archivos.items():
            archivo.writestr(ruta, contenido)
    return destino


def crear_release(raiz: Path, resumen: str = "release",
                  requisitos: str = "") -> Path:
    """Release extraído mínimo con wheel y referencia como build_release.py."""
    (raiz / "share").mkdir(parents=True)
    (raiz / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    wheel = crear_wheel(raiz / "wheels", WHEEL, "parlar", VERSION, resumen,
                        requisitos)
    (raiz / "share" / pi.REFERENCIA_WHEEL).write_text(
        f"{sha256(wheel)}  {WHEEL}\n", encoding="utf-8")
    return wheel


class ReferenciaDelWheel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name) / "release con espacios ñ"
        self.wheel = crear_release(self.raiz)
        self.referencia = self.raiz / "share" / pi.REFERENCIA_WHEEL

    def tearDown(self):
        self._tmp.cleanup()

    def test_release_valido_referencia_ruta_y_hash(self):
        origen = pi.detectar_origen(self.raiz)
        esperado = sha256(self.wheel)
        self.assertEqual(origen.wheel_sha256, esperado)
        uri = self.wheel.resolve().as_uri()
        self.assertEqual(origen.objetivo(), f"parlar @ {uri}#sha256={esperado}")
        self.assertEqual(origen.objetivo("[cuda]"),
                         f"parlar[cuda] @ {uri}#sha256={esperado}")

    def test_wheel_modificado_se_rechaza(self):
        with zipfile.ZipFile(self.wheel, "a") as archivo:
            archivo.writestr("extra.txt", "x")
        with self.assertRaisesRegex(pi.ErrorInstalacion, "no coincide"):
            pi.detectar_origen(self.raiz)

    def test_referencia_ausente_se_rechaza(self):
        self.referencia.unlink()
        with self.assertRaisesRegex(pi.ErrorInstalacion, "referencia válida"):
            pi.detectar_origen(self.raiz)

    def test_referencias_malformadas_se_rechazan(self):
        hash_real = sha256(self.wheel)
        casos = (
            "",
            f"{hash_real} {WHEEL}\n",
            f"{hash_real.upper()}  {WHEEL}\n",
            f"{hash_real[:-1]}  {WHEEL}\n",
            f"{hash_real}  otro-1.0-py3-none-any.whl\n",
            f"{hash_real}  ../wheels/{WHEEL}\n",
            f"{hash_real}  {WHEEL}\n{hash_real}  {WHEEL}\n",
        )
        for contenido in casos:
            with self.subTest(contenido=contenido):
                self.referencia.write_text(contenido, encoding="utf-8")
                with self.assertRaisesRegex(pi.ErrorInstalacion, "inválida"):
                    pi.detectar_origen(self.raiz)

    def test_referencia_no_utf8_se_rechaza(self):
        self.referencia.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(pi.ErrorInstalacion, "referencia válida"):
            pi.detectar_origen(self.raiz)

    def test_origen_release_sin_wheel_verificado_no_produce_objetivo(self):
        origen = pi.Origen(
            modo="release", raiz=self.raiz, version=VERSION,
            share=self.raiz / "share", icono=self.raiz / "x",
            constraints=self.raiz / "constraints.txt",
            uninstall=self.raiz / "uninstall.sh", wheels=self.raiz / "wheels")
        with self.assertRaises(pi.ErrorInstalacion):
            origen.objetivo()

    def test_checkout_sigue_instalando_desde_el_arbol(self):
        origen = pi.detectar_origen(ROOT)
        self.assertEqual(origen.modo, "checkout")
        self.assertEqual(origen.objetivo(), str(ROOT))
        self.assertEqual(origen.objetivo("[cuda]"), f"{ROOT}[cuda]")


@unittest.skipUnless(importlib.util.find_spec("pip"), "requiere pip")
class SeleccionConPipReal(unittest.TestCase):
    """pip real, sin red: el objetivo del release no admite sustitutos."""

    VARIANTES_AJENAS = (
        WHEEL,
        f"parlar-{VERSION}-1-py3-none-any.whl",
        f"parlar-{VERSION}-py{sys.version_info[0]}{sys.version_info[1]}"
        "-none-any.whl",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.raiz = self.base / "release"
        self.wheel = crear_release(
            self.raiz, resumen="LEGITIMO",
            requisitos='Provides-Extra: cuda\n'
                       'Requires-Dist: dependencia-cuda; extra == "cuda"\n')
        crear_wheel(self.raiz / "wheels", "dependencia_cuda-2.0-py3-none-any.whl",
                    "dependencia-cuda", "2.0", "dependencia")
        crear_wheel(self.raiz / "wheels", "dependencia_cuda-2.1-py3-none-any.whl",
                    "dependencia-cuda", "2.1", "dependencia")
        self.constraints = self.base / "constraints.txt"
        self.constraints.write_text("dependencia-cuda==2.0\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def indice_con(self, nombre_archivo: str) -> Path:
        indice = self.base / f"indice-{len(list(self.base.iterdir()))}"
        proyecto = indice / "parlar"
        crear_wheel(proyecto, nombre_archivo, "parlar", VERSION, "AJENO")
        (proyecto / "index.html").write_text(
            f'<html><body><a href="{nombre_archivo}">{nombre_archivo}</a>'
            "</body></html>\n", encoding="utf-8")
        return indice

    def descargar(self, objetivo: str, indice: Path) -> tuple[int, Path]:
        destino = self.base / f"descarga-{len(list(self.base.iterdir()))}"
        entorno = dict(os.environ, PIP_CONFIG_FILE=os.devnull,
                       PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        for variable in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
                         "PIP_FIND_LINKS", "PIP_NO_INDEX"):
            entorno.pop(variable, None)
        resultado = subprocess.run(
            [sys.executable, "-m", "pip", "download", "--isolated",
             "--no-cache-dir", "--only-binary=:all:",
             "--index-url", indice.as_uri(),
             "--find-links", str(self.raiz / "wheels"),
             "-c", str(self.constraints), "-d", str(destino), objetivo],
            env=entorno, capture_output=True, text=True, timeout=120,
            check=False)
        return resultado.returncode, destino

    def test_indice_con_mismo_nombre_y_version_no_sustituye_el_wheel(self):
        origen = pi.detectar_origen(self.raiz)
        for variante in self.VARIANTES_AJENAS:
            with self.subTest(variante=variante):
                codigo, destino = self.descargar(
                    origen.objetivo(), self.indice_con(variante))
                self.assertEqual(codigo, 0)
                descargados = sorted(p.name for p in destino.iterdir())
                self.assertEqual(descargados, [WHEEL])
                self.assertEqual(sha256(destino / WHEEL), sha256(self.wheel))

    def test_extra_cuda_respeta_wheel_y_constraints(self):
        origen = pi.detectar_origen(self.raiz)
        codigo, destino = self.descargar(
            origen.objetivo("[cuda]"), self.indice_con(self.VARIANTES_AJENAS[1]))
        self.assertEqual(codigo, 0)
        self.assertEqual(sorted(p.name for p in destino.iterdir()),
                         ["dependencia_cuda-2.0-py3-none-any.whl", WHEEL])
        self.assertEqual(sha256(destino / WHEEL), sha256(self.wheel))

    def test_pip_rechaza_un_wheel_alterado_despues_de_verificar(self):
        origen = pi.detectar_origen(self.raiz)
        with zipfile.ZipFile(self.wheel, "a") as archivo:
            archivo.writestr("extra.txt", "x")
        codigo, destino = self.descargar(
            origen.objetivo(), self.indice_con(WHEEL))
        self.assertNotEqual(codigo, 0)
        self.assertFalse(destino.exists() and any(destino.iterdir()))


if __name__ == "__main__":
    unittest.main()
