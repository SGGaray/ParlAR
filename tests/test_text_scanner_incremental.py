"""Scanner lineal, contexto literal incremental y efectos de comandos citados."""

import threading
import time
import unittest
from types import SimpleNamespace

from parlar.app import App
from parlar.procesador_texto import (
    ProcesadorTexto, _proteger_regiones_citadas,
)


CORPUS_ESTRUCTURADO = (
    "(-0,75)", "€-18,90", "−18,90", ".125", ".5", ".env",
    ".gitignore", "(21:07)", "localhost:9142", "127.0.0.1:8000",
    "[::1]:8000", 'emit("em",  .25)', "echo em", 'x = ".125"',
    'x  =  ".125"',
    "$+10.50", "(+7.25);", ".config", "example.com:443",
    'console.log("uh dos")', "foo::bar",
)


class InyectorEfectos:
    def __init__(self):
        self.textos = []
        self.enters = 0
        self.undos = 0
        self.newlines = []

    def escribir_texto(self, texto, registrar=True):
        self.textos.append(texto)
        return True

    def presionar_enter(self):
        self.enters += 1
        return True

    def borrar_ultima_oracion(self):
        self.undos += 1
        return True

    def nueva_linea(self, cantidad=1):
        self.newlines.append(cantidad)
        return True


class SinkTexto:
    def __init__(self):
        self.textos = []

    def escribir_texto(self, texto):
        self.textos.append(texto)
        return True


def app_efectos():
    app = App.__new__(App)
    app._salida_lock = threading.RLock()
    app._necesita_espacio = False
    app.cfg = SimpleNamespace(comando_enviar=True)
    app.inyector = InyectorEfectos()
    app.guionar = SinkTexto()
    app.sesion = SinkTexto()
    app._puede_emit = lambda generacion: generacion == 1
    app.stops = 0
    app.detener_grabacion = (
        lambda **_kwargs: setattr(app, "stops", app.stops + 1) or True)
    return app


def procesar_particion(texto, partes):
    procesador = ProcesadorTexto(rewrite_mode="none", voice_commands=False)
    procesador.iniciar_unidad()
    salida = "".join(procesador.procesar_fragmento(parte) for parte in partes)
    procesador.finalizar_unidad()
    assert "".join(partes) == texto
    return salida


class PruebasScannerLineal(unittest.TestCase):
    def test_corpus_exacto_en_matriz_de_modos(self):
        for modo in ("none", "formal", "concise", "email"):
            procesador = ProcesadorTexto(
                rewrite_mode=modo, voice_commands=False)
            for entrada in CORPUS_ESTRUCTURADO:
                with self.subTest(modo=modo, entrada=entrada):
                    self.assertEqual(
                        procesador.procesar_frase(entrada).texto, entrada)

    def test_citas_abiertas_se_embalsaman_una_vez(self):
        for cantidad in (1000, 2000, 4000):
            with self.subTest(cantidad=cantidad):
                regiones = []
                entrada = "“" * cantidad
                salida = _proteger_regiones_citadas(
                    entrada, lambda region: regiones.append(region) or "X")
                self.assertEqual(salida, "X")
                self.assertEqual(regiones, [entrada])

    def test_escalado_malformado_tiene_presupuesto_holgado(self):
        procesador = ProcesadorTexto(voice_commands=False)
        duraciones = []
        for cantidad in (2000, 4000, 8000):
            inicio = time.perf_counter()
            self.assertEqual(
                procesador.procesar_frase("“" * cantidad).texto,
                "“" * cantidad)
            duraciones.append(time.perf_counter() - inicio)
        self.assertLess(max(duraciones), 0.5)

    def test_placeholders_multiples_anidados_y_repetidos(self):
        entrada = (
            '\ue000PARLAR0\ue001 emit("em",  .25) '
            'headers["em"] {"em":{"uh":".125"}}')
        for modo in ("none", "formal", "concise", "email"):
            with self.subTest(modo=modo):
                procesador = ProcesadorTexto(
                    rewrite_mode=modo, voice_commands=False)
                self.assertEqual(procesador.procesar_frase(entrada).texto,
                                 entrada)


