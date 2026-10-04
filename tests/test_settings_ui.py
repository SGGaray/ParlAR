"""Vista de Settings con Tk real (Phase 9A): estructura, estados y teclado.

Sin runtime: STT, audio, hotkeys e IPC son fakes. Si no hay display, la
suite se omite (el gate no exige pantalla). Las ventanas se mapean fuera de
la pantalla para poder medir foco y geometría sin molestar.
"""

import os
import time
import unittest
from unittest import mock

from parlar.config import Config
from parlar.settings_backend import (
    EstadoParlARSettings,
    ResultadoPersistencia,
    obtener_capacidades,
    snapshot_configuracion,
)
from parlar.settings_window import (
    ARQUITECTURA_SETTINGS,
    ControlSettings,
    VentanaSettings,
)
from parlar.ui_tema import Interruptor, Tema

try:
    import tkinter as tk
    _raiz = tk.Tk()
    _raiz.destroy()
    HAY_DISPLAY = True
except Exception:
    HAY_DISPLAY = False


LISTO = EstadoParlARSettings(
    "ready", "Listo", "ParlAR está listo para dictar.", True, "connected")
DESCONECTADO = EstadoParlARSettings(
    "ready", "Listo", "ParlAR está listo para dictar.", True, "disconnected")
ATENCION = EstadoParlARSettings(
    "attention", "Requiere atención", "Revisá el micrófono.", True, None)


def prueba_falsa():
    prueba = mock.Mock()
    prueba.estado.return_value = mock.Mock(
        fase="idle", error=None, nivel=0.0)
    return prueba


def autostart_falso():
    auto = mock.Mock()
    auto.estado = mock.Mock(activado=False, mensaje="Desactivado.",
                            modificable=True, estado="disabled")
    return auto


@unittest.skipUnless(HAY_DISPLAY, "Tk necesita un display")
class Base(unittest.TestCase):
    def setUp(self):
        self.raices = []
        self.escalas = []

    def tearDown(self):
        for root, original in getattr(self, "escalas", []):
            try:
                root.tk.call("tk", "scaling", original)
            except Exception:
                pass
        for root in self.raices:
            try:
                root.destroy()
            except Exception:
                pass

    def ventana(self, cfg=None, *, estado=LISTO, persistir=None,
                advertencia=None, escala=None, mapear=True, geometria=None):
        cfg = cfg or Config()
        root = tk.Tk()
        self.raices.append(root)
        if escala:
            # ``tk scaling`` persiste para el display en todo el proceso.
            self.escalas.append((root, root.tk.call("tk", "scaling")))
            root.tk.call("tk", "scaling", escala * 96 / 72)
        self.guardados = []

        def persistir_falso(base, snapshot):
            self.guardados.append(snapshot)
            return ResultadoPersistencia(
                snapshot, requires_restart=snapshot_configuracion(base)
                != snapshot and _requiere(base, snapshot))

        from parlar.settings_backend import requiere_reinicio

        def _requiere(base, snapshot):
            return requiere_reinicio(snapshot_configuracion(base), snapshot)

        control = ControlSettings(
            cfg, snapshot_configuracion(cfg),
            persistir=persistir or persistir_falso,
            requiere_reparacion=advertencia is not None)
        self.popen = mock.Mock()
        v = VentanaSettings(
            root, control, obtener_capacidades(),
            control_prueba=prueba_falsa(), estado_parlar=estado,
            consultar_estado=lambda: estado, permitir_foco_inicial=False,
            control_autostart=autostart_falso(), listar_entradas=lambda: (),
            advertencia_config=advertencia, popen=self.popen)
        if mapear:
            root.geometry((geometria or "") + "+4000+4000")
            root.deiconify()
        self.asentar(root)
        return v

    @staticmethod
    def asentar(root, segundos=0.15):
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            root.update()
            time.sleep(0.01)


class PruebasEstructura(Base):
    def test_01_02_03_abre_con_todo_y_valores_iniciales(self):
        cfg = Config(mode="streaming", language="en", overlay=False,
                     guionar_exclusive_output=False)
        v = self.ventana(cfg)
        self.assertEqual(v.root.title(), "Configuración de ParlAR")
        for pestaña in ARQUITECTURA_SETTINGS.pestañas:
            for campo in pestaña.orden_foco:
                with self.subTest(campo=campo):
                    self.assertIn(campo, v._widgets_foco)
        valores = v._valores()
        self.assertEqual(valores.mode, "streaming")
        self.assertEqual(valores.language, "en")
        self.assertFalse(valores.overlay)
        self.assertFalse(valores.guionar_exclusive_output)
        self.assertTrue(valores.guionar)
        self.assertEqual(v.check_overlay._posicion, 0.0)
        self.assertEqual(v.check_guionar._posicion, 1.0)
        self.assertEqual(v.seccion_actual, "Dictado")

    def test_navegacion_lateral_y_atajos_de_seccion(self):
        v = self.ventana()
        v.seleccionar_seccion("GuionAR")
        self.asentar(v.root)
        self.assertTrue(v.check_guionar.winfo_viewable())
        self.assertFalse(v.selector_audio.winfo_viewable())
        self.assertIn("selected", v._navegacion["GuionAR"].state())
        v._ciclar_seccion(1)
        self.assertEqual(v.seccion_actual, "Aplicación")
        v._ciclar_seccion(-2)
        self.assertEqual(v.seccion_actual, "Micrófono")

    def test_tema_tokens_y_sin_ruta_de_socket(self):
        v = self.ventana(Config(guionar_socket="/run/user/1/guionar.sock"))
        textos = []

        def recorrer(widget):
            for hijo in widget.winfo_children():
                try:
                    textos.append(str(hijo.cget("text")))
                except Exception:
                    pass
                recorrer(hijo)

        recorrer(v.root)
        self.assertFalse(any("guionar.sock" in t or "/run/" in t
                             for t in textos))
        self.assertEqual(v._valores().guionar_socket,
                         "/run/user/1/guionar.sock")


