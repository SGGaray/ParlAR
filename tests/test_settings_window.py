"""Lógica de Settings testeable sin crear una ventana ni requerir display."""

import dataclasses
import subprocess
import sys
import threading
import unittest
from unittest import mock

from parlar.audio_test import EstadoPruebaAudio
from parlar.config import Config, ErrorConfiguracion
from parlar.settings_backend import (
    DispositivoEntrada,
    ErrorDispositivosAudio,
    ResultadoPersistencia,
    crear_identidad_entrada,
    resolver_dispositivo_entrada,
    snapshot_configuracion,
)
from parlar.settings_window import (
    ControlPruebaMicrofono,
    ControlSettings,
    OpcionEntrada,
    ValoresFormulario,
    VentanaSettings,
    _indice_opcion_audio,
    cargar_inventario_entradas,
    construir_opciones_entrada,
    guardado_habilitado,
    mensaje_resolucion_entrada,
    mensaje_persistencia,
    parsear_context_terms,
    snapshot_desde_valores,
    valores_desde_snapshot,
)


class PruebaAudioFalsa:
    def __init__(self, *, error_inicio=None, error_detencion=None):
        self.error_inicio = error_inicio
        self.error_detencion = error_detencion
        self.inicios = []
        self.detenciones = 0
        self.estado_actual = EstadoPruebaAudio(0.0, False, None)

    def iniciar(self, indice):
        self.inicios.append(indice)
        if self.error_inicio:
            raise RuntimeError(self.error_inicio)
        self.estado_actual = EstadoPruebaAudio(0.0, True, None)
        return True

    def detener(self):
        self.detenciones += 1
        self.estado_actual = EstadoPruebaAudio(0.0, False, None)
        if self.error_detencion:
            raise RuntimeError(self.error_detencion)
        return True

    def estado(self):
        return self.estado_actual


class PruebaAudioBloqueante(PruebaAudioFalsa):
    def __init__(self):
        super().__init__()
        self.inicio_entrado = threading.Event()
        self.liberar_inicio = threading.Event()

    def iniciar(self, indice):
        self.inicios.append(indice)
        self.inicio_entrado.set()
        self.liberar_inicio.wait(2.0)
        self.estado_actual = EstadoPruebaAudio(0.0, True, None)
        return True


