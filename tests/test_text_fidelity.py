"""Regresiones de fidelidad post-Whisper y filtrado conservador."""

import json
import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App
from parlar.motor_transcripcion import (
    TranscriptorFrase,
    TranscriptorStreaming,
    _clasificar_alucinacion,
)
from parlar.procesador_texto import ProcesadorTexto


def segmento(texto, no_speech, logprob, *, duracion=1.0, compresion=1.0):
    palabras = [
        SimpleNamespace(
            word=" " + token, start=indice * 0.2, end=(indice + 1) * 0.2)
        for indice, token in enumerate(texto.split())
    ]
    return SimpleNamespace(
        text=texto,
        start=0.0,
        end=duracion,
        no_speech_prob=no_speech,
        avg_logprob=logprob,
        compression_ratio=compresion,
        words=palabras,
    )


class MotorSegmentos:
    def __init__(self, segmentos):
        self.segmentos = segmentos

    def decodificar(self, audio, **_kwargs):
        return [
            (especificacion if hasattr(especificacion, "text")
             else segmento(*especificacion))
            for especificacion in self.segmentos
        ]


def transcribir(*segmentos):
    motor = MotorSegmentos(segmentos)
    audio = np.zeros(1600, dtype=np.float32)
    return TranscriptorFrase(motor).transcribir(audio)


