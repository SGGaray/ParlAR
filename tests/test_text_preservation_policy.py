"""Política conservadora para comandos, multiline y streaming append-only."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App
from parlar.coordinador_salida import CoordinadorSalida
from parlar.entrega import EstadoEntrega, ResultadoSink
from parlar.inyector_salida import Inyector
from parlar.motor_transcripcion import TranscriptorStreaming
from parlar.procesador_texto import Procesado, ProcesadorTexto


class InyectorEfectos:
    def __init__(self):
        self.textos = []
        self.enters = 0
        self.undos = 0
        self.newlines = []

    def escribir_texto(self, texto, registrar=True):
        self.textos.append(texto)
        return ResultadoSink(EstadoEntrega.INSERTED)

    def presionar_enter(self):
        self.enters += 1
        return True

    def borrar_ultima_oracion(self):
        self.undos += 1
        return True

    def nueva_linea(self, cantidad=1):
        self.newlines.append(cantidad)
        return True


def _app(inyector, *, permitir_return=True):
    app = App.__new__(App)
    app._salida_lock = threading.RLock()
    app.cfg = SimpleNamespace(comando_enviar=permitir_return)
    app.salida = CoordinadorSalida(
        inyector,
        SimpleNamespace(es_nulo=True),
        SimpleNamespace(es_nulo=True),
    )
    app._puede_emit = lambda generacion: generacion == 1
    app.stops = 0
    app.detener_grabacion = (
        lambda **_kwargs: setattr(app, "stops", app.stops + 1) or True)
    return app


class PruebasGramaticaComandos(unittest.TestCase):
    LITERALES = (
        '"borra la última oración',
        '`borra la última oración`',
        '"borra" "la última oración"',
        '`detener dictado`',
        '("detener dictado")',
        '[detener dictado]',
        "'detener dictado'",
        '“detener dictado”',
        '‘detener dictado’',
        '«detener dictado»',
        '「detener dictado」',
        '**detener dictado**',
        'detener dictado;',
    )

    def test_regiones_literales_son_texto_sin_efectos_app(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        for entrada in self.LITERALES:
            with self.subTest(entrada=entrada):
                app = _app(InyectorEfectos())
                resultado = procesador.procesar_frase(entrada)
                self.assertIsNone(resultado.comando)
                self.assertEqual(resultado.texto, entrada)
                app._emitir(resultado, 1)
                self.assertEqual(app.stops, 0)
                self.assertEqual(app.inyector.undos, 0)
                self.assertEqual(app.inyector.enters, 0)
                self.assertEqual(app.inyector.newlines, [])
                self.assertEqual(app.inyector.textos, [entrada])

    def test_ordenes_desnudas_admiten_un_signo_terminal_y_un_efecto(self):
        casos = (
            ("  detener dictado.  ", "stop"),
            ("borra la última oración!", "undo"),
            ("enviar?", "return"),
            ("nueva línea…", "newline"),
        )
        procesador = ProcesadorTexto(comando_enviar=True)
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                app = _app(InyectorEfectos())
                app._emitir(procesador.procesar_frase(entrada), 1)
                observados = {
                    "stop": app.stops,
                    "undo": app.inyector.undos,
                    "return": app.inyector.enters,
                    "newline": sum(app.inyector.newlines),
                }
                self.assertEqual(observados[esperado], 1)
                self.assertEqual(sum(observados.values()), 1)
                self.assertEqual(app.inyector.textos, [])

    def test_literal_de_undo_con_historial_real_no_envia_backspace(self):
        inyector = Inyector(
            backend="wtype", notify=False, permitir_return=True)
        llamadas = []
        app = _app(inyector)
        procesador = ProcesadorTexto(comando_enviar=True)
        with (mock.patch.object(
                inyector, "_correr",
                side_effect=lambda argv: llamadas.append(argv) or True),
              mock.patch.object(inyector, "_portapapeles", return_value=True)):
            app._emitir(Procesado(texto="Primera oración."), 1)
            app._emitir(
                procesador.procesar_frase("`borra la última oración`"), 1)

        self.assertEqual(inyector._registro_oraciones[0], "Primera oración.")
        self.assertEqual(len(inyector._registro_oraciones), 2)
        self.assertFalse(any("BackSpace" in argumento
                             for argv in llamadas for argumento in argv))
        self.assertFalse(any("Return" in argumento
                             for argv in llamadas for argumento in argv))
        self.assertTrue(any("`borra la última oración`" in argumento
                            for argumento in llamadas[-1]))


class PruebasMultilineConservador(unittest.TestCase):
    CORPUS = (
        "a\nb",
        "a\r\nb",
        "a\rb",
        "a\n\nb",
        "\tfoo\n\tbar",
        "foo\tbar",
        "\tfoo",
        'print("em")\nprint("uh")',
        '```python\nprint("em")\nprint("uh")\n```',
        "- uno\n- dos\n\n    bloque indentado",
        '{\n  "clave": "valor"\n}',
        "if true; then\n\techo ok\nfi",
    )

    def test_corpus_preserva_line_endings_tabs_e_indentacion_exactos(self):
        for modo in ("none", "formal", "concise", "email"):
            procesador = ProcesadorTexto(
                rewrite_mode=modo, voice_commands=True)
            for entrada in self.CORPUS:
                with self.subTest(modo=modo, entrada=repr(entrada)):
                    resultado = procesador.procesar_frase(entrada)
                    self.assertIsNone(resultado.comando)
                    self.assertEqual(resultado.texto, entrada)

    def test_app_e_inyector_copian_multiline_exacto_sin_return(self):
        entrada = 'print("em")\nprint("uh")'
        procesador = ProcesadorTexto(
            rewrite_mode="none", comando_enviar=False)
        inyector = Inyector(
            backend="wtype", notify=False, permitir_return=False)
        llamadas = []
        copias = []
        app = _app(inyector, permitir_return=False)
        with (mock.patch.object(
                inyector, "_correr",
                side_effect=lambda argv: llamadas.append(argv) or True),
              mock.patch.object(
                  inyector, "_portapapeles",
                  side_effect=lambda texto: copias.append(texto) or True)):
            resultado = app._emitir(procesador.procesar_frase(entrada), 1)

        self.assertEqual(resultado.inyector, EstadoEntrega.COPIED)
        self.assertEqual(llamadas, [])
        self.assertEqual(copias, [entrada])

    def test_bloques_despues_de_insercion_llegan_exactos_a_clipboard(self):
        bloques = (
            "a\nb",
            "a\r\nb",
            "a\rb",
            "a\n\nb",
            "\tfoo\n\tbar",
            'print("em")\nprint("uh")',
            '```python\nprint("em")\nprint("uh")\n```',
            "    bloque\n    indentado",
            '{\n  "clave": "valor"\n}',
        )
        for entrada in bloques:
            with self.subTest(entrada=repr(entrada)):
                procesador = ProcesadorTexto(comando_enviar=False)
                inyector = Inyector(
                    backend="wtype", notify=False, permitir_return=False)
                app = _app(inyector, permitir_return=False)
                llamadas = []
                copias = []
                with (
                    mock.patch.object(
                        inyector, "_correr",
                        side_effect=lambda argv: llamadas.append(argv) or True,
                    ),
                    mock.patch.object(
                        inyector, "_portapapeles",
                        side_effect=lambda texto: copias.append(texto) or True,
                    ),
                ):
                    app._emitir(Procesado(texto="previo"), 1)
                    llamadas.clear()
                    procesado = procesador.procesar_frase(entrada)
                    resultado = app._emitir(procesado, 1)

                self.assertEqual(procesado.texto, entrada)
                self.assertEqual(resultado.inyector, EstadoEntrega.COPIED)
                self.assertEqual(copias, [entrada])
                self.assertEqual(llamadas, [])
                self.assertTrue(app._necesita_espacio)

    def test_copy_multiline_no_cambia_estado_de_separacion(self):
        for copia_ok, estado in (
                (True, EstadoEntrega.COPIED),
                (False, EstadoEntrega.FAILED)):
            with self.subTest(copia_ok=copia_ok):
                inyector = Inyector(
                    backend="wtype", notify=False, permitir_return=False)
                app = _app(inyector, permitir_return=False)
                tipeados = []
                with (
                    mock.patch.object(
                        inyector, "_correr",
                        side_effect=lambda argv: tipeados.append(argv[-1]) or True,
                    ),
                    mock.patch.object(
                        inyector, "_portapapeles", return_value=copia_ok),
                ):
                    app._emitir(Procesado(texto="previo"), 1)
                    bloque = app._emitir(Procesado(texto="a\nb"), 1)
                    app._emitir(Procesado(texto="después"), 1)

                self.assertEqual(bloque.inyector, estado)
                self.assertEqual(tipeados, ["previo", " después"])
                self.assertTrue(app._necesita_espacio)


def _procesar_partes(texto, cortes):
    partes = [texto[inicio:fin]
              for inicio, fin in zip((0,) + cortes, cortes + (len(texto),))]
    procesador = ProcesadorTexto(rewrite_mode="none")
    procesador.iniciar_unidad()
    salida = "".join(procesador.procesar_fragmento(parte) for parte in partes)
    procesador.finalizar_unidad()
    return salida


class PalabraFalsa:
    def __init__(self, texto, fin):
        self.word = texto
        self.end = fin
        self.start = None


class SegmentoFalso:
    def __init__(self, textos):
        self.words = [PalabraFalsa(texto, (indice + 1) * 0.2)
                      for indice, texto in enumerate(textos)]


class MotorPartido:
    def __init__(self):
        self.hipotesis = (
            ("em",), ("em",), ("em", "ail"), ("em", "ail"),
            ("em", "ail"),
        )
        self.llamadas = 0

    def decodificar(self, audio, word_timestamps=False, beam_size=None):
        indice = min(self.llamadas, len(self.hipotesis) - 1)
        self.llamadas += 1
        return [SegmentoFalso(self.hipotesis[indice])]


class PruebasParticionIncremental(unittest.TestCase):
    CORPUS = (
        "email", "example", "emergency", "embedded", "em",
        '"email"', "'don't change'", '"em vive aquí"',
        "localhost:9142", "foo@bar.com", 'emit("em", .25)', ".env",
        "empresa", "emisión", "emails", "emoji", "mañana", "acción",
        "I'm", "it's", "user's", "don't",
    )

    def test_todos_los_cortes_preservan_caracteres_lexicos(self):
        for texto in self.CORPUS:
            particiones = [()] + [(corte,) for corte in range(1, len(texto))]
            for cortes in particiones:
                with self.subTest(texto=texto, cortes=cortes):
                    self.assertEqual(_procesar_partes(texto, cortes), texto)

    def test_localagreement_emite_em_y_ail_sin_perder_prefijo(self):
        streaming = TranscriptorStreaming(
            MotorPartido(), sample_rate=16000,
            interval_s=0.1, trim_s=999)
        procesador = ProcesadorTexto(rewrite_mode="none")
        procesador.iniciar_unidad()
        confirmados = []
        for _ in range(4):
            streaming.aceptar_audio(np.zeros(16000, dtype=np.float32))
            confirmados.append(streaming.procesar())
        confirmados.append(streaming.finalizar())
        salida = "".join(
            procesador.procesar_fragmento(fragmento)
            for fragmento in confirmados)
        procesador.finalizar_unidad()
        self.assertEqual(confirmados, ["", "em", "", "ail", ""])
        self.assertEqual(salida, "email")

    def test_fronteras_resetean_sin_cola_lexica_pendiente(self):
        procesador = ProcesadorTexto(rewrite_mode="none")
        for frontera in (
                procesador.finalizar_unidad, procesador.cancelar_unidad):
            procesador.iniciar_unidad()
            self.assertEqual(procesador.procesar_fragmento("don'"), "don'")
            frontera()
            procesador.iniciar_unidad()
            self.assertEqual(procesador.procesar_fragmento("t"), "t")
        texto_largo = "x" * 100_000
        self.assertIs(procesador.procesar_fragmento(texto_largo), texto_largo)


if __name__ == "__main__":
    unittest.main()
