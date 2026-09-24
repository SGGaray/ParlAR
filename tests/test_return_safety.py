"""Regresiones de la frontera física para Enter/Return."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from parlar.app import App
from parlar.config import Config
from parlar.entrega import EstadoEntrega
from parlar.inyector_salida import Inyector
from parlar.procesador_texto import ProcesadorTexto


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


class PruebasReturnFisico(unittest.TestCase):
    BACKENDS = ("xdotool", "wtype", "ydotool")

    def preparar(self, backend, permitir_return):
        inyector = Inyector(
            backend=backend, notify=False, permitir_return=permitir_return)
        llamadas = []
        copias = []
        correr = mock.patch.object(
            inyector, "_correr",
            side_effect=lambda argv: llamadas.append(argv) or True)
        copiar = mock.patch.object(
            inyector, "_portapapeles",
            side_effect=lambda texto: copias.append(texto) or True)
        copiar_x11 = mock.patch.object(
            inyector, "_copiar_x11",
            side_effect=lambda texto: copias.append(texto) or True)
        dormir = mock.patch("parlar.inyector_salida.time.sleep")
        correr.start()
        copiar.start()
        copiar_x11.start()
        dormir.start()
        self.addCleanup(correr.stop)
        self.addCleanup(copiar.stop)
        self.addCleanup(copiar_x11.stop)
        self.addCleanup(dormir.stop)
        return inyector, llamadas, copias

    @staticmethod
    def llamadas_return(llamadas):
        return [argv for argv in llamadas
                if "Return" in argv or "28:1" in argv or "28:0" in argv]

    def test_multilinea_bloqueada_copia_intacto_en_todos_los_backends(self):
        for backend in self.BACKENDS:
            with self.subTest(backend=backend):
                inyector, llamadas, copias = self.preparar(backend, False)
                resultado = inyector.escribir_texto("hola\nmundo")
                self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
                self.assertEqual(llamadas, [])
                self.assertEqual(copias, ["hola\nmundo"])
                self.assertEqual(inyector._registro_oraciones, [])

    def test_cr_y_crlf_tambien_quedan_detras_del_gate(self):
        for salto in ("\r", "\r\n"):
            with self.subTest(salto=repr(salto)):
                inyector, llamadas, copias = self.preparar("xdotool", False)
                texto = f"hola{salto}mundo"
                self.assertEqual(
                    inyector.escribir_texto(texto).estado,
                    EstadoEntrega.COPIED,
                )
                self.assertEqual(llamadas, [])
                self.assertEqual(copias, [texto])

    def test_multilinea_autorizada_usa_una_return_en_todos_los_backends(self):
        esperadas = {
            "xdotool": [
                ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                ["xdotool", "key", "--clearmodifiers", "Return"],
                ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
            ],
            "wtype": [
                ["wtype", "--", "hola"], ["wtype", "-k", "Return"],
                ["wtype", "--", "mundo"],
            ],
            "ydotool": [
                ["ydotool", "type", "--key-delay", "1", "--", "hola"],
                ["ydotool", "key", "28:1", "28:0"],
                ["ydotool", "type", "--key-delay", "1", "--", "mundo"],
            ],
        }
        for backend in self.BACKENDS:
            with self.subTest(backend=backend):
                inyector, llamadas, copias = self.preparar(backend, True)
                resultado = inyector.escribir_texto("hola\nmundo")
                self.assertEqual(resultado.estado, EstadoEntrega.INSERTED)
                self.assertEqual(llamadas, esperadas[backend])
                self.assertEqual(len(self.llamadas_return(llamadas)), 1)
                self.assertFalse(any("\n" in argumento for argv in llamadas
                                     for argumento in argv))
                self.assertEqual(
                    copias, ["hola", "mundo"] if backend == "xdotool" else [])
                esperado_undo = (
                    [] if backend == "xdotool" else ["hola\nmundo"])
                self.assertEqual(
                    inyector._registro_oraciones, esperado_undo)

    def test_clipboard_preserva_multilinea_con_ambos_valores_del_flag(self):
        for permitir in (False, True):
            with self.subTest(permitir=permitir):
                inyector, llamadas, copias = self.preparar(
                    "clipboard", permitir)
                resultado = inyector.escribir_texto("hola\nmundo")
                self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
                self.assertEqual(llamadas, [])
                self.assertEqual(copias, ["hola\nmundo"])

    def test_comandos_directos_tambien_respetan_la_frontera(self):
        for backend in self.BACKENDS:
            with self.subTest(backend=backend):
                inyector, llamadas, _ = self.preparar(backend, False)
                self.assertFalse(inyector.presionar_enter())
                self.assertFalse(inyector.nueva_linea(2))
                self.assertEqual(llamadas, [])

                inyector.permitir_return = True
                self.assertTrue(inyector.presionar_enter())
                self.assertTrue(inyector.nueva_linea(2))
                self.assertEqual(len(self.llamadas_return(llamadas)), 3)

    def test_streaming_bloqueado_recupera_unidad_y_no_vuelve_a_tipear(self):
        inyector, llamadas, copias = self.preparar("wtype", False)
        inyector.iniciar_unidad(7)
        resultados = [
            inyector.escribir_texto("hola").estado,
            inyector.escribir_texto("\nmundo").estado,
            inyector.escribir_texto(" final").estado,
        ]
        self.assertEqual(
            resultados,
            [EstadoEntrega.INSERTED, EstadoEntrega.COPIED, EstadoEntrega.COPIED],
        )
        self.assertEqual(llamadas, [["wtype", "--", "hola"]])
        self.assertEqual(copias, ["\nmundo", "\nmundo final"])
        self.assertEqual(self.llamadas_return(llamadas), [])

    def test_streaming_autorizado_inserta_y_representa_el_salto(self):
        inyector, llamadas, copias = self.preparar("wtype", True)
        inyector.iniciar_unidad(8)
        resultados = [
            inyector.escribir_texto("hola").estado,
            inyector.escribir_texto("\nmundo").estado,
        ]
        self.assertEqual(
            resultados, [EstadoEntrega.INSERTED, EstadoEntrega.INSERTED])
        self.assertEqual(len(self.llamadas_return(llamadas)), 1)
        self.assertEqual(copias, [])

    def test_rewrite_ollama_multilinea_no_elude_el_flag(self):
        procesador = ProcesadorTexto(
            rewrite_mode="formal", ollama_model="modelo-falso",
            voice_commands=False)
        with mock.patch(
                "urllib.request.urlopen",
                return_value=RespuestaOllamaFalsa("Hola.\nSaludos.")):
            texto = procesador.procesar_frase("hola").texto
        self.assertEqual(texto, "Hola.\nSaludos.")

        inyector, llamadas, copias = self.preparar("wtype", False)
        resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
        self.assertEqual(llamadas, [])
        self.assertEqual(copias, ["Hola.\nSaludos."])

        inyector, llamadas, copias = self.preparar("wtype", True)
        resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.INSERTED)
        self.assertEqual(len(self.llamadas_return(llamadas)), 1)
        self.assertEqual(copias, [])

    def test_app_propaga_la_autorizacion_al_inyector_productivo(self):
        dependencias = {
            nombre: SimpleNamespace(es_nulo=True)
            for nombre in ("frases", "streaming", "proc", "guionar", "sesion",
                           "mic", "ui", "control", "atajos")
        }
        for permitir in (False, True):
            with self.subTest(permitir=permitir):
                cfg = Config(comando_enviar=permitir)
                with mock.patch("parlar.app.Inyector") as constructor:
                    App(cfg, motor=object(), **dependencias)
                constructor.assert_called_once_with(
                    cfg.injector, cfg.type_delay_ms, cfg.notify, permitir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