class RespuestaOllamaFalsa:
    def __init__(self, texto):
        self.data = json.dumps({"response": texto}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, cantidad):
        trozo, self.data = self.data[:cantidad], self.data[cantidad:]
        return trozo


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

    def test_concise_preserva_calificadores_y_negacion(self):
        procesador = ProcesadorTexto(
            rewrite_mode="concise", voice_commands=False)
        self.assertEqual(
            procesador.procesar_frase(
                "básicamente o sea esto funciona viste").texto,
            "básicamente o sea esto funciona viste",
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
    AMARA_CONTEXTO = (
        "Subtítulos del informe: no los publiques en amara.org.",
        "Estoy revisando los subtítulos que descargué de amara.org.",
        "Amara.org es una plataforma de subtítulos.",
        "Los subtítulos mencionan a amara.org en la bibliografía.",
        "No uses los subtítulos de amara.org para esta entrega.",
        "La frase contiene subtítulos y después menciona amara.org.",
    )
    AMARA_CONOCIDO = (
        "Subtítulos realizados por la comunidad de Amara.org",
        "Subtítulos por la comunidad de Amara.org",
    )

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

    def test_segmento_completo_con_una_sola_senal_debil_se_filtra(self):
        casos = (
            ("¡Suscríbete!", 0.75, -0.2, 1.0),
            ("Subtítulos realizados por la comunidad de Amara.org",
             0.1, -1.2, 1.0),
            ("¡Suscríbete!", 0.1, -0.2, 2.5),
        )
        for texto, no_speech, logprob, compresion in casos:
            with self.subTest(texto=texto, compresion=compresion):
                self.assertEqual(
                    transcribir(segmento(
                        texto, no_speech, logprob, compresion=compresion)),
                    "",
                )

    def test_contexto_legitimo_se_conserva_aun_con_metricas_debiles(self):
        for texto in (
            "La palabra del ejemplo es suscríbete",
            "Amara.org es una plataforma de subtítulos",
        ):
            with self.subTest(texto=texto):
                self.assertEqual(transcribir((texto, 0.95, -2.0)), texto)

    def test_contexto_amara_sobrevive_cada_metrica_sospechosa(self):
        texto = "Subtítulos del informe: no los publiques en amara.org"
        self.assertIsNone(_clasificar_alucinacion(texto))
        casos = (
            segmento(texto, 0.95, -0.2, compresion=1.0),
            segmento(texto, 0.1, -1.2, compresion=1.0),
            segmento(texto, 0.1, -0.2, compresion=2.5),
        )
        for caso in casos:
            with self.subTest(
                    no_speech=caso.no_speech_prob,
                    logprob=caso.avg_logprob,
                    compresion=caso.compression_ratio):
                self.assertEqual(transcribir(caso), texto)

    def test_menciones_contextuales_amara_siempre_se_conservan(self):
        for texto in self.AMARA_CONTEXTO:
            with self.subTest(texto=texto):
                self.assertNotEqual(
                    _clasificar_alucinacion(texto), "exact_or_near_exact")
                self.assertEqual(
                    transcribir(segmento(
                        texto, 0.95, -2.0, compresion=2.5)),
                    texto,
                )

    def test_plantillas_amara_explicitas_conservan_clasificacion(self):
        variantes = self.AMARA_CONOCIDO + (
            "¡Subtítulos realizados por la comunidad de Amara.org!",
            "SUBTÍTULOS POR LA COMUNIDAD DE AMARA.ORG.",
        )
        for texto in variantes:
            with self.subTest(texto=texto):
                self.assertEqual(
                    _clasificar_alucinacion(texto), "exact_or_near_exact")

    def test_plantilla_amara_requiere_una_metrica_sospechosa(self):
        for texto in self.AMARA_CONOCIDO:
            with self.subTest(texto=texto, metricas="sospechosas"):
                self.assertEqual(
                    transcribir(segmento(texto, 0.75, -0.2)), "")
            with self.subTest(texto=texto, metricas="sanas"):
                self.assertEqual(
                    transcribir(segmento(texto, 0.02, -0.2)), texto)

    def test_contexto_amara_sobrevive_en_streaming(self):
        texto = "Subtítulos del informe: no los publiques en amara.org"
        motor = MotorSegmentos([segmento(texto, 0.95, -2.0)])
        streaming = TranscriptorStreaming(
            motor, sample_rate=16000, interval_s=0.1, trim_s=999)
        audio = np.zeros(16000, dtype=np.float32)
        streaming.aceptar_audio(audio)
        self.assertEqual(streaming.procesar(), "")
        streaming.aceptar_audio(audio)
        self.assertEqual(streaming.procesar().strip(), texto)

    def test_contexto_amara_llega_al_sink_desde_app(self):
        texto = "Subtítulos del informe: no los publiques en amara.org"
        app = App.__new__(App)
        app.frases = TranscriptorFrase(MotorSegmentos([
            segmento(texto, 0.95, -2.0),
        ]))
        app.proc = ProcesadorTexto(voice_commands=False)
        app._estado_visual_si_vigente = lambda *_args: None
        emitidos = []
        app._emitir = lambda procesado, generacion: emitidos.append(
            (procesado.texto, generacion))

        app._atender_frase(np.zeros(1600, dtype=np.float32), 7)

        self.assertEqual(emitidos, [(texto, 7)])

    def test_literal_completo_con_evidencia_fuerte_se_conserva(self):
        self.assertEqual(
            transcribir(("Suscríbete.", 0.02, -0.2)),
            "Suscríbete.",
        )

    def test_multiples_segmentos_deciden_individualmente(self):
        observado = transcribir(
            ("contenido válido", 0.01, -0.1),
            ("suscríbete", 0.95, -2.0),
            ("visita www.youtube.com", 0.01, -0.1),
        )
        self.assertEqual(
            observado, "contenido válido visita www.youtube.com")

    def test_secuencia_no_arrastra_estado_entre_segmentos(self):
        observado = transcribir(
            ("contenido real", 0.01, -0.1),
            ("¡Suscríbete!", 0.75, -0.2),
            ("gracias por ver el vídeo", 0.1, -1.2),
            ("contenido posterior", 0.01, -0.1),
        )
        self.assertEqual(observado, "contenido real contenido posterior")

    def test_diagnostico_no_expone_texto(self):
        for texto in (
            "¡Suscríbete!",
            "Subtítulos realizados por la comunidad de Amara.org",
        ):
            with self.subTest(texto=texto):
                diagnostico = io.StringIO()
                with redirect_stderr(diagnostico):
                    self.assertEqual(transcribir((texto, 0.75, -0.2)), "")
                log = diagnostico.getvalue()
                self.assertIn("classification=exact_or_near_exact", log)
                self.assertIn("decision=drop", log)
                self.assertIn("duration_ms=1000.000", log)
                self.assertIn("no_speech_prob=0.750", log)
                self.assertIn("avg_logprob=-0.200", log)
                self.assertIn("compression_ratio=1.000", log)
                self.assertNotIn(texto, log)

    def test_streaming_aplica_la_misma_politica_por_segmento(self):
        class MotorStreaming:
            def __init__(self, segmentos):
                self.segmentos = segmentos

            def decodificar(self, audio, word_timestamps=False, beam_size=None):
                return self.segmentos

        motor = MotorStreaming([
            segmento("contenido real", 0.01, -0.1),
            segmento("¡Suscríbete!", 0.75, -0.2),
        ])
        streaming = TranscriptorStreaming(
            motor, sample_rate=16000, interval_s=0.1, trim_s=999)
        audio = np.zeros(16000, dtype=np.float32)
        streaming.aceptar_audio(audio)
        self.assertEqual(streaming.procesar(), "")
        streaming.aceptar_audio(audio)
        self.assertEqual(streaming.procesar().strip(), "contenido real")
        self.assertEqual(streaming.finalizar(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