class PruebasIncrementales(unittest.TestCase):
    def test_particiones_no_deciden_fidelidad_literal(self):
        matrices = {
            '"em vive aquí"': (
                ['"em vive aquí"'],
                ['"em ', 'vive aquí"'],
                ['"em vive ', 'aquí"'],
                ['"', 'em', ' vive ', 'aquí', '"'],
            ),
            '"localhost:9142"': (
                ['"localhost:9142"'], ['"local', 'host:9142"']),
            '"emit(\\"em\\", .25)"': (
                ['"emit(\\"em\\", .25)"'],
                ['"emit(', '\\"em\\"', ', .25)"'],
                ['"emit(\\', '"em', '\\"', ', .25)', '"'],
            ),
            '"detener dictado"': (
                ['"detener dictado"'], ['"', 'detener ', 'dictado', '"']),
        }
        for texto, particiones in matrices.items():
            for partes in particiones:
                with self.subTest(texto=texto, partes=partes):
                    self.assertEqual(procesar_particion(texto, partes), texto)

    def test_escape_partido_no_cierra_la_cita(self):
        texto = '"él dijo \\"em\\" acá"'
        particiones = (
            [texto],
            ['"él dijo \\', '"em\\" acá"'],
            ['"él ', 'dijo \\', '"em', '\\" acá', '"'],
        )
        for partes in particiones:
            with self.subTest(partes=partes):
                self.assertEqual(procesar_particion(texto, partes), texto)

    def test_cita_abierta_final_se_preserva_sin_inventar_cierre(self):
        procesador = ProcesadorTexto(voice_commands=False)
        procesador.iniciar_unidad()
        self.assertEqual(procesador.procesar_fragmento('"em'), '"em')
        procesador.finalizar_unidad()

    def test_muletilla_solo_se_limpia_al_inicio_real(self):
        procesador = ProcesadorTexto(voice_commands=False)
        procesador.iniciar_unidad()
        self.assertEqual(procesador.procesar_fragmento("  em probamos "),
                         "probamos ")
        self.assertEqual(procesador.procesar_fragmento("echo em"), "echo em")

    def test_final_cancel_y_nueva_unidad_resetean_estado(self):
        for frontera in ("finalizar_unidad", "cancelar_unidad"):
            with self.subTest(frontera=frontera):
                procesador = ProcesadorTexto(voice_commands=False)
                procesador.iniciar_unidad()
                self.assertEqual(procesador.procesar_fragmento('"em'), '"em')
                getattr(procesador, frontera)()
                procesador.iniciar_unidad()
                self.assertEqual(
                    procesador.procesar_fragmento("em normal"), "normal")

    def test_app_wiring_resetea_en_restart_gap_cancelacion(self):
        eventos = []
        app = App.__new__(App)
        app.proc = SimpleNamespace(
            iniciar_unidad=lambda: eventos.append("inicio"),
            finalizar_unidad=lambda: eventos.append("final"),
            cancelar_unidad=lambda: eventos.append("cancel"),
        )
        app.streaming = SimpleNamespace(
            reiniciar=lambda: eventos.append("streaming"))
        app._iniciar_contexto_texto()
        app._finalizar_contexto_texto()
        app._reiniciar_streaming()
        self.assertEqual(
            eventos, ["inicio", "final", "streaming", "cancel"])


class PruebasComandosCitadosApp(unittest.TestCase):
    def test_puntuacion_exterior_nunca_dispara_efectos(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        entradas = (
            '"detener dictado"…', '«borra la última oración»,',
            '“enviar”…', '"enviar" ', '"detener dictado";   ',
            '"detener dictado":?!…',
        )
        for entrada in entradas:
            with self.subTest(entrada=entrada):
                app = app_efectos()
                resultado = procesador.procesar_frase(entrada)
                self.assertIsNone(resultado.comando)
                app._emitir(resultado, 1)
                self.assertEqual(app.stops, 0)
                self.assertEqual(app.inyector.undos, 0)
                self.assertEqual(app.inyector.enters, 0)
                self.assertEqual(len(app.inyector.textos), 1)

    def test_comandos_reales_siguen_disparando_efectos(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        casos = (
            ("detener dictado.", "stops"),
            ("borra la última oración!", "undos"),
            ("enviar!", "enters"),
        )
        for entrada, efecto in casos:
            with self.subTest(entrada=entrada):
                app = app_efectos()
                app._emitir(procesador.procesar_frase(entrada), 1)
                observado = (app.stops if efecto == "stops" else
                             getattr(app.inyector, efecto))
                self.assertEqual(observado, 1)


class PruebasConciseSemantico(unittest.TestCase):
    def test_calificadores_y_viste_se_preservan(self):
        corpus = (
            "No significa literalmente lo mismo",
            "No es básicamente equivalente",
            "This is actually different",
            "It is kind of equivalent",
            "viste, esto funciona", "esto funciona, viste",
            "no, viste", "no viste", "no lo viste", "si lo viste",
            "¿viste el resultado?", "viste el auto", "lo viste ayer",
            '"viste"',
        )
        procesador = ProcesadorTexto(
            rewrite_mode="concise", voice_commands=False)
        for entrada in corpus:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).texto,
                    entrada if not entrada.startswith("¿") else
                    "¿Viste el resultado?",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
