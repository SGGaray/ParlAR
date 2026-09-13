"""Contratos de distribución, clipboard, undo y acciones Return."""

import contextlib
import io
import subprocess
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


class PruebasXdotoolUnicode(unittest.TestCase):
    CORPUS = (
        "cómo",
        "están",
        "podrá",
        "rápido",
        "últimamente",
        "pingüino",
        "diseñador",
        "¿Qué?",
        "á é í ó ú Á É Í Ó Ú ñ Ñ ¿ ¡",
        "¿Cómo están? ¿Qué podrás hacer? Será rápido. "
        "Últimamente está funcionando.",
    )

    @staticmethod
    def _disponible(nombre):
        return f"/usr/bin/{nombre}" if nombre == "xclip" else None

    @staticmethod
    def _resultado_ok(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0)

    def test_auto_x11_exige_xdotool_y_xclip(self):
        for disponibles, esperado in (
            ({"xdotool", "xclip"}, "xdotool"),
            ({"xdotool"}, "clipboard"),
        ):
            with self.subTest(disponibles=disponibles):
                with (
                    mock.patch(
                        "parlar.inyector_salida.detectar_sesion",
                        return_value="x11",
                    ),
                    mock.patch(
                        "parlar.inyector_salida._cual",
                        side_effect=lambda nombre: (
                            f"/usr/bin/{nombre}"
                            if nombre in disponibles else None
                        ),
                    ),
                ):
                    self.assertEqual(
                        Inyector(backend="auto", notify=False).backend,
                        esperado,
                    )

    def test_xclip_mas_paste_inserta_y_conserva_undo(self):
        texto = "¿Cómo están? El pingüino habló con el diseñador."
        inyector = Inyector(backend="xdotool", notify=False)
        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=self._resultado_ok,
            ) as ejecutar,
            mock.patch("parlar.inyector_salida.time.sleep") as dormir,
        ):
            resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.INSERTED)
        self.assertEqual(inyector._registro_oraciones, [texto])
        self.assertEqual(ejecutar.call_count, 2)
        self.assertEqual(
            ejecutar.call_args_list[0],
            mock.call(
                ["xclip", "-selection", "clipboard"],
                input=texto.encode(), check=True, timeout=5,
            ),
        )
        self.assertEqual(
            ejecutar.call_args_list[1],
            mock.call(
                ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                capture_output=True, check=False, timeout=30,
            ),
        )
        self.assertEqual(
            dormir.call_args_list, [mock.call(0.05), mock.call(0.05)])
        with mock.patch.object(inyector, "retroceso", return_value=True) as borrar:
            self.assertTrue(inyector.borrar_ultima_oracion())
        borrar.assert_called_once_with(len(texto))

    def test_corpus_unicode_llega_sin_mutacion(self):
        inyector = Inyector(backend="xdotool", notify=False)
        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=self._resultado_ok,
            ) as ejecutar,
            mock.patch("parlar.inyector_salida.time.sleep"),
        ):
            estados = [inyector.escribir_texto(texto).estado
                       for texto in self.CORPUS]
        self.assertEqual(estados, [EstadoEntrega.INSERTED] * len(self.CORPUS))
        copias = [llamada for llamada in ejecutar.call_args_list
                  if llamada.args[0][0] == "xclip"]
        pegados = [llamada for llamada in ejecutar.call_args_list
                   if llamada.args[0][0] == "xdotool"]
        self.assertEqual(
            [llamada.kwargs["input"].decode() for llamada in copias],
            list(self.CORPUS),
        )
        self.assertEqual(len(pegados), len(self.CORPUS))
        self.assertTrue(all(
            llamada.args[0] ==
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
            for llamada in pegados))

    def test_rafaga_de_quince_invocaciones_conserva_orden_y_contenido(self):
        base = ("¿Cómo están? ¿Qué podrás hacer? Será rápido. "
                "Últimamente está funcionando. ")
        entradas = [f"[{indice}] {base}" for indice in range(1, 16)]
        inyector = Inyector(backend="xdotool", notify=False)
        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=self._resultado_ok,
            ) as ejecutar,
            mock.patch("parlar.inyector_salida.time.sleep"),
        ):
            for entrada in entradas:
                self.assertEqual(
                    inyector.escribir_texto(entrada).estado,
                    EstadoEntrega.INSERTED,
                )
        self.assertEqual(len(ejecutar.call_args_list), 30)
        self.assertEqual(
            [llamada.kwargs["input"].decode()
             for llamada in ejecutar.call_args_list[::2]],
            entradas,
        )
        self.assertTrue(all(
            llamada.args[0] ==
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
            for llamada in ejecutar.call_args_list[1::2]))

    def test_fragmentos_streaming_conservan_orden_y_contenido(self):
        fragmentos = ("¿Cómo", " están?", " Será", " rápido.",
                      " Últimamente", " está", " funcionando.")
        inyector = Inyector(backend="xdotool", notify=False)
        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=self._resultado_ok,
            ) as ejecutar,
            mock.patch("parlar.inyector_salida.time.sleep"),
        ):
            inyector.iniciar_unidad(1)
            for fragmento in fragmentos:
                self.assertEqual(
                    inyector.escribir_texto(
                        fragmento, registrar=False).estado,
                    EstadoEntrega.INSERTED,
                )
            inyector.finalizar_unidad()
        self.assertEqual(
            [llamada.kwargs["input"].decode()
             for llamada in ejecutar.call_args_list[::2]],
            list(fragmentos),
        )
        self.assertEqual(len(ejecutar.call_args_list[1::2]), len(fragmentos))
        self.assertEqual(inyector._registro_oraciones, ["".join(fragmentos)])

    def test_clipboard_explicito_solo_copia(self):
        texto = "á é í ó ú ñ ü ¿ ¡"
        inyector = Inyector(backend="clipboard", notify=False)
        with (
            mock.patch(
                "parlar.inyector_salida.detectar_sesion", return_value="x11"),
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=self._resultado_ok,
            ) as ejecutar,
        ):
            resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
        ejecutar.assert_called_once_with(
            ["xclip", "-selection", "clipboard"],
            input=texto.encode(), check=True, timeout=5,
        )
        self.assertEqual(inyector._registro_oraciones, [])

    def test_fallo_de_xclip_no_pega_ni_invalida_historial(self):
        texto = "¿Qué podrás hacer?"
        inyector = Inyector(backend="xdotool", notify=False)
        inyector._registro_oraciones[:] = ["anterior"]
        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["xclip"]),
            ) as ejecutar,
            mock.patch("parlar.inyector_salida.time.sleep") as dormir,
        ):
            resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.FAILED)
        self.assertEqual(ejecutar.call_count, 1)
        dormir.assert_not_called()
        self.assertEqual(inyector._registro_oraciones, ["anterior"])

    def test_fallo_de_paste_reporta_copy_e_invalida_historial(self):
        texto = "Será rápido."
        inyector = Inyector(backend="xdotool", notify=False)
        inyector._registro_oraciones[:] = ["anterior"]

        def ejecutar(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv, 1 if argv[0] == "xdotool" else 0)

        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=self._disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run", side_effect=ejecutar,
            ) as proceso,
            mock.patch("parlar.inyector_salida.time.sleep"),
        ):
            resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
        self.assertEqual(proceso.call_count, 2)
        self.assertEqual(inyector._registro_oraciones, [])

    def test_excepcion_y_notify_no_filtran_ni_cambian_copy(self):
        texto = "secreto ágil ñandú"
        inyector = Inyector(backend="xdotool", notify=True)

        def disponible(nombre):
            if nombre in ("xclip", "notify-send"):
                return f"/usr/bin/{nombre}"
            return None

        def ejecutar(argv, **_kwargs):
            if argv[0] == "xclip":
                return subprocess.CompletedProcess(argv, 0)
            if argv[0] == "xdotool":
                raise subprocess.TimeoutExpired(argv, 30)
            raise OSError("notify indisponible")

        salida = io.StringIO()
        with (
            mock.patch(
                "parlar.inyector_salida._cual", side_effect=disponible),
            mock.patch(
                "parlar.inyector_salida.subprocess.run", side_effect=ejecutar),
            mock.patch("parlar.inyector_salida.time.sleep"),
            contextlib.redirect_stderr(salida),
        ):
            resultado = inyector.escribir_texto(texto)
        self.assertEqual(resultado.estado, EstadoEntrega.COPIED)
        self.assertNotIn(texto, salida.getvalue())
        self.assertIn("TimeoutExpired", salida.getvalue())
        self.assertIn("OSError", salida.getvalue())


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

    def test_aliases_largos_respetan_el_gate_de_return(self):
        proc = ProcesadorTexto()
        comandos = (
            "mandar mensaje",
            "enviar mensaje",
            "salto de línea",
            "salto de párrafo",
            "¡Nuevo párrafo!",
            "¿Nueva línea?",
        )
        app = app_minima(permitir_enter=False)
        for texto in comandos:
            app._emitir(proc.procesar_frase(texto), 1)
        self.assertEqual(app.inyector.newlines, [])
        self.assertEqual(app.inyector.enters, 0)

        app = app_minima(permitir_enter=True)
        for texto in comandos:
            app._emitir(proc.procesar_frase(texto), 1)
        self.assertEqual(app.inyector.newlines, [1, 2, 2, 1])
        self.assertEqual(app.inyector.enters, 2)

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
