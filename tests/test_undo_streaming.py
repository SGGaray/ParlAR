"""Composición de undo con unidades streaming y sus fronteras físicas."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from parlar.app import App
from parlar.entrega import EstadoEntrega
from parlar.inyector_salida import Inyector
from parlar.motor_transcripcion import TranscriptorStreaming
from parlar.procesador_texto import Procesado, ProcesadorTexto
from tests.test_streaming_alignment import MotorGuionado


class SinkNulo:
    es_nulo = True


def app_minima(inyector):
    app = App.__new__(App)
    app._salida_lock = threading.RLock()
    app._necesita_espacio = False
    app.cfg = SimpleNamespace(comando_enviar=False)
    app.inyector = inyector
    app.guionar = SinkNulo()
    app.sesion = SinkNulo()
    app.proc = ProcesadorTexto()
    app._puede_emit = lambda generacion: generacion == 1
    return app


def emitir_unidad(inyector, fragmentos, *, resultados=None, finalizar=True):
    if resultados is None:
        resultados = [True] * len(fragmentos)
    inyector.iniciar_unidad(1)
    with mock.patch.object(inyector, "_tipear", side_effect=resultados):
        estados = [
            inyector.escribir_texto(texto, registrar=False).estado
            for texto in fragmentos
        ]
    if finalizar:
        inyector.finalizar_unidad()
    return estados


class PruebasUndoStreaming(unittest.TestCase):
    def test_utterance_incierta_no_atraviesa_historial_con_backspace(self):
        for fallo in ("false", "exception"):
            with self.subTest(fallo=fallo):
                inyector = Inyector(backend="xdotool", notify=False)
                app = app_minima(inyector)
                editor = [""]
                fase = ["primero"]
                retrocesos = []
                copias = []
                transporte_x11 = [""]

                def copiar_x11(texto):
                    # Modela solo el intervalo entre xclip y Ctrl+V; la ruta
                    # física no garantiza disponibilidad después del pegado.
                    transporte_x11[0] = texto
                    return True

                def correr(argv):
                    if argv[-1] == "ctrl+v":
                        texto = transporte_x11[0]
                        if fase[0] == "primero":
                            editor[0] += texto
                            return True
                        editor[0] += texto[:4]
                        if fallo == "exception":
                            raise OSError("fallo físico simulado")
                        return False
                    cantidad = int(argv[argv.index("--repeat") + 1])
                    retrocesos.append(cantidad)
                    editor[0] = editor[0][:-cantidad]
                    return True

                with (
                    mock.patch.object(inyector, "_correr", side_effect=correr),
                    mock.patch.object(
                        inyector, "_copiar_x11", side_effect=copiar_x11,
                    ),
                    mock.patch.object(
                        inyector, "_portapapeles",
                        side_effect=lambda texto: copias.append(texto) or True,
                    ),
                    mock.patch("parlar.inyector_salida.time.sleep"),
                ):
                    inyector.iniciar_unidad(1)
                    app._emitir(Procesado(texto="primero"), 1)
                    inyector.finalizar_unidad()
                    self.assertEqual(
                        inyector._registro_oraciones, ["primero"])

                    fase[0] = "incierta"
                    inyector.iniciar_unidad(1)
                    resultado = app._emitir(
                        Procesado(texto="abcdefghijk"), 1)
                    inyector.finalizar_unidad()
                    self.assertEqual(
                        resultado.inyector, EstadoEntrega.COPIED)
                    self.assertEqual(copias, [" abcdefghijk"])
                    self.assertEqual(editor[0], "primero abc")

                    app._emitir(app.proc.procesar_frase(
                        "borra la última oración"), 1)

                self.assertEqual(inyector._registro_oraciones, [])
                self.assertEqual(retrocesos, [])
                self.assertEqual(editor[0], "primero abc")

    def test_utterance_clipboard_directo_conserva_historial(self):
        inyector = Inyector(backend="xdotool", notify=False)
        with mock.patch.object(inyector, "_tipear", return_value=True):
            inyector.escribir_texto("primero")
        inyector.backend = "clipboard"
        with mock.patch.object(inyector, "_portapapeles", return_value=True):
            resultado = inyector.escribir_texto("solo copiado")
        self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
        self.assertEqual(inyector._registro_oraciones, ["primero"])

    def test_utterance_streaming_local_agreement_y_dos_undos(self):
        inyector = Inyector(backend="xdotool", notify=False)
        app = app_minima(inyector)
        streaming = TranscriptorStreaming(
            MotorGuionado([
                ["continuación"], ["continuación"], ["continuación"],
            ]),
            sample_rate=16000, interval_s=0.1, trim_s=999,
        )

        with mock.patch.object(inyector, "_tipear", return_value=True):
            app._emitir(Procesado(texto="primero"), 1)
            self.assertEqual(inyector._registro_oraciones, ["primero"])

            inyector.iniciar_unidad(1)
            for _ in range(2):
                streaming.aceptar_audio(np.zeros(16000, dtype=np.float32))
                fragmento = streaming.procesar()
                if fragmento:
                    app._escribir_en_todas(fragmento, 1)
            cola = streaming.finalizar()
            if cola:
                app._escribir_en_todas(cola, 1)
            inyector.finalizar_unidad()

        self.assertEqual(
            inyector._registro_oraciones, ["primero", " continuación"])
        intentos = []
        with mock.patch.object(
                inyector, "retroceso",
                side_effect=lambda cantidad: intentos.append(cantidad) or True):
            app._emitir(app.proc.procesar_frase(
                "borra la última oración"), 1)
            app._emitir(app.proc.procesar_frase(
                "borra la última oración"), 1)
        self.assertEqual(intentos, [len(" continuación"), len("primero")])
        self.assertEqual(inyector._registro_oraciones, [])

    def test_multifragmento_es_una_entrada_y_un_intento(self):
        inyector = Inyector(backend="xdotool", notify=False)
        estados = emitir_unidad(inyector, ["hola", " mundo", "."])
        self.assertEqual(estados, [EstadoEntrega.INSERTED] * 3)
        self.assertEqual(inyector._registro_oraciones, ["hola mundo."])
        with mock.patch.object(inyector, "retroceso", return_value=True) as borrar:
            self.assertTrue(inyector.borrar_ultima_oracion())
        borrar.assert_called_once_with(len("hola mundo."))

    def test_vacia_y_copy_only_preservan_historial_anterior(self):
        inyector = Inyector(backend="xdotool", notify=False)
        with mock.patch.object(inyector, "_tipear", return_value=True):
            inyector.escribir_texto("primero")

        emitir_unidad(inyector, [])
        self.assertEqual(inyector._registro_oraciones, ["primero"])

        inyector.backend = "clipboard"
        with mock.patch.object(inyector, "_portapapeles", return_value=True):
            estados = emitir_unidad(
                inyector, ["solo", " copiado"], resultados=[False])
        self.assertEqual(estados, [EstadoEntrega.COPIED] * 2)
        self.assertEqual(inyector._registro_oraciones, ["primero"])

    def test_mixta_e_incierta_invalidan_historial(self):
        for copia_ok, esperado in ((True, EstadoEntrega.COPIED),
                                   (False, EstadoEntrega.FAILED)):
            with self.subTest(copia_ok=copia_ok):
                inyector = Inyector(backend="xdotool", notify=False)
                with mock.patch.object(inyector, "_tipear", return_value=True):
                    inyector.escribir_texto("primero")
                inyector.iniciar_unidad(1)
                with (
                    mock.patch.object(
                        inyector, "_tipear", side_effect=[True, False]),
                    mock.patch.object(
                        inyector, "_portapapeles", return_value=copia_ok),
                ):
                    self.assertEqual(
                        inyector.escribir_texto(
                            "hola", registrar=False).estado,
                        EstadoEntrega.INSERTED,
                    )
                    self.assertEqual(
                        inyector.escribir_texto(
                            " mundo", registrar=False).estado,
                        esperado,
                    )
                inyector.finalizar_unidad()
                self.assertEqual(inyector._registro_oraciones, [])
                self.assertFalse(inyector.borrar_ultima_oracion())

    def test_fallo_inicial_fisicamente_incierto_invalida(self):
        inyector = Inyector(backend="xdotool", notify=False)
        with mock.patch.object(inyector, "_tipear", return_value=True):
            inyector.escribir_texto("primero")
        inyector.iniciar_unidad(1)
        with (
            mock.patch.object(inyector, "_tipear", return_value=False),
            mock.patch.object(inyector, "_portapapeles", return_value=False),
        ):
            self.assertEqual(
                inyector.escribir_texto("incierto", registrar=False).estado,
                EstadoEntrega.FAILED,
            )
        inyector.finalizar_unidad()
        self.assertEqual(inyector._registro_oraciones, [])

    def test_gap_finalizado_registra_y_cancel_posterior_no_duplica(self):
        inyector = Inyector(backend="xdotool", notify=False)
        emitir_unidad(inyector, ["antes", " del gap"])
        inyector.cancelar_unidad()
        self.assertEqual(inyector._registro_oraciones, ["antes del gap"])

    def test_cancel_y_shutdown_no_inventan_unidad(self):
        for cerrar in (False, True):
            with self.subTest(shutdown=cerrar):
                inyector = Inyector(backend="xdotool", notify=False)
                with mock.patch.object(inyector, "_tipear", return_value=True):
                    inyector.escribir_texto("anterior")
                emitir_unidad(
                    inyector, ["incompleta"], resultados=[True],
                    finalizar=False,
                )
                if cerrar:
                    inyector.cerrar()
                else:
                    inyector.cancelar_unidad()
                self.assertEqual(inyector._registro_oraciones, [])

    def test_restart_descarta_bookkeeping_y_final_tardio_no_registra(self):
        inyector = Inyector(backend="xdotool", notify=False)
        emitir_unidad(
            inyector, ["generación vieja"], resultados=[True],
            finalizar=False,
        )
        inyector.reiniciar_registro()
        inyector.finalizar_unidad()
        self.assertEqual(inyector._registro_oraciones, [])

    def test_stop_finaliza_una_vez_y_undo_fallido_conserva_tope(self):
        inyector = Inyector(backend="xdotool", notify=False)
        emitir_unidad(inyector, ["stop", " válido"])
        inyector.finalizar_unidad()
        self.assertEqual(inyector._registro_oraciones, ["stop válido"])
        with mock.patch.object(
                inyector, "retroceso", side_effect=[False, True]) as borrar:
            self.assertFalse(inyector.borrar_ultima_oracion())
            self.assertEqual(inyector._registro_oraciones, ["stop válido"])
            self.assertTrue(inyector.borrar_ultima_oracion())
        self.assertEqual(borrar.call_count, 2)

    def test_limite_20_guarda_unidades_completas_unicode(self):
        inyector = Inyector(backend="xdotool", notify=False)
        unidades = ["á🙂", "mañana", "acción"] + [f"unidad-{i}" for i in range(22)]
        for unidad in unidades:
            emitir_unidad(inyector, [unidad[:1], unidad[1:]])
        self.assertEqual(len(inyector._registro_oraciones), 20)
        self.assertEqual(inyector._registro_oraciones, unidades[-20:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