class PruebasInteraccion(Base):
    def test_04_toggle_teclado_click_y_animacion_corta(self):
        v = self.ventana()
        v.seleccionar_seccion("Aplicación")
        self.asentar(v.root)
        interruptor = v.check_overlay
        self.assertIsInstance(interruptor, Interruptor)
        interruptor.focus_set()
        interruptor._tecla()
        self.assertFalse(v.variables["overlay"].get())
        self.assertTrue(interruptor.animando)
        self.asentar(v.root, 0.3)
        self.assertFalse(interruptor.animando)       # sin timer residual
        self.assertEqual(interruptor._posicion, 0.0)
        interruptor._click()
        self.assertTrue(v.variables["overlay"].get())

    def test_05_06_07_select_dirty_y_guardar(self):
        v = self.ventana()
        self.assertTrue(v.boton_guardar.instate(["disabled"]))
        self.assertEqual(v.boton_cancelar.cget("text"), "Cerrar")
        selector, opciones = v.selectores["mode"]
        selector.current(next(i for i, o in enumerate(opciones)
                              if o.valor == "streaming"))
        selector.event_generate("<<ComboboxSelected>>")
        v.variables["mode"].set(selector.get())
        self.asentar(v.root)
        self.assertEqual(v._valores().mode, "streaming")
        self.assertTrue(v.boton_guardar.instate(["!disabled"]))
        self.assertEqual(v.boton_cancelar.cget("text"), "Descartar cambios")
        v._guardar()
        self.assertEqual(len(self.guardados), 1)
        self.assertEqual(self.guardados[0].mode, "streaming")
        self.assertEqual(str(v.etiqueta_estado.cget("style")),
                         "Success.Status.TLabel")
        self.assertIn("reinici", v.estado.get().lower())      # 18

    def test_08_cerrar_limpio_y_sucio_sin_dialogo_del_sistema(self):
        v = self.ventana()
        v.variables["guardar_sesion"].set(True)
        v._solicitar_cierre()
        self.assertIsNotNone(v._modal_descarte)               # propio, no SO
        v._seguir_editando()
        self.assertIsNone(v._modal_descarte)
        v.variables["guardar_sesion"].set(False)
        v._solicitar_cierre()
        self.assertTrue(v._cerrando)
        self.popen.assert_not_called()                        # 20 runtime vivo

    def test_09_10_11_19_guionar_estado_toggles_y_salida_en_vivo(self):
        v = self.ventana(estado=DESCONECTADO)
        self.assertEqual(v.estado_guionar.get(), "No detectado")
        self.assertEqual(v.indicador_guionar.categoria, "stopped")
        v._aplicar_estado_parlar(LISTO)
        self.assertEqual(v.estado_guionar.get(), "Conectado")
        self.assertEqual(v.indicador_guionar.categoria, "ready")
        self.assertEqual(str(v.etiqueta_estado_guionar.cget("style")),
                         "Success.Status.TLabel")
        self.assertTrue(v.check_guionar_exclusivo.instate(["!disabled"]))
        v.variables["guionar"].set(False)
        self.asentar(v.root)
        self.assertTrue(v.check_guionar_exclusivo.instate(["disabled"]))
        v.check_guionar_exclusivo._tecla()                    # ignorado
        self.assertTrue(v.variables["guionar_exclusive_output"].get())
        self.assertEqual(
            str(v.check_guionar_exclusivo.lienzo.cget("takefocus")), "0")
        v.variables["guionar"].set(True)
        v.check_guionar_exclusivo._tecla()
        self.assertFalse(v.variables["guionar_exclusive_output"].get())
        v._guardar()
        self.assertIn("No es necesario reiniciar", v.estado.get())

    def test_12_estado_del_runtime_en_el_header(self):
        v = self.ventana(estado=LISTO)
        self.assertEqual(v.estado_runtime_titulo.get(), "Listo")
        self.assertEqual(v.indicador_runtime.categoria, "ready")
        v._aplicar_estado_parlar(ATENCION)
        self.assertEqual(v.estado_runtime_titulo.get(), "Requiere atención")
        self.assertEqual(str(v.etiqueta_runtime_mensaje.cget("style")),
                         "Warning.Header.Status.TLabel")
        self.assertEqual(v.indicador_runtime.categoria, "attention")

    def test_13_17_config_invalida_y_sin_perdida(self):
        cfg = Config(guionar_socket="/x.sock", guionar_explicit=True)
        v = self.ventana(cfg, advertencia="La configuración guardada no "
                         "es válida.")
        self.assertEqual(str(v.etiqueta_estado.cget("style")),
                         "Error.Status.TLabel")
        self.assertTrue(v.boton_guardar.instate(["!disabled"]))
        v._guardar()
        guardado = self.guardados[0]
        self.assertEqual(guardado, snapshot_configuracion(cfg))


