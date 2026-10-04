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
        # Checkout: ícono del paquete; release: la copia en share/.
        instalador = (ROOT / "scripts/parlar_installer.py").read_text(
            encoding="utf-8")
        self.assertIn('"parlar" / "assets" / "parlar.svg"', instalador)
        self.assertIn('share / "parlar.svg"', instalador)
        build = (ROOT / "scripts/build_release.py").read_text(encoding="utf-8")
        self.assertIn('("parlar/assets/parlar.svg", "parlar.svg")', build)


def _rects(ruta):
    raiz = ET.parse(ruta).getroot()
    return [tuple(float(r.get(a, "0")) for a in ("x", "y", "width", "height"))
            for r in raiz.iter(SVG_NS + "rect")]


class PruebasTray(unittest.TestCase):
    ESTADOS = ("parlar-tray.svg", "parlar-tray-activo.svg",
               "parlar-tray-atencion.svg")

    def test_tray_deriva_del_mark_y_estados_comparten_silueta(self):
        # Máster de 16 sin la baldosa = barras de voz + cursor.
        master = [r for r in _rects(ASSETS / "brand/parlar-16.svg")
                  if r[2] != 16]
        barras_master, cursor_master = master[:3], master[3]
        for nombre in self.ESTADOS:
            with self.subTest(estado=nombre):
                rects = _rects(ASSETS / "tray" / nombre)
                self.assertEqual(rects[:3], barras_master)   # ritmo 2/6/4
                cursor = rects[3:]
                # La columna del cursor ocupa el mismo lugar y alto total.
                self.assertTrue(all(r[0] == cursor_master[0]
                                    and r[2] == cursor_master[2]
                                    for r in cursor))
                self.assertEqual(min(r[1] for r in cursor), cursor_master[1])
                self.assertEqual(max(r[1] + r[3] for r in cursor),
                                 cursor_master[1] + cursor_master[3])

    def test_estados_distinguibles_por_acento(self):
        def acento(nombre):
            raiz = ET.parse(ASSETS / "tray" / nombre).getroot()
            colores = re.findall(r'fill="(#[0-9A-Fa-f]{6})"',
                                 ET.tostring(raiz, encoding="unicode"))
            return colores[-1].upper()
        acentos = {nombre: acento(nombre) for nombre in self.ESTADOS}
        self.assertEqual(len(set(acentos.values())), 3, acentos)

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
                for lado in (16, 20, 24, 32):
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


@unittest.skipUnless(HAY_DISPLAY, "Tk necesita un display")
class PruebasSettingsMarca(unittest.TestCase):
    def ventana(self, escala=1.0):
        from unittest import mock
        from parlar.config import Config
        from parlar.settings_backend import (
            EstadoParlARSettings, obtener_capacidades, snapshot_configuracion)
        from parlar.settings_window import ControlSettings, VentanaSettings
        root = tk.Tk()
        self.addCleanup(root.destroy)
        # ``tk scaling`` persiste para el display en todo el proceso:
        # restaurarlo antes de destruir para no escalar tests posteriores.
        original = root.tk.call("tk", "scaling")
        self.addCleanup(root.tk.call, "tk", "scaling", original)
        root.tk.call("tk", "scaling", escala * 96 / 72)
        prueba = mock.Mock()
        prueba.estado.return_value = mock.Mock(fase="idle", error=None, nivel=0.0)
        auto = mock.Mock()
        auto.estado = mock.Mock(activado=False, mensaje="", modificable=True,
                                estado="disabled")
        estado = EstadoParlARSettings("ready", "Listo", "Listo.", True, None)
        cfg = Config()
        return VentanaSettings(
            root, ControlSettings(cfg, snapshot_configuracion(cfg)),
            obtener_capacidades(), control_prueba=prueba,
            estado_parlar=estado, consultar_estado=lambda: estado,
            permitir_foco_inicial=False, control_autostart=auto,
            listar_entradas=lambda: ())

    def test_header_muestra_el_app_mark_y_la_ventana_su_icono(self):
        v = self.ventana()
        self.assertIsNotNone(v.marca_header)
        self.assertEqual(v.marca_header.height(), v._px(28))
        self.assertEqual(len(v._iconos_ventana), 2)        # 16 + 64
        fuente = Path(__file__).resolve().parents[1] / "parlar/settings_window.py"
        self.assertIn('"parlar.svg"', fuente.read_text(encoding="utf-8"))

    def test_hidpi_rasteriza_la_marca_a_densidad_real(self):
        normal = self.ventana(1.0)
        doble = self.ventana(2.0)
        self.assertEqual(doble.marca_header.height(),
                         2 * normal.marca_header.height())


if __name__ == "__main__":
    unittest.main(verbosity=2)