class PruebasLogicaSettings(unittest.TestCase):
    def setUp(self):
        self.identidad_audio = crear_identidad_entrada("Mic USB", "ALSA")
        self.base = Config(
            model_size="base",
            device="cpu",
            compute_type="int8",
            language="es",
            context_terms=["COBIT", "Acme Corporation"],
            mode="streaming",
            rewrite_mode="formal",
            injector="clipboard",
            hotkey_toggle="<ctrl>+<alt>+d",
            overlay=False,
            guionar=True,
            guionar_socket="/tmp/guionar.sock",
            guardar_sesion=True,
            audio_input_device=self.identidad_audio,
        )
        self.snapshot = snapshot_configuracion(self.base)

    def test_snapshot_se_mapea_a_valores_de_formulario(self):
        valores = valores_desde_snapshot(self.snapshot)

        self.assertIsInstance(valores, ValoresFormulario)
        self.assertEqual(valores.model_size, "base")
        self.assertEqual(valores.context_terms, "COBIT\nAcme Corporation")
        self.assertFalse(valores.overlay)
        self.assertTrue(valores.guionar)
        self.assertEqual(valores.audio_input_device, self.identidad_audio)

    def test_formulario_se_mapea_a_snapshot_sin_mutar_inicial(self):
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            language="en",
            context_terms="OWASP\nParlAR",
            overlay=True,
            audio_input_device="default",
        )

        candidata = snapshot_desde_valores(self.snapshot, valores)

        self.assertEqual(candidata.language, "en")
        self.assertEqual(candidata.context_terms, ("OWASP", "ParlAR"))
        self.assertTrue(candidata.overlay)
        self.assertEqual(candidata.audio_input_device, "default")
        self.assertEqual(self.snapshot.language, "es")
        self.assertEqual(
            self.snapshot.audio_input_device, self.identidad_audio)

    def test_context_terms_usa_un_termino_por_linea(self):
        self.assertEqual(
            parsear_context_terms("  COBIT  \n\nAcme   Corporation\r\n OWASP "),
            ("COBIT", "Acme   Corporation", "OWASP"),
        )
        self.assertEqual(parsear_context_terms("\n  \n"), ())

    def test_detecta_cambio_y_sin_cambio(self):
        control = ControlSettings(self.base, self.snapshot)
        iguales = valores_desde_snapshot(self.snapshot)
        cambiados = dataclasses.replace(iguales, mode="utterance")

        self.assertFalse(control.esta_sucio(iguales))
        self.assertTrue(control.esta_sucio(cambiados))

    def test_validacion_invalida_no_persiste(self):
        persistir = mock.Mock()
        control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=persistir,
        )
        invalidos = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            device="tpu",
        )

        with self.assertRaisesRegex(ErrorConfiguracion, "device"):
            control.guardar(invalidos)

        persistir.assert_not_called()

    def test_persistencia_valida_usa_backend_y_actualiza_inicial(self):
        persistir = mock.Mock()
        control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=persistir,
        )
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            mode="utterance",
        )
        candidata = snapshot_desde_valores(self.snapshot, valores)
        persistir.return_value = ResultadoPersistencia(candidata, True)

        resultado = control.guardar(valores)

        persistir.assert_called_once_with(self.base, candidata)
        self.assertIs(resultado, persistir.return_value)
        self.assertFalse(control.esta_sucio(valores))

    def test_resultado_refleja_requires_restart(self):
        con_reinicio = ResultadoPersistencia(self.snapshot, True)
        sin_reinicio = ResultadoPersistencia(self.snapshot, False)

        self.assertEqual(
            mensaje_persistencia(con_reinicio),
            "Configuración guardada. Los cambios se aplicarán al reiniciar ParlAR.",
        )
        self.assertEqual(
            mensaje_persistencia(sin_reinicio),
            "Configuración guardada. No es necesario reiniciar ParlAR.",
        )

    def test_cancelar_cierra_sin_persistir(self):
        persistir = mock.Mock()
        cerrar = mock.Mock()
        control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=persistir,
        )

        control.cancelar(cerrar)

        cerrar.assert_called_once_with()
        persistir.assert_not_called()