class PruebasTecladoYGeometria(Base):
    def test_14_recorrido_de_foco_completo(self):
        v = self.ventana()
        for seccion, pestaña in zip(
                [p.nombre for p in ARQUITECTURA_SETTINGS.pestañas],
                ARQUITECTURA_SETTINGS.pestañas):
            with self.subTest(seccion=seccion):
                v.seleccionar_seccion(seccion)
                self.asentar(v.root, 0.05)
                widget = v._navegacion[seccion]
                visitados = []
                for _ in range(40):
                    widget = widget.tk_focusNext()
                    if widget is None or widget in visitados:
                        break
                    visitados.append(widget)
                esperados = [
                    v._widgets_foco[c] for c in pestaña.orden_foco
                    if not hasattr(v._widgets_foco[c], "instate")
                    or v._widgets_foco[c].instate(["!disabled"])]
                lienzos = [getattr(w, "lienzo", w) for w in esperados]
                for esperado in lienzos:
                    self.assertIn(esperado, visitados)
                self.assertIn(v.boton_cancelar, visitados)
                # Orden: los controles de la sección, en su orden lógico.
                posiciones = [visitados.index(w) for w in lienzos]
                self.assertEqual(posiciones, sorted(posiciones))

    def esperar_ancho(self, root, ancho, timeout=3.0):
        """El WM aplica la geometría de forma asíncrona (más lento con la
        suite completa en paralelo): esperar al ancho real, no a un tiempo."""
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            self.asentar(root, 0.05)
            if abs(root.winfo_width() - ancho) <= 2:
                self.asentar(root, 0.1)     # deja correr los <Configure>
                return True
        return False

    def test_15_16_resize_pequeno_apila_y_grande_alinea(self):
        v = self.ventana(geometria="620x440")
        self.assertTrue(self.esperar_ancho(v.root, 620))
        fila_control = v.etiqueta_hotkey.master
        self.assertEqual(int(fila_control.grid_info()["row"]), 2)
        v.root.geometry("1280x860")
        self.assertTrue(self.esperar_ancho(v.root, 1280))
        self.assertEqual(int(fila_control.grid_info()["row"]), 0)
        # Nada queda fuera del área visible en horizontal.
        canvas = v._areas_scroll["Dictado"]["canvas"]
        contenido = v._areas_scroll["Dictado"]["contenido"]
        self.assertLessEqual(contenido.winfo_reqwidth(),
                             canvas.winfo_width() + 2)

    def test_hidpi_escala_medidas_y_geometria(self):
        normal = self.ventana(escala=1.0, mapear=False)
        doble = self.ventana(escala=2.0, mapear=False)
        self.assertAlmostEqual(doble.tema.escala / normal.tema.escala, 2.0,
                               places=1)
        self.assertEqual(doble.tema.px(10), 2 * normal.tema.px(10))
        self.assertGreater(doble.root.minsize()[0], normal.root.minsize()[0])

    def test_sin_timers_de_animacion_en_reposo(self):
        v = self.ventana()
        self.asentar(v.root, 0.4)
        interruptores = [w for w in v._widgets_foco.values()
                         if isinstance(w, Interruptor)]
        self.assertTrue(interruptores)
        self.assertFalse(any(i.animando for i in interruptores))
        pendientes = v.root.tk.call("after", "info")
        # Sólo los polls existentes (audio y estado del runtime).
        self.assertLessEqual(len(pendientes), 2)


@unittest.skipUnless(HAY_DISPLAY, "Tk necesita un display")
class PruebasTema(unittest.TestCase):
    def test_estilos_de_estado_tienen_color_propio(self):
        root = tk.Tk()
        try:
            Tema(root)
            from tkinter import ttk
            estilo = ttk.Style(root)
            colores = {nombre: estilo.lookup(nombre, "foreground")
                       for nombre in ("Status.TLabel", "Success.Status.TLabel",
                                      "Warning.Status.TLabel",
                                      "Error.Status.TLabel")}
            self.assertEqual(len(set(colores.values())), 4, colores)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
