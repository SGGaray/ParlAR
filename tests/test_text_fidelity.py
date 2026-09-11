"""Regresiones de fidelidad post-Whisper y filtrado conservador."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.motor_transcripcion import TranscriptorFrase
from parlar.procesador_texto import ProcesadorTexto


class MotorSegmentos:
    def __init__(self, segmentos):
        self.segmentos = segmentos

    def decodificar(self, audio):
        return [
            SimpleNamespace(
                text=texto,
                no_speech_prob=no_speech,
                avg_logprob=logprob,
            )
            for texto, no_speech, logprob in self.segmentos
        ]


def transcribir(*segmentos):
    motor = MotorSegmentos(segmentos)
    audio = np.zeros(1600, dtype=np.float32)
    return TranscriptorFrase(motor).transcribir(audio)


class RespuestaOllamaFalsa:
    def __init__(self, texto):
        self.texto = texto

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"response": self.texto}).encode()


class PruebasLiteral(unittest.TestCase):
    def setUp(self):
        self.procesador = ProcesadorTexto(
            rewrite_mode="none", voice_commands=False)

    def verificar_corpus(self, casos):
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                observado = self.procesador.procesar_frase(entrada).texto
                self.assertEqual(observado, esperado)

    def test_numeros_preservan_estructura(self):
        self.verificar_corpus((
            ("3.14", "3.14"),
            ("1,234.56", "1,234.56"),
            ("10.0.0.1", "10.0.0.1"),
            ("192.168.1.1", "192.168.1.1"),
            ("2026-09-10", "2026-09-10"),
            ("12:30", "12:30"),
            ("50%", "50%"),
            ("$100", "$100"),
            ("€99.50", "€99.50"),
        ))

    def test_urls_y_dominios_se_preservan(self):
        self.verificar_corpus((
            ("https://example.com", "https://example.com"),
            ("https://example.com/a?x=1", "https://example.com/a?x=1"),
            ("http://localhost:3000", "http://localhost:3000"),
            ("github.com/user/repo", "github.com/user/repo"),
            ("www.youtube.com", "www.youtube.com"),
            ("visita example.com ahora", "visita example.com ahora"),
        ))

    def test_emails_se_preservan(self):
        self.verificar_corpus((
            ("user@example.com", "user@example.com"),
            ("first.last+tag@example.co.uk", "first.last+tag@example.co.uk"),
            ("email em@example.com", "email em@example.com"),
        ))

    def test_codigo_e_identificadores_se_preservan(self):
        tokens = (
            "foo.bar(x)", "foo_bar", "foo-bar", "C++", "C#", "HTTP/2",
            "JSON.parse()", "npm run build", "git status", "127.0.0.1:8000",
            "v1.2.3",
        )
        self.verificar_corpus(tuple((token, token) for token in tokens))

    def test_acronimos_y_nombres_se_preservan(self):
        textos = (
            "EM algorithm", "NASA API", "HTTP request", "Dale Carnegie",
            "San Martín", "OpenAI", "GitHub", "PyQt6",
        )
        self.verificar_corpus(tuple((texto, texto) for texto in textos))

    def test_espanol_negacion_unicode_y_emoji(self):
        self.verificar_corpus((
            ("no viste el error", "no viste el error"),
            ("no quiero este informe", "no quiero este informe"),
            ("¿cómo estás?", "¿Cómo estás?"),
            ("¡perfecto!", "¡Perfecto!"),
            ("él dijo que no", "él dijo que no"),
            ("esto no funciona", "esto no funciona"),
            ("niñez, canción y pingüino 🙂", "niñez, canción y pingüino 🙂"),
            ("漢字 y café", "漢字 y café"),
        ))

    def test_repeticiones_legitimas(self):
        textos = (
            "muy muy bien", "sí sí", "I think I think this works",
            "New York New York",
        )
        self.verificar_corpus(tuple((texto, texto) for texto in textos))

    def test_mixed_language_no_castellaniza_tokens(self):
        textos = (
            "el HTTP request falló", "hacé git status",
            "OpenAI API responde 200",
        )
        self.verificar_corpus(tuple((texto, texto) for texto in textos))

    def test_cleanup_solo_corrige_prosa_inequivoca(self):
        self.verificar_corpus((
            ("hola , mundo", "hola, mundo"),
            ("¿cómo estás?todo bien.¿y vos?", "¿Cómo estás? Todo bien. ¿Y vos?"),
            ("a test.it works", "a test.it works"),
        ))

    def test_placeholders_no_colisionan_ni_se_filtran(self):
        entrada = "\ue000PARLAR0\ue001 y 3.14"
        observado = self.procesador.procesar_frase(entrada).texto
        self.assertEqual(observado, entrada)


class PruebasFillersYComandos(unittest.TestCase):
    def test_fillers_aislados_y_acronimos(self):
        procesador = ProcesadorTexto(voice_commands=False)
        casos = (
            ("eh bueno", "Bueno"),
            ("em probamos", "Probamos"),
            ("mmm, seguimos", "Seguimos"),
            ("esteee vamos", "Vamos"),
            ("EM algorithm", "EM algorithm"),
            ("no quiero este informe", "no quiero este informe"),
            ("tema importante", "tema importante"),
            ("eh", ""),
        )
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).texto, esperado)

    def test_comandos_actuales_siguen_funcionando(self):
        procesador = ProcesadorTexto(comando_enviar=True)
        casos = (
            ("nuevo párrafo", "nueva_linea"),
            ("punto y aparte", "nueva_linea"),
            ("nueva línea", "nueva_linea"),
            ("borra la última oración", "borrar_ultima"),
            ("detener dictado", "detener"),
            ("stop dictation", "detener"),
            ("enviar", "enviar"),
        )
        for entrada, comando in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(
                    procesador.procesar_frase(entrada).comando, comando)

    def test_comando_citado_se_conserva_como_texto(self):
        procesador = ProcesadorTexto()
        for entrada in ('"detener dictado"', "'detener dictado'"):
            with self.subTest(entrada=entrada):
                resultado = procesador.procesar_frase(entrada)
                self.assertIsNone(resultado.comando)
                self.assertEqual(resultado.texto, entrada)


class PruebasRewrite(unittest.TestCase):
    CRITICOS = (
        "Dale Carnegie", "no viste el error", "no quiero este informe",
        "NASA API", "EM algorithm", "PA system", "user@example.com", "3.14",
    )

    def test_modo_none_no_invoca_ollama(self):
        procesador = ProcesadorTexto(
            rewrite_mode="none", ollama_model="modelo-falso",
            voice_commands=False)
        with mock.patch("urllib.request.urlopen") as urlopen:
            resultado = procesador.procesar_frase("user@example.com")
        urlopen.assert_not_called()
        self.assertEqual(resultado.texto, "user@example.com")

    def test_modos_locales_preservan_casos_criticos(self):
        for modo in ("none", "formal", "concise", "email"):
            procesador = ProcesadorTexto(
                rewrite_mode=modo, voice_commands=False)
            for entrada in self.CRITICOS:
                with self.subTest(modo=modo, entrada=entrada):
                    self.assertEqual(
                        procesador.procesar_frase(entrada).texto, entrada)

    def test_formal_y_email_transforman_solo_reglas_seguras(self):
        esperado = "De acuerdo por favor mandame el informe el fin de semana"
        for modo in ("formal", "email"):
            procesador = ProcesadorTexto(
                rewrite_mode=modo, voice_commands=False)
            with self.subTest(modo=modo):
                self.assertEqual(
                    procesador.procesar_frase(
                        "ok porfa mandame el informe el finde").texto,
                    esperado,
                )
                self.assertEqual(
                    procesador.procesar_frase(
                        "ok user@example.com cuesta $100").texto,
                    "De acuerdo user@example.com cuesta $100",
                )

    def test_concise_elimina_discursivos_sin_borrar_negacion(self):
        procesador = ProcesadorTexto(
            rewrite_mode="concise", voice_commands=False)
        self.assertEqual(
            procesador.procesar_frase(
                "básicamente o sea esto funciona viste").texto,
            "Esto funciona viste",
        )
        self.assertEqual(
            procesador.procesar_frase("no viste el error").texto,
            "no viste el error",
        )

    def test_ollama_opt_in_y_fallback_local_explicito(self):
        procesador = ProcesadorTexto(
            rewrite_mode="formal", ollama_model="modelo-falso",
            voice_commands=False)
        with mock.patch(
                "urllib.request.urlopen",
                return_value=RespuestaOllamaFalsa("Texto transformado")):
            self.assertEqual(
                procesador.procesar_frase("texto original").texto,
                "Texto transformado",
            )
        with mock.patch(
                "urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertEqual(
                procesador.procesar_frase("ok porfa").texto,
                "De acuerdo por favor",
            )


class PruebasFiltroAlucinaciones(unittest.TestCase):
    def test_alta_confianza_url_sospechosa_se_conserva(self):
        self.assertEqual(
            transcribir(("visita www.youtube.com para la clase", 0.01, -0.1)),
            "visita www.youtube.com para la clase",
        )

    def test_alta_confianza_suscribete_se_conserva(self):
        self.assertEqual(
            transcribir(("suscríbete al canal de pruebas", 0.02, -0.2)),
            "suscríbete al canal de pruebas",
        )

    def test_baja_confianza_y_patron_sospechoso_se_filtra(self):
        self.assertEqual(
            transcribir(("gracias por ver el vídeo", 0.95, -2.0)), "")

    def test_baja_confianza_sin_patron_se_conserva(self):
        self.assertEqual(
            transcribir(("frase incierta", 0.95, -2.0)), "frase incierta")

    def test_multiples_segmentos_deciden_individualmente(self):
        observado = transcribir(
            ("contenido válido", 0.01, -0.1),
            ("suscríbete", 0.95, -2.0),
            ("visita www.youtube.com", 0.01, -0.1),
        )
        self.assertEqual(
            observado, "contenido válido visita www.youtube.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
