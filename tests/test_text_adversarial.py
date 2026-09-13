"""Corpus adversarial de fidelidad, citas y reescritura conservadora."""

import unittest
from unittest import mock

from parlar.procesador_texto import ProcesadorTexto


NUMEROS = (
    "3.14", "-3.14", "3,14", "-3,14", "1,234.56", "1.234,56",
    "12:30", "12:30,", "09:45.", "50%", "-10%", "$100", "€99,50",
    "2026-09-11", "10.0.0.1", "127.0.0.1:8000", "v1.2.3", "-12,30,",
)

LITERALES = (
    'headers["em"]', "headers['em']", '{"em": "no"}',
    "{'em': 'no'}", 'foo["este"]', "foo['este']", 'JSON.parse("em")',
    'print("eh")', 'x = "em"', "x = 'em'", "foo.bar(x)", "C++", "C#",
    "HTTP/2", "foo_bar", "foo-bar", '{"em":"no"}', '{"mmm":"eh"}',
    'call("o sea")', r'headers[\"em\"]',
)


class PruebasFidelidadAdversarial(unittest.TestCase):
    def test_none_preserva_corpus_estructurado_exacto(self):
        procesador = ProcesadorTexto(
            rewrite_mode="none", voice_commands=False)
        for entrada in NUMEROS + LITERALES:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).texto, entrada)

    def test_todos_los_modos_preservan_regiones_estructuradas(self):
        for modo in ("none", "formal", "concise", "email"):
            procesador = ProcesadorTexto(
                rewrite_mode=modo, voice_commands=False)
            for entrada in NUMEROS + LITERALES:
                with self.subTest(modo=modo, entrada=entrada):
                    self.assertEqual(
                        procesador.procesar_frase(entrada).texto, entrada)

    def test_prosa_puede_cambiar_sin_corromper_regiones(self):
        entrada = 'ok mandame el valor -3,14 y headers["em"]'
        esperado = 'De acuerdo mandame el valor -3,14 y headers["em"]'
        for modo in ("formal", "email"):
            with self.subTest(modo=modo):
                procesador = ProcesadorTexto(
                    rewrite_mode=modo, voice_commands=False)
                self.assertEqual(
                    procesador.procesar_frase(entrada).texto, esperado)

    def test_ollama_no_puede_perder_regiones_protegidas(self):
        procesador = ProcesadorTexto(
            rewrite_mode="formal", ollama_model="modelo-falso",
            voice_commands=False)
        entrada = 'ok cuesta -3,14 y headers["em"]'
        with mock.patch.object(
                procesador, "_reescribir_ollama",
                side_effect=lambda texto: texto.replace("ok", "correcto")):
            self.assertEqual(
                procesador.procesar_frase(entrada).texto,
                'correcto cuesta -3,14 y headers["em"]',
            )
        with mock.patch.object(
                procesador, "_reescribir_ollama", return_value="sin datos"):
            self.assertEqual(
                procesador.procesar_frase(entrada).texto,
                'De acuerdo cuesta -3,14 y headers["em"]',
            )

    def test_puntuacion_exterior_no_entra_en_horarios(self):
        procesador = ProcesadorTexto(
            rewrite_mode="none", voice_commands=False)
        self.assertEqual(
            procesador.procesar_frase("Son las 12:30,").texto,
            "Son las 12:30,",
        )
        self.assertEqual(
            procesador.procesar_frase('dijo "em y 3.14".').texto,
            'dijo "em y 3.14".',
        )


