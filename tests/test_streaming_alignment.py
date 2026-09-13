"""Regresiones adversariales para el acuerdo append-only de streaming."""

import unittest

import numpy as np

from parlar.motor_transcripcion import TranscriptorStreaming, _norm


class PalabraFalsa:
    def __init__(self, texto, fin, inicio=None):
        self.word = texto
        self.end = fin
        self.start = inicio


class SegmentoFalso:
    def __init__(self, palabras):
        self.words = palabras


class MotorGuionado:
    """Una hipótesis por llamada; cada token puede incluir su timestamp."""

    def __init__(self, hipotesis):
        self.hipotesis = list(hipotesis)
        self.llamadas = 0

    def decodificar(self, audio, word_timestamps=False, beam_size=None):
        spec = self.hipotesis[min(self.llamadas, len(self.hipotesis) - 1)]
        self.llamadas += 1
        palabras = []
        for i, item in enumerate(spec):
            if isinstance(item, tuple) and len(item) == 3:
                texto, inicio, fin = item
            elif isinstance(item, tuple):
                texto, fin = item
                inicio = None
            else:
                texto, fin = item, 0.35 * (i + 1)
                inicio = None
            palabras.append(PalabraFalsa(" " + texto, fin, inicio))
        return [SegmentoFalso(palabras)]


class MotorProgresoEstable:
    """Emite el mismo token dos veces y lo ubica cerca del final del audio."""

    def __init__(self, sample_rate):
        self.sr = sample_rate
        self.llamadas = 0

    def decodificar(self, audio, word_timestamps=False, beam_size=None):
        token = f"w{self.llamadas // 2}"
        self.llamadas += 1
        fin = max(0.1, audio.size / self.sr - 0.05)
        return [SegmentoFalso([PalabraFalsa(" " + token, fin)])]


def crear(hipotesis, *, trim_s=999, sample_rate=16000):
    return TranscriptorStreaming(
        MotorGuionado(hipotesis), sample_rate=sample_rate,
        interval_s=0.1, trim_s=trim_s)


def paso(st, segundos=1.0):
    st.aceptar_audio(np.zeros(int(st.sr * segundos), dtype=np.float32))
    return st.procesar()


def texto(chunks):
    return " ".join("".join(chunks).split())


