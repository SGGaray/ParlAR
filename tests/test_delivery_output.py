"""Contratos de distribución, clipboard, undo y acciones Return."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from parlar.app import App
from parlar.entrega import EstadoEntrega, ResultadoSink
from parlar.inyector_salida import Inyector
from parlar.procesador_texto import Procesado, ProcesadorTexto


class InyectorFalso:
    def __init__(self, resultado=True, error=False):
        self.resultado = resultado
        self.error = error
        self.textos = []
        self.enters = 0
        self.newlines = []
        self.undos = 0

    def escribir_texto(self, texto, registrar=True):
        self.textos.append(texto)
        if self.error:
            raise RuntimeError("inyector")
        return self.resultado

    def presionar_enter(self):
        self.enters += 1
        return True

    def nueva_linea(self, cantidad=1):
        self.newlines.append(cantidad)
        return True

    def borrar_ultima_oracion(self):
        self.undos += 1
        return True


class SinkFalso:
    def __init__(self, resultado=True, error=False):
        self.resultado = resultado
        self.error = error
        self.textos = []

    def escribir_texto(self, texto):
        self.textos.append(texto)
        if self.error:
            raise OSError("sink")
        return self.resultado


def app_minima(inyector=None, guionar=None, sesion=None, permitir_enter=False):
    app = App.__new__(App)
    app._salida_lock = threading.RLock()
    app._necesita_espacio = False
    app.cfg = SimpleNamespace(comando_enviar=permitir_enter)
    app.inyector = inyector or InyectorFalso()
    app.guionar = guionar or SinkFalso()
    app.sesion = sesion or SinkFalso()
    app._puede_emit = lambda generacion: generacion == 1
    return app


class PruebasDistribucion(unittest.TestCase):
    def test_fallos_de_sinks_son_independientes(self):
        casos = (
            (InyectorFalso(error=True), SinkFalso(), SinkFalso(),
             (EstadoEntrega.FAILED, EstadoEntrega.MIRRORED,
              EstadoEntrega.PERSISTED)),
            (InyectorFalso(), SinkFalso(error=True), SinkFalso(),
             (EstadoEntrega.INSERTED, EstadoEntrega.FAILED,
              EstadoEntrega.PERSISTED)),
            (InyectorFalso(), SinkFalso(), SinkFalso(error=True),
             (EstadoEntrega.INSERTED, EstadoEntrega.MIRRORED,
              EstadoEntrega.FAILED)),
        )
        for inyector, guionar, sesion, esperado in casos:
            with self.subTest(esperado=esperado):
                app = app_minima(inyector, guionar, sesion)
                r = app._emitir(Procesado(texto="hola"), 1)
                self.assertEqual((r.inyector, r.guionar, r.sesion), esperado)
                self.assertEqual(inyector.textos, ["hola"])
                self.assertEqual(guionar.textos, ["hola"])
                self.assertEqual(sesion.textos, ["hola"])

    def test_todos_funcionan_una_vez_y_stale_no_emite(self):
        app = app_minima()
        r = app._emitir(Procesado(texto="hola"), 1)
        self.assertEqual(
            (r.inyector, r.guionar, r.sesion),
            (EstadoEntrega.INSERTED, EstadoEntrega.MIRRORED,
             EstadoEntrega.PERSISTED))
        self.assertEqual(app.inyector.textos, ["hola"])

    def test_copiado_se_distingue_de_insertado_sin_bloquear_sinks(self):
        app = app_minima(
            InyectorFalso(ResultadoSink(EstadoEntrega.COPIED)),
            SinkFalso(), SinkFalso())
        r = app._emitir(Procesado(texto="hola"), 1)
        self.assertEqual(r.inyector, EstadoEntrega.COPIED)
        self.assertEqual(r.guionar, EstadoEntrega.MIRRORED)
        self.assertEqual(r.sesion, EstadoEntrega.PERSISTED)
        self.assertFalse(app._necesita_espacio)
        r = app._emitir(Procesado(texto="stale"), 2)
        self.assertTrue(all(estado == EstadoEntrega.SKIPPED for estado in (
            r.inyector, r.guionar, r.sesion)))
        self.assertEqual(app.inyector.textos, ["hola"])


class PruebasClipboardUndo(unittest.TestCase):
    def setUp(self):
        self.inyector = Inyector(backend="clipboard", notify=False)
        self.copias = []
        self.parche = mock.patch.object(
            self.inyector, "_portapapeles",
            side_effect=lambda texto: self.copias.append(texto) or True)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()

    def test_streaming_acumula_y_fronteras_resetean(self):
        self.inyector.iniciar_unidad(1)
        for texto in ("hola", " mundo", " final"):
            self.assertEqual(
                self.inyector.escribir_texto(texto).estado,
                EstadoEntrega.COPIED)
        self.assertEqual(self.copias[-1], "hola mundo final")
        self.inyector.finalizar_unidad()
        self.inyector.iniciar_unidad(1)
        self.inyector.escribir_texto("otra frase")
        self.assertEqual(self.copias[-1], "otra frase")
        self.inyector.cancelar_unidad()
        self.inyector.iniciar_unidad(2)
        self.inyector.escribir_texto("nueva sesión")
        self.assertEqual(self.copias[-1], "nueva sesión")

    def test_copiado_no_entra_en_undo_insertado_si(self):
        self.inyector.iniciar_unidad(1)
        self.inyector.escribir_texto("á🙂")
        self.assertEqual(self.inyector._registro_oraciones, [])
        self.assertFalse(self.inyector.borrar_ultima_oracion())

        tipeo = Inyector(backend="xdotool", notify=False)
        with mock.patch.object(tipeo, "_tipear", return_value=True):
            resultado = tipeo.escribir_texto("á🙂")
        self.assertEqual(resultado.estado, EstadoEntrega.INSERTED)
        self.assertEqual(tipeo._registro_oraciones, ["á🙂"])
        with mock.patch.object(tipeo, "retroceso", return_value=False):
            self.assertFalse(tipeo.borrar_ultima_oracion())

        fallback = Inyector(backend="xdotool", notify=False)
        with (mock.patch.object(fallback, "_tipear", return_value=False),
              mock.patch.object(fallback, "_portapapeles", return_value=True)):
            resultado = fallback.escribir_texto("solo copiado")
        self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
        self.assertEqual(fallback._registro_oraciones, [])


class PruebasComandosReturn(unittest.TestCase):
    def test_default_bloquea_todas_las_acciones_enter_y_opt_in_las_permite(self):
        proc = ProcesadorTexto()
        comandos = ("nueva línea", "nuevo párrafo", "enviar")
        app = app_minima(permitir_enter=False)
        for texto in comandos:
            app._emitir(proc.procesar_frase(texto), 1)
        self.assertEqual(app.inyector.newlines, [])
        self.assertEqual(app.inyector.enters, 0)

        app = app_minima(permitir_enter=True)
        for texto in comandos:
            app._emitir(proc.procesar_frase(texto), 1)
        self.assertEqual(app.inyector.newlines, [1, 2])
        self.assertEqual(app.inyector.enters, 1)

    def test_texto_normal_y_comandos_citados_no_son_acciones(self):
        proc = ProcesadorTexto()
        app = app_minima()
        entradas = (
            "escribí nueva línea en el documento", '"nueva línea"',
            '"nuevo párrafo"', '"enviar"',
        )
        for entrada in entradas:
            app._emitir(proc.procesar_frase(entrada), 1)
        self.assertEqual(app.inyector.newlines, [])
        self.assertEqual(app.inyector.enters, 0)
        self.assertEqual(len(app.inyector.textos), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