class PruebasCitasAdversariales(unittest.TestCase):
    def test_comandos_completamente_citados_son_texto_con_puntuacion(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        entradas = (
            '"detener dictado".', '"detener dictado"?',
            '"detener dictado"!', "'nueva línea'.",
            "«borra la última oración».", "“enviar”.", "‘nuevo párrafo’!",
            '"deterner dictado".',
        )
        for entrada in entradas:
            with self.subTest(entrada=entrada):
                resultado = procesador.procesar_frase(entrada)
                self.assertIsNone(resultado.comando)
                self.assertEqual(resultado.texto, entrada)

    def test_prosa_alrededor_de_citas_no_es_comando(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        entradas = (
            'dijo "detener dictado".', 'escribí "enviar".',
            'la frase "nueva línea" aparece acá.',
            'repitió «borra la última oración».',
        )
        for entrada in entradas:
            with self.subTest(entrada=entrada):
                resultado = procesador.procesar_frase(entrada)
                self.assertIsNone(resultado.comando)
                self.assertEqual(resultado.texto, entrada)

    def test_comandos_reales_con_puntuacion_siguen_funcionando(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        casos = (
            ("detener dictado.", "detener"),
            ("borra la última oración!", "borrar_ultima"),
            ("enviar?", "enviar"),
            ("nueva línea.", "nueva_linea"),
            ("nuevo párrafo!", "nueva_linea"),
        )
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).comando, esperado)


class PruebasComandosPHYS003(unittest.TestCase):
    def test_aliases_largos_y_puntuacion_espanola_emiten_accion_exacta(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        casos = (
            ("mandar mensaje", "enviar", None),
            ("Mandar mensaje.", "enviar", None),
            ("enviar mensaje", "enviar", None),
            ("Enviar mensaje!", "enviar", None),
            ("salto de línea", "nueva_linea", "\n"),
            ("Salto de línea.", "nueva_linea", "\n"),
            ("salto de párrafo", "nueva_linea", "\n\n"),
            ("Salto de párrafo.", "nueva_linea", "\n\n"),
            ("¡Nuevo párrafo!", "nueva_linea", "\n\n"),
            ("¿Nueva línea?", "nueva_linea", "\n"),
        )
        for entrada, comando, carga in casos:
            with self.subTest(entrada=entrada):
                resultado = procesador.procesar_frase(entrada)
                self.assertEqual(resultado.comando, comando)
                self.assertEqual(resultado.carga, carga)
                self.assertEqual(resultado.texto, "")

    def test_errores_acusticos_y_prosa_siguen_siendo_texto(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        casos = (
            ("MBR", "MBR"),
            ("en mi ar", "en mi ar"),
            ("barrafo", "barrafo"),
            ("nuevo parrafó", "nuevo parrafó"),
            ("hola mandar mensaje", "hola mandar mensaje"),
            ("escribí enviar mensaje", "escribí enviar mensaje"),
            ("la frase salto de línea", "la frase salto de línea"),
            ("¡hola nueva línea!", "¡Hola nueva línea!"),
            ('dijo "enviar"', 'dijo "enviar"'),
            ("hola nueva línea", "hola nueva línea"),
            ("(enviar)", "(enviar)"),
            ("`enviar`", "`enviar`"),
            ("**enviar**", "**enviar**"),
            ('"enviar"', '"enviar"'),
        )
        for entrada, texto in casos:
            with self.subTest(entrada=entrada):
                resultado = procesador.procesar_frase(entrada)
                self.assertIsNone(resultado.comando)
                self.assertEqual(resultado.texto, texto)

    def test_delimitadores_espanoles_solo_aceptan_un_par_completo(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        entradas = (
            "¡nuevo párrafo",
            "nuevo párrafo¡",
            "¿nueva línea!",
            "¡ nuevo párrafo!",
            "¡nuevo párrafo !",
            "¡¡nuevo párrafo!!",
            "¡nuevo párrafo! extra",
        )
        for entrada in entradas:
            with self.subTest(entrada=entrada):
                self.assertIsNone(
                    procesador.procesar_frase(entrada).comando)


class PruebasConciseAdversariales(unittest.TestCase):
    def test_viste_verbal_o_ambiguo_se_conserva(self):
        procesador = ProcesadorTexto(
            rewrite_mode="concise", voice_commands=False)
        casos = (
            ("no viste", "no viste"),
            ("no lo viste", "no lo viste"),
            ("no la viste", "no la viste"),
            ("si lo viste", "si lo viste"),
            ("¿viste el resultado?", "¿Viste el resultado?"),
            ("viste el auto", "viste el auto"),
            ("lo viste ayer", "lo viste ayer"),
            ("básicamente esto funciona viste",
             "básicamente esto funciona viste"),
        )
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).texto, esperado)

    def test_coma_no_basta_para_eliminar_viste(self):
        procesador = ProcesadorTexto(
            rewrite_mode="concise", voice_commands=False)
        casos = (
            ("viste, esto funciona", "viste, esto funciona"),
            ("esto funciona, viste", "esto funciona, viste"),
            ("esto funciona, viste.", "esto funciona, viste."),
        )
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).texto, esperado)


if __name__ == "__main__":
    unittest.main(verbosity=2)
