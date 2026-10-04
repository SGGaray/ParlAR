"""Identidad de ParlAR (Phase 9C): assets vectoriales, tray y escritorio.

Sin runtime: sólo archivos, el mapeo de estados del tray y, si hay
display, un render chico con el soporte SVG de Tk.
"""

import fnmatch
import re
import tomllib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from parlar.tray_gtk import icono_tray_para

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "parlar" / "assets"
SVG_NS = "{http://www.w3.org/2000/svg}"

REQUERIDOS = (
    "parlar.svg",
    "brand/parlar-mono.svg",
    "brand/parlar-16.svg",
    "tray/parlar-tray.svg",
    "tray/parlar-tray-activo.svg",
    "tray/parlar-tray-atencion.svg",
)


def svgs():
    return sorted(ASSETS.rglob("*.svg"))


class PruebasArchivos(unittest.TestCase):
    def test_requeridos_existen(self):
        for relativo in REQUERIDOS:
            with self.subTest(relativo=relativo):
                self.assertTrue((ASSETS / relativo).is_file())

    def test_svg_validos_vectoriales_y_autonomos(self):
        self.assertGreaterEqual(len(svgs()), len(REQUERIDOS))
        for ruta in svgs():
            with self.subTest(svg=ruta.name):
                texto = ruta.read_text(encoding="utf-8")
                raiz = ET.fromstring(texto.split("-->", 1)[-1]
                                     if texto.startswith("<!--") else texto)
                self.assertEqual(raiz.tag, SVG_NS + "svg")
                caja = [float(v) for v in raiz.get("viewBox", "").split()]
                self.assertEqual(len(caja), 4)
                self.assertEqual(caja[:2], [0.0, 0.0])
                self.assertGreater(caja[2], 0)
                self.assertEqual(caja[2], caja[3])          # caja cuadrada
                etiquetas = {e.tag.replace(SVG_NS, "") for e in raiz.iter()}
                for prohibida in ("image", "text", "font", "filter",
                                  "linearGradient", "radialGradient",
                                  "script", "foreignObject"):
                    self.assertNotIn(prohibida, etiquetas)
                self.assertNotRegex(texto, r"@font-face|data:|https?://(?!www\.w3\.org)")
                self.assertIsNotNone(raiz.find(SVG_NS + "title"))

    def test_mono_usa_currentcolor_y_tray_sin_baldosa(self):
        mono = (ASSETS / "brand/parlar-mono.svg").read_text(encoding="utf-8")
        self.assertIn("currentColor", mono)
        self.assertNotRegex(mono, r"#[0-9A-Fa-f]{6}")
        for nombre in ("parlar-tray.svg", "parlar-tray-activo.svg",
                       "parlar-tray-atencion.svg"):
            raiz = ET.parse(ASSETS / "tray" / nombre).getroot()
            primeros = [e for e in raiz if e.tag != SVG_NS + "title"]
            # Sin baldosa de fondo: el primer elemento no cubre la caja.
            self.assertFalse(primeros[0].get("width") == "16"
                             and primeros[0].get("height") == "16")

    def test_master_16_cae_en_pixeles_enteros(self):
        raiz = ET.parse(ASSETS / "brand/parlar-16.svg").getroot()
        for rect in raiz.iter(SVG_NS + "rect"):
            for atributo in ("x", "y", "width", "height"):
                valor = float(rect.get(atributo, "0"))
                self.assertEqual(valor, round(valor), (atributo, valor))

    def test_icono_de_app_conserva_marca_del_instalador(self):
        from scripts.render_desktop import MARCA_ICONO, cargar_icono
        contenido = cargar_icono(ASSETS / "parlar.svg")
        self.assertTrue(contenido.startswith(MARCA_ICONO))

    def test_package_data_incluye_todos_los_svg(self):
        datos = tomllib.loads((ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))
        patrones = datos["tool"]["setuptools"]["package-data"]["parlar"]
        for ruta in svgs():
            relativo = ruta.relative_to(ROOT / "parlar").as_posix()
            with self.subTest(relativo=relativo):
                self.assertTrue(any(fnmatch.fnmatch(relativo, p)
                                    for p in patrones))

    def test_desktop_entry_apunta_al_icono_instalado(self):
        plantilla = (ROOT / "scripts/parlar.desktop.in").read_text(
            encoding="utf-8")
        self.assertRegex(plantilla, re.compile(r"^Icon=parlar$", re.M))
        instalador = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("parlar/assets/parlar.svg", instalador)


class PruebasTray(unittest.TestCase):
    def test_cada_estado_real_tiene_icono_existente(self):
        app = ASSETS / "parlar.svg"
        esperados = {
            "available": "parlar-tray.svg",
            "paused": "parlar-tray.svg",
            "busy": "parlar-tray-activo.svg",
            "attention": "parlar-tray-atencion.svg",
            "unavailable": "parlar-tray-atencion.svg",
        }
        for categoria, nombre in esperados.items():
            with self.subTest(categoria=categoria):
                ruta = icono_tray_para(categoria, app)
                self.assertEqual(ruta.name, nombre)
                self.assertTrue(ruta.is_file())

    def test_sin_variantes_cae_al_icono_de_app(self):
        falso = Path("/no/existe/assets/parlar.svg")
        self.assertEqual(icono_tray_para("busy", falso), falso)


try:
    import tkinter as tk
    _r = tk.Tk()
    _r.destroy()
    HAY_DISPLAY = True
except Exception:
    HAY_DISPLAY = False


@unittest.skipUnless(HAY_DISPLAY, "Tk necesita un display")
class PruebasRender(unittest.TestCase):
    def test_render_chico_con_tk(self):
        root = tk.Tk()
        try:
            for ruta in svgs():
                for lado in (16, 24, 32):
                    with self.subTest(svg=ruta.name, lado=lado):
                        imagen = tk.PhotoImage(
                            master=root, file=str(ruta),
                            format=f"svg -scaletoheight {lado}")
                        self.assertEqual(imagen.height(), lado)
                        # Algo visible: al menos un píxel no transparente.
                        visibles = sum(
                            1 for x in range(lado) for y in range(lado)
                            if not imagen.transparency_get(x, y))
                        self.assertGreater(visibles, lado)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