class PruebasSelectorYPruebaAudio(unittest.TestCase):
    def setUp(self):
        self.identidad = crear_identidad_entrada("Mic USB", "ALSA")
        self.otro = crear_identidad_entrada("Mic Interno", "PulseAudio")
        self.mic = DispositivoEntrada(
            indice=7,
            nombre="Mic USB",
            canales_entrada=2,
            predeterminado=True,
            host_api="ALSA",
            identidad=self.identidad,
        )
        self.mic_otro = DispositivoEntrada(
            indice=11,
            nombre="Mic Interno",
            canales_entrada=1,
            predeterminado=False,
            host_api="PulseAudio",
            identidad=self.otro,
        )

    def crear_control(self, prueba=None, inventario=None):
        control = ControlPruebaMicrofono(
            tuple(inventario if inventario is not None else (self.mic,)),
            prueba=prueba or PruebaAudioFalsa(),
        )
        self.addCleanup(self._cerrar_control, control)
        return control

    def _cerrar_control(self, control):
        control.cerrar()
        self.assertTrue(control.esperar_cierre())

    def test_opcion_default_existe_aun_sin_inventario(self):
        opciones = construir_opciones_entrada("default", ())

        self.assertEqual(len(opciones), 1)
        self.assertEqual(opciones[0].valor, "default")
        self.assertTrue(opciones[0].disponible)

    def test_opcion_concreta_muestra_nombre_y_host_sin_indice(self):
        opciones = construir_opciones_entrada(self.identidad, (self.mic,))

        concreta = opciones[1]
        self.assertEqual(concreta.valor, self.identidad)
        self.assertEqual(concreta.etiqueta, "Mic USB — ALSA")
        self.assertNotIn("7", concreta.etiqueta)

    def test_labels_iguales_se_resuelven_por_indice_y_no_por_texto(self):
        opciones = (
            OpcionEntrada("valor-a", "Misma etiqueta", True),
            OpcionEntrada("valor-b", "Misma etiqueta", True),
        )
        ventana = object.__new__(VentanaSettings)
        ventana.opciones_audio = opciones
        ventana.selector_audio = mock.Mock()
        ventana.control = mock.Mock()
        ventana.control.snapshot_inicial.audio_input_device = "persistido"

        self.assertEqual(_indice_opcion_audio(opciones, "valor-b"), 1)
        ventana.selector_audio.current.return_value = 0
        self.assertEqual(ventana._audio_actual(), "valor-a")
        ventana.selector_audio.current.return_value = 1
        self.assertEqual(ventana._audio_actual(), "valor-b")
        ventana.selector_audio.current.return_value = -1
        self.assertEqual(ventana._audio_actual(), "persistido")

    def test_preferencia_ausente_permanece_visible_sin_cambiar_valor(self):
        opciones = construir_opciones_entrada(self.otro, (self.mic,))

        ausente = opciones[1]
        self.assertEqual(ausente.valor, self.otro)
        self.assertFalse(ausente.disponible)
        self.assertIn("no disponible", ausente.etiqueta)

    def test_identidad_ambigua_se_marca_y_no_se_publican_indices(self):
        duplicado = dataclasses.replace(self.mic, indice=19)

        opciones = construir_opciones_entrada(
            self.identidad, (self.mic, duplicado))

        self.assertEqual(len(opciones), 2)
        self.assertFalse(opciones[1].disponible)
        self.assertIn("ambigua: 2", opciones[1].etiqueta)
        self.assertNotIn("19", opciones[1].etiqueta)

    def test_inventario_exitoso_se_inmoviliza(self):
        resultado = cargar_inventario_entradas(
            lambda: [self.mic, self.mic_otro])

        self.assertEqual(resultado.dispositivos, (self.mic, self.mic_otro))
        self.assertIsNone(resultado.error)

    def test_error_de_enumeracion_no_impide_abrir_settings(self):
        def fallar():
            raise ErrorDispositivosAudio("PortAudio no disponible")

        resultado = cargar_inventario_entradas(fallar)

        self.assertEqual(resultado.dispositivos, ())
        self.assertEqual(resultado.error, "PortAudio no disponible")

    def test_mensaje_hace_explicito_fallback_por_ausencia(self):
        resolucion = resolver_dispositivo_entrada(self.otro, (self.mic,))

        mensaje = mensaje_resolucion_entrada(resolucion)

        self.assertIn("guardado no está disponible", mensaje)
        self.assertIn("temporalmente Mic USB — ALSA", mensaje)
        self.assertEqual(resolucion.seleccion_solicitada, self.otro)

    def test_mensaje_de_inventario_vacio_es_claro(self):
        resolucion = resolver_dispositivo_entrada("default", ())

        self.assertEqual(
            mensaje_resolucion_entrada(resolucion),
            "No se detectaron dispositivos de entrada.",
        )

    def test_mensaje_de_ambiguedad_es_explicito(self):
        duplicado = dataclasses.replace(
            self.mic, indice=19, predeterminado=False)
        resolucion = resolver_dispositivo_entrada(
            self.identidad, (self.mic, duplicado, self.mic_otro))

        mensaje = mensaje_resolucion_entrada(resolucion)

        self.assertIn("coincide con varios", mensaje)
        self.assertIn("temporalmente Mic USB — ALSA", mensaje)

    def test_inicio_usa_indice_resuelto_pero_conserva_seleccion(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba)

        self.assertTrue(control.iniciar(self.identidad))
        self.assertTrue(control.esperar_fase("active"))

        self.assertEqual(prueba.inicios, [7])
        self.assertEqual(control.estado().seleccion, self.identidad)

    def test_doble_inicio_rapido_no_abre_dos_streams(self):
        prueba = PruebaAudioBloqueante()
        control = self.crear_control(prueba)

        self.assertTrue(control.iniciar(self.identidad))
        self.assertTrue(prueba.inicio_entrado.wait(1.0))
        self.assertFalse(control.iniciar(self.identidad))
        prueba.liberar_inicio.set()
        self.assertTrue(control.esperar_fase("active"))

        self.assertEqual(prueba.inicios, [7])

    def test_cierre_durante_inicio_espera_y_detiene(self):
        prueba = PruebaAudioBloqueante()
        control = self.crear_control(prueba)
        control.iniciar(self.identidad)
        self.assertTrue(prueba.inicio_entrado.wait(1.0))

        self.assertTrue(control.cerrar())
        prueba.liberar_inicio.set()
        self.assertTrue(control.esperar_cierre())

        self.assertEqual(control.estado().fase, "closed")
        self.assertEqual(prueba.detenciones, 1)

    def test_detener_es_asincrono_y_vuelve_a_idle(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba)
        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("active"))

        self.assertTrue(control.detener())
        self.assertTrue(control.esperar_fase("idle"))

        self.assertEqual(prueba.detenciones, 1)
        self.assertEqual(control.estado().nivel, 0.0)

    def test_estado_publica_nivel_sin_exponer_audio(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba)
        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("active"))
        prueba.estado_actual = EstadoPruebaAudio(0.42, True, None)

        estado = control.estado()

        self.assertEqual(estado.nivel, 0.42)
        self.assertEqual(estado.fase, "active")

    def test_error_al_abrir_vuelve_a_idle_y_se_publica(self):
        prueba = PruebaAudioFalsa(error_inicio="ocupado")
        control = self.crear_control(prueba)

        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("idle"))

        self.assertIn("No se pudo iniciar", control.estado().error)
        self.assertIn("ocupado", control.estado().error)

    def test_error_del_callback_cierra_estado_activo(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba)
        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("active"))
        prueba.estado_actual = EstadoPruebaAudio(
            0.0, False, "desborde de entrada")

        estado = control.estado()

        self.assertEqual(estado.fase, "idle")
        self.assertEqual(estado.error, "desborde de entrada")

    def test_resolucion_invalida_no_intenta_abrir_stream(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba, inventario=())

        self.assertFalse(control.iniciar("default"))

        self.assertEqual(prueba.inicios, [])
        self.assertIn("inventario_vacio", control.estado().error)

    def test_cierre_activo_detiene_y_finaliza_worker(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba)
        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("active"))

        self.assertTrue(control.cerrar())
        self.assertTrue(control.esperar_cierre())

        self.assertEqual(control.estado().fase, "closed")
        self.assertEqual(prueba.detenciones, 1)

    def test_no_permite_hot_swap_mientras_prueba_esta_activa(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(
            prueba, inventario=(self.mic, self.mic_otro))
        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("active"))

        self.assertFalse(control.iniciar(self.otro))

        self.assertEqual(prueba.inicios, [7])
        self.assertEqual(control.estado().seleccion, self.identidad)

    def test_error_al_detener_no_impide_cerrar_estado(self):
        prueba = PruebaAudioFalsa(error_detencion="falló close")
        control = self.crear_control(prueba)
        control.iniciar(self.identidad)
        self.assertTrue(control.esperar_fase("active"))

        control.detener()
        self.assertTrue(control.esperar_fase("idle"))

        self.assertIn("No se pudo detener", control.estado().error)

    def test_guardar_persiste_identidad_y_nunca_indice(self):
        base = Config(audio_input_device="default")
        inicial = snapshot_configuracion(base)
        persistir = mock.Mock()
        candidata = dataclasses.replace(
            inicial, audio_input_device=self.identidad)
        persistir.return_value = ResultadoPersistencia(candidata, True)
        control = ControlSettings(base, inicial, persistir=persistir)
        valores = dataclasses.replace(
            valores_desde_snapshot(inicial),
            audio_input_device=self.identidad,
        )

        control.guardar(valores)

        guardada = persistir.call_args.args[1]
        self.assertEqual(guardada.audio_input_device, self.identidad)
        self.assertNotEqual(guardada.audio_input_device, "7")

    def test_guardar_se_permite_activo_pero_no_durante_transiciones(self):
        self.assertTrue(guardado_habilitado(
            sucio=True, fase_audio="active"))
        self.assertFalse(guardado_habilitado(
            sucio=True, fase_audio="starting"))
        self.assertFalse(guardado_habilitado(
            sucio=True, fase_audio="stopping"))
        self.assertFalse(guardado_habilitado(
            sucio=True, fase_audio="active", cerrando=True))
        self.assertFalse(guardado_habilitado(
            sucio=False, fase_audio="idle"))

    def test_importar_modulo_no_carga_tk_hardware_app_ni_whisper(self):
        codigo = """
import sys
import parlar.settings_window
prohibidos = {'tkinter', 'sounddevice', 'parlar.app', 'faster_whisper'}
print(','.join(sorted(prohibidos.intersection(sys.modules))))
"""

        proceso = subprocess.run(
            [sys.executable, "-c", codigo],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proceso.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