class PruebasAlineacionStreaming(unittest.TestCase):
    def test_prefijo_simple_creciente(self):
        st = crear([
            ["hola"],
            ["hola", "mundo"],
            ["hola", "mundo"],
            ["hola", "mundo"],
        ])
        chunks = [paso(st), paso(st), paso(st), st.finalizar()]
        self.assertEqual(texto(chunks), "hola mundo")
        self.assertEqual(texto(chunks).count("hola"), 1)
        self.assertEqual(texto(chunks).count("mundo"), 1)

    def test_repeticion_legitima_incremental(self):
        st = crear([
            ["very"],
            ["very", "very"],
            ["very", "very", "good"],
            ["very", "very", "good"],
            ["very", "very", "good"],
        ])
        chunks = [paso(st) for _ in range(4)] + [st.finalizar()]
        self.assertEqual(texto(chunks), "very very good")

    def test_insercion_antes_de_committed_no_duplica(self):
        st = crear([
            ["very", "good"],
            ["very", "good"],
            ["very", "very", "good"],
            ["very", "very", "good"],
            ["very", "very", "good"],
        ])
        chunks = [paso(st) for _ in range(4)] + [st.finalizar()]
        # "good" ya fue irrevocablemente emitido: el segundo "very" no se
        # puede insertar delante sin retractar. La política segura es congelar
        # esa inserción tardía, no inventar un sufijo duplicado.
        self.assertEqual(texto(chunks), "very good")
        self.assertNotIn("very good good", texto(chunks))
        self.assertNotIn("very good very good", texto(chunks))
        self.assertGreater(st.divergencias, 0)

    def test_eliminacion_temporal_se_recupera_sin_perder_sufijo(self):
        st = crear([
            ["I", "think", "this"],
            ["I", "think", "this"],
            ["I", "think"],
            ["I", "think", "this", "works"],
            ["I", "think", "this", "works"],
        ])
        chunks = [paso(st) for _ in range(4)] + [st.finalizar()]
        self.assertEqual(texto(chunks), "I think this works")

    def test_hipotesis_mucho_mas_corta_no_indexa_ni_recorta(self):
        st = crear([
            ["a", "b", "c"],
            ["a", "b", "c"],
            ["a"],
            ["a"],
        ], trim_s=2.5)
        chunks = [paso(st), paso(st), paso(st)]
        self.assertEqual(st.buffer.size, 3 * st.sr)
        chunks.append(st.finalizar())
        self.assertEqual(texto(chunks), "a b c")

    def test_divergencia_temprana_no_confirma_token_contradictorio(self):
        st = crear([
            ["turn", "on", "the", "light"],
            ["turn", "on", "the", "light"],
            ["turn", "off", "the", "light"],
            ["turn", "off", "the", "light"],
        ])
        chunks = [paso(st), paso(st), paso(st), st.finalizar()]
        self.assertEqual(texto(chunks), "turn on the light")
        self.assertNotIn("off", texto(chunks))

    def test_repeticiones_naturales_completas(self):
        frases = [
            "hello hello",
            "I think I think this works",
            "the result is is correct",
            "New York New York",
            "very very good",
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                tokens = frase.split()
                st = crear([tokens, tokens, tokens])
                resultado = texto([paso(st), paso(st), st.finalizar()])
                self.assertEqual(resultado, frase)

    def test_puntuacion_y_mayusculas_cosmeticas(self):
        st = crear([
            ["hola", "mundo"],
            ["Hola,", "mundo."],
            ["Hola,", "mundo."],
            ["Hola,", "mundo."],
        ])
        chunks = [paso(st), paso(st), paso(st), st.finalizar()]
        self.assertEqual(texto(chunks), "Hola, mundo.")

    def test_normalizacion_no_fusiona_tokens_tecnicos(self):
        tecnicos = ["C++", "C#", "foo.bar", "foo_bar", "HTTP/2", "3.14"]
        normalizados = [_norm(token) for token in tecnicos]
        self.assertEqual(len(normalizados), len(set(normalizados)))
        self.assertEqual(_norm("Hola,"), _norm("hola"))
        self.assertNotEqual(_norm("C++"), _norm("C#"))
        self.assertNotEqual(_norm("foo.bar"), _norm("foo_bar"))

    def test_unicode_no_corrompe_tokens(self):
        tokens = ["niñez", "canción", "¿cómo?", "¡bien!", "🙂", "漢字"]
        st = crear([tokens, tokens, tokens])
        resultado = texto([paso(st), paso(st), st.finalizar()])
        self.assertEqual(resultado, " ".join(tokens))
        self.assertTrue(all(_norm(token) for token in tokens))

    def test_final_inserta_antes_de_committed_sin_duplicar(self):
        st = crear([
            ["very", "good"],
            ["very", "good"],
            ["very", "very", "good"],
        ])
        chunks = [paso(st), paso(st), st.finalizar()]
        self.assertEqual(texto(chunks), "very good")

    def test_final_alinea_sufijo_committed_y_conserva_texto_nuevo(self):
        st = crear([
            ["I", "think"],
            ["I", "think"],
            ["think", "this", "works"],
        ])
        chunks = [paso(st), paso(st), st.finalizar()]
        self.assertEqual(texto(chunks), "I think this works")

    def test_final_mucho_mas_corto_no_duplica(self):
        st = crear([
            ["a", "b", "c"],
            ["a", "b", "c"],
            ["a"],
        ])
        chunks = [paso(st), paso(st), st.finalizar()]
        self.assertEqual(texto(chunks), "a b c")

    def test_divergencia_impide_trim_hasta_realinear(self):
        st = crear([
            ["a", "b", "c"],
            ["a", "b", "c"],
            ["a"],
        ], trim_s=2.5)
        paso(st)
        paso(st)
        paso(st)
        self.assertEqual(st.buffer.size, 3 * st.sr)

    def test_timestamp_fuera_del_buffer_no_autoriza_trim(self):
        st = crear([
            [("hola", 999.0)],
            [("hola", 999.0)],
        ], trim_s=1.5)
        paso(st)
        paso(st)
        self.assertEqual(st.buffer.size, 2 * st.sr)

    def test_timestamps_invalidos_no_autorizan_trim(self):
        casos = [
            [[("hola", -1.0)], [("hola", -1.0)]],
            [[("hola", float("nan"))], [("hola", float("nan"))]],
            [
                [("a", 1.0), ("b", 0.5)],
                [("a", 1.0), ("b", 0.5)],
            ],
        ]
        for hipotesis in casos:
            with self.subTest(hipotesis=hipotesis):
                st = crear(hipotesis, trim_s=1.5)
                paso(st)
                paso(st)
                self.assertEqual(st.buffer.size, 2 * st.sr)

    def test_no_recorta_audio_de_palabra_pendiente_superpuesta(self):
        st = crear([
            [("hola", 0.0, 0.8), ("mundo", 0.7, 1.4)],
            [("hola", 0.0, 0.8), ("mundx", 0.7, 1.4)],
        ], trim_s=1.5)
        paso(st)
        paso(st)
        self.assertEqual(st.buffer.size, 2 * st.sr)

    def test_buffer_acotado_con_progreso_estable(self):
        sr = 16000
        st = TranscriptorStreaming(
            MotorProgresoEstable(sr), sample_rate=sr,
            interval_s=0.1, trim_s=1.5)
        chunks = []
        max_buffer = 0
        for _ in range(100):
            chunks.append(paso(st))
            max_buffer = max(max_buffer, st.buffer.size)
        self.assertLessEqual(max_buffer, 2 * sr)
        self.assertEqual(texto(chunks), " ".join(f"w{i}" for i in range(50)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
