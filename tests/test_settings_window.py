"""Lógica de Settings testeable sin crear una ventana ni requerir display."""

import dataclasses
import io
import subprocess
import sys
import threading
import unittest
from unittest import mock

import parlar.settings_window as settings_window_mod
from parlar.audio_test import EstadoPruebaAudio
from parlar.config import Config, ErrorConfiguracion
from parlar.settings_backend import (
    DispositivoEntrada,
    EstadoAutostart,
    ErrorDispositivosAudio,
    ResultadoPersistencia,
    SettingsCapabilities,
    crear_identidad_entrada,
    resolver_dispositivo_entrada,
    snapshot_configuracion,
)
from parlar.restart import (
    EstadoResultadoReinicio,
    ResultadoReinicio,
    serializar_resultado,
)
from parlar.settings_window import (
    ARQUITECTURA_SETTINGS,
    ControlPruebaMicrofono,
    ControlAutostart,
    ControlSettings,
    OpcionEntrada,
    OpcionSelector,
    ValoresFormulario,
    VentanaSettings,
    _indice_opcion_audio,
    accion_cierre_settings,
    cargar_inventario_entradas,
    construir_opciones_entrada,
    construir_selectores,
    guardado_habilitado,
    indice_opcion_selector,
    mensaje_autostart_inmediato,
    mensaje_error_prueba_audio,
    mensaje_reinicio_previo,
    mensaje_resolucion_entrada,
    mensaje_persistencia,
    mensaje_persistencia_contextual,
    parsear_context_terms,
    refrescar_entradas,
    snapshot_desde_valores,
    valor_opcion_selector,
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
            overlay_position="top-right",
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
        self.assertEqual(valores.overlay_position, "top-right")

    def test_formulario_se_mapea_a_snapshot_sin_mutar_inicial(self):
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            language="en",
            context_terms="OWASP\nParlAR",
            overlay=True,
            audio_input_device="default",
            overlay_position="bottom-left",
        )

        candidata = snapshot_desde_valores(self.snapshot, valores)

        self.assertEqual(candidata.language, "en")
        self.assertEqual(candidata.context_terms, ("OWASP", "ParlAR"))
        self.assertTrue(candidata.overlay)
        self.assertEqual(candidata.audio_input_device, "default")
        self.assertEqual(candidata.overlay_position, "bottom-left")
        self.assertEqual(self.snapshot.language, "es")
        self.assertEqual(
            self.snapshot.audio_input_device, self.identidad_audio)
        self.assertEqual(self.snapshot.overlay_position, "top-right")

    def test_context_terms_usa_un_termino_por_linea(self):
        self.assertEqual(
            parsear_context_terms("  COBIT  \n\nAcme   Corporation\r\n OWASP "),
            ("COBIT", "Acme   Corporation", "OWASP"),
        )
        self.assertEqual(parsear_context_terms("\n  \n"), ())

    def test_contexto_multilinea_hace_roundtrip(self):
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            context_terms="María Elena\nINTA\nAPI de Pagos",
        )

        candidata = snapshot_desde_valores(self.snapshot, valores)
        reconstruidos = valores_desde_snapshot(candidata)

        self.assertEqual(
            reconstruidos.context_terms,
            "María Elena\nINTA\nAPI de Pagos",
        )

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

    def test_mensaje_reinicio_contextual_depende_del_runtime(self):
        resultado = ResultadoPersistencia(self.snapshot, True)
        self.assertEqual(
            mensaje_persistencia_contextual(
                resultado, runtime_activo=True),
            "Cambios guardados. Reiniciá ParlAR para aplicarlos.",
        )
        self.assertEqual(
            mensaje_persistencia_contextual(
                resultado, runtime_activo=False),
            "Cambios guardados. Se aplicarán al iniciar ParlAR.",
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


class PruebasSelectoresHumanos(unittest.TestCase):
    def setUp(self):
        self.capacidades = SettingsCapabilities(
            devices=tuple(sorted(Config.DISPOSITIVOS)),
            compute_types=tuple(sorted(Config.COMPUTE_TYPES)),
            modes=tuple(sorted(Config.MODOS)),
            rewrite_modes=tuple(sorted(Config.REESCRITURAS)),
            injectors=tuple(sorted(Config.INYECTORES)),
            session_type="x11",
            overlay_positions=tuple(sorted(Config.OVERLAY_POSITIONS)),
        )
        self.selectores = construir_selectores(self.capacidades)

    def test_cada_selector_hace_roundtrip_de_todos_sus_valores(self):
        for nombre, opciones in self.selectores.items():
            with self.subTest(selector=nombre):
                for indice, opcion in enumerate(opciones):
                    self.assertEqual(
                        valor_opcion_selector(
                            opciones, indice, "valor-original"),
                        opcion.valor,
                    )

    def test_el_label_humano_no_se_convierte_en_valor_persistido(self):
        opciones = self.selectores["device"]
        indice = indice_opcion_selector(opciones, "cuda")

        self.assertEqual(opciones[indice].etiqueta, "NVIDIA CUDA")
        self.assertEqual(
            valor_opcion_selector(opciones, indice, "cpu"), "cuda")
        self.assertNotEqual(opciones[indice].etiqueta, "cuda")

    def test_seleccion_inicial_se_busca_por_valor_interno(self):
        opciones = self.selectores["mode"]

        indice = indice_opcion_selector(opciones, "streaming")

        self.assertGreaterEqual(indice, 0)
        self.assertEqual(opciones[indice].etiqueta, "Incremental")

    def test_indice_invalido_conserva_valor_original(self):
        opciones = self.selectores["rewrite_mode"]

        self.assertEqual(
            valor_opcion_selector(opciones, -1, "formal"), "formal")
        self.assertEqual(
            valor_opcion_selector(opciones, len(opciones), "formal"),
            "formal",
        )

    def test_labels_duplicados_siguen_resolviendose_por_indice(self):
        opciones = (
            OpcionSelector("interno-a", "Mismo label"),
            OpcionSelector("interno-b", "Mismo label"),
        )

        self.assertEqual(
            valor_opcion_selector(opciones, 0, "original"), "interno-a")
        self.assertEqual(
            valor_opcion_selector(opciones, 1, "original"), "interno-b")

    def test_posicion_overlay_usa_label_humano_pero_persiste_valor(self):
        opciones = self.selectores["overlay_position"]
        indice = indice_opcion_selector(opciones, "bottom-center")

        self.assertEqual(opciones[indice].etiqueta, "Abajo · Centro")
        self.assertEqual(
            valor_opcion_selector(opciones, indice, "top-left"),
            "bottom-center",
        )
        self.assertNotEqual(opciones[indice].etiqueta, "bottom-center")

    def test_posicion_overlay_con_labels_duplicados_conserva_indice(self):
        opciones = (
            OpcionSelector("top-left", "Misma posición"),
            OpcionSelector("bottom-right", "Misma posición"),
        )

        self.assertEqual(
            valor_opcion_selector(opciones, 0, "bottom-center"),
            "top-left",
        )
        self.assertEqual(
            valor_opcion_selector(opciones, 1, "bottom-center"),
            "bottom-right",
        )

    def test_device_y_audio_input_device_permanecen_independientes(self):
        inicial = snapshot_configuracion(Config())
        valores = dataclasses.replace(
            valores_desde_snapshot(inicial),
            device="cuda",
            audio_input_device="audio-input:ALSA:Mic%20USB",
        )

        candidata = snapshot_desde_valores(inicial, valores)

        self.assertEqual(candidata.device, "cuda")
        self.assertEqual(
            candidata.audio_input_device, "audio-input:ALSA:Mic%20USB")

    def test_compute_type_conserva_valor_real(self):
        opciones = self.selectores["compute_type"]
        indice = indice_opcion_selector(opciones, "int8_float16")

        self.assertEqual(
            valor_opcion_selector(opciones, indice, "auto"), "int8_float16")

    def test_mode_conserva_valor_real(self):
        opciones = self.selectores["mode"]
        indice = indice_opcion_selector(opciones, "utterance")

        self.assertEqual(
            valor_opcion_selector(opciones, indice, "streaming"), "utterance")

    def test_rewrite_mode_conserva_valor_real(self):
        opciones = self.selectores["rewrite_mode"]
        indice = indice_opcion_selector(opciones, "concise")

        self.assertEqual(
            valor_opcion_selector(opciones, indice, "none"), "concise")

    def test_language_conserva_codigo_editable(self):
        inicial = snapshot_configuracion(Config(language="es"))
        valores = dataclasses.replace(
            valores_desde_snapshot(inicial), language="en")

        candidata = snapshot_desde_valores(inicial, valores)

        self.assertEqual(candidata.language, "en")

    def test_injector_conserva_valor_real(self):
        opciones = self.selectores["injector"]
        indice = indice_opcion_selector(opciones, "clipboard")

        self.assertEqual(
            valor_opcion_selector(opciones, indice, "auto"), "clipboard")

    def test_snapshot_formulario_roundtrip_sigue_estable(self):
        inicial = snapshot_configuracion(Config(
            device="cpu",
            compute_type="int8",
            mode="streaming",
            rewrite_mode="email",
            injector="wtype",
            overlay_position="middle-right",
        ))

        reconstruido = snapshot_desde_valores(
            inicial, valores_desde_snapshot(inicial))

        self.assertEqual(reconstruido, inicial)

    def test_mensaje_de_reinicio_es_modelable_sin_tk(self):
        self.assertEqual(
            mensaje_reinicio_previo(),
            "Los cambios se aplican al reiniciar ParlAR.",
        )

    def test_model_size_sigue_siendo_texto_libre(self):
        inicial = snapshot_configuracion(Config(model_size="small"))
        valores = dataclasses.replace(
            valores_desde_snapshot(inicial), model_size="modelo-local")

        candidata = snapshot_desde_valores(inicial, valores)

        self.assertEqual(candidata.model_size, "modelo-local")

    def test_opciones_salen_solo_de_capacidades_reales(self):
        for nombre, valores in (
            ("device", self.capacidades.devices),
            ("compute_type", self.capacidades.compute_types),
            ("mode", self.capacidades.modes),
            ("rewrite_mode", self.capacidades.rewrite_modes),
            ("injector", self.capacidades.injectors),
            ("overlay_position", self.capacidades.overlay_positions),
        ):
            self.assertEqual(
                tuple(opcion.valor for opcion in self.selectores[nombre]),
                valores,
            )

    def test_ventana_traduce_el_indice_y_no_el_texto_visible(self):
        ventana = object.__new__(VentanaSettings)
        opciones = self.selectores["device"]
        selector = mock.Mock()
        selector.current.return_value = indice_opcion_selector(opciones, "cpu")
        ventana.selectores = {"device": (selector, opciones)}
        ventana.control = mock.Mock()
        ventana.control.snapshot_inicial.device = "auto"

        self.assertEqual(ventana._valor_selector_actual("device"), "cpu")


class PruebasArquitecturaSettings(unittest.TestCase):
    def test_tres_pestanas_tienen_orden_final(self):
        self.assertEqual(
            tuple(pestana.nombre for pestana in ARQUITECTURA_SETTINGS.pestañas),
            ("Dictado", "Aplicación", "Avanzado"),
        )

    def test_dictado_concentra_recorrido_principal(self):
        dictado = ARQUITECTURA_SETTINGS.pestañas[0]

        self.assertEqual(
            dictado.campos,
            (
                "hotkey_toggle", "audio_input_device", "audio_refresh",
                "audio_test", "language", "context_terms",
            ),
        )
        self.assertEqual(dictado.orden_foco, dictado.campos)

    def test_aplicacion_contiene_estado_indicador_y_posicion(self):
        aplicacion = ARQUITECTURA_SETTINGS.pestañas[1]

        self.assertEqual(
            aplicacion.campos,
            ("runtime_status", "overlay", "overlay_position", "autostart"))
        self.assertEqual(
            aplicacion.orden_foco,
            ("overlay", "overlay_position", "autostart"))

    def test_autostart_aplica_inmediato_sin_marcar_config_dirty(self):
        base = Config()
        snapshot = snapshot_configuracion(base)
        control_settings = ControlSettings(base, snapshot)
        estado_inicial = EstadoAutostart(
            "disabled", "Desactivado.", True)
        estado_final = EstadoAutostart(
            "enabled", "Se iniciará.", True)
        establecer = mock.Mock(return_value=mock.Mock(
            resultado="changed", estado=estado_final,
            mensaje=estado_final.mensaje))
        control = ControlAutostart(
            consultar=lambda: estado_inicial,
            establecer=establecer,
        )

        resultado = control.cambiar(True)

        self.assertEqual(resultado.estado, estado_final)
        establecer.assert_called_once_with(True)
        self.assertFalse(control_settings.esta_sucio(
            valores_desde_snapshot(snapshot)))

    def test_autostart_fallido_conserva_estado_real(self):
        real = EstadoAutostart("enabled", "Sigue activo.", True)
        fallido = mock.Mock(
            resultado="failed", estado=real, mensaje="No se pudo cambiar.")
        control = ControlAutostart(
            consultar=lambda: real,
            establecer=lambda _activar: fallido,
        )

        resultado = control.cambiar(False)

        self.assertEqual(resultado.resultado, "failed")
        self.assertTrue(control.estado.activado)

    def test_checkbox_revierte_visual_si_operacion_falla(self):
        real = EstadoAutostart("enabled", "Sigue activo.", True)
        resultado = mock.Mock(
            resultado="failed", estado=real,
            mensaje="No se pudo cambiar el inicio automático.")
        ventana = VentanaSettings.__new__(VentanaSettings)
        ventana.control_autostart = mock.Mock()
        ventana.control_autostart.cambiar.return_value = resultado
        ventana.autostart_activado = mock.Mock()
        ventana.autostart_activado.get.return_value = False
        ventana.estado_autostart = mock.Mock()
        ventana.check_autostart = mock.Mock()
        ventana.etiqueta_autostart = mock.Mock()
        ventana._cerrando = False

        ventana._alternar_autostart()

        ventana.control_autostart.cambiar.assert_called_once_with(False)
        ventana.autostart_activado.set.assert_called_once_with(True)
        ventana.estado_autostart.set.assert_called_once_with(
            resultado.mensaje)

    def test_autostart_unavailable_no_bloquea_settings(self):
        ausente = EstadoAutostart(
            "unavailable",
            "El inicio automático no está disponible en esta instalación.",
            False,
        )
        control = ControlAutostart(consultar=lambda: ausente)
        self.assertEqual(control.estado.estado, "unavailable")
        self.assertFalse(control.estado.modificable)

    def test_avanzado_contiene_opciones_tecnicas(self):
        avanzado = ARQUITECTURA_SETTINGS.pestañas[2]

        self.assertTrue({
            "model_size", "device", "compute_type", "mode",
            "rewrite_mode", "injector", "guardar_sesion",
            "guionar_status", "guionar",
        }.issubset(avanzado.campos))
        self.assertEqual(avanzado.orden_foco, avanzado.campos)
        # La ruta del socket es CLI/config: no es una opción de uso normal.
        self.assertFalse(any(
            "guionar_socket" in pestaña.campos
            for pestaña in ARQUITECTURA_SETTINGS.pestañas))

    def test_footer_es_fijo_y_no_pertenece_al_scroll(self):
        self.assertTrue(ARQUITECTURA_SETTINGS.footer_fijo)
        self.assertFalse(ARQUITECTURA_SETTINGS.footer_dentro_scroll)

    def _ventana_reinicio(self, *, ejecutandose=True):
        ventana = VentanaSettings.__new__(VentanaSettings)
        ventana.boton_reiniciar = mock.Mock()
        ventana._proceso_reinicio = None
        ventana._reinicio_requerido = True
        ventana._gestor_reintento = None
        ventana._cerrando = False
        ventana.estado_parlar = mock.Mock(ejecutandose=ejecutandose)
        ventana.estado = mock.Mock()
        ventana.etiqueta_estado = mock.Mock()
        return ventana

    def test_settings_sin_runtime_no_ofrece_restart_normal(self):
        ventana = self._ventana_reinicio(ejecutandose=False)

        ventana._actualizar_boton_reinicio()

        ventana.boton_reiniciar.grid_remove.assert_called_once_with()

    def test_requires_restart_con_runtime_muestra_boton(self):
        ventana = self._ventana_reinicio(ejecutandose=True)

        ventana._actualizar_boton_reinicio()

        ventana.boton_reiniciar.grid.assert_called_once_with()
        ventana.boton_reiniciar.configure.assert_called_once_with(
            text="Reiniciar ParlAR")

    def test_restart_exitoso_limpia_aviso(self):
        ventana = self._ventana_reinicio()
        proceso = mock.Mock()
        proceso.poll.return_value = 0
        proceso.stdout = io.StringIO(serializar_resultado(
            ResultadoReinicio(EstadoResultadoReinicio.LISTO, "Listo")))
        ventana._proceso_reinicio = proceso

        ventana._poll_reinicio()

        self.assertFalse(ventana._reinicio_requerido)
        ventana.estado.set.assert_called_with("Listo")

    def test_restart_fallido_deja_reintento_usable_sin_runtime(self):
        ventana = self._ventana_reinicio(ejecutandose=False)
        proceso = mock.Mock()
        proceso.poll.return_value = 1
        proceso.stdout = io.StringIO(serializar_resultado(ResultadoReinicio(
            EstadoResultadoReinicio.FALLIDO,
            "Revisá la configuración.",
            "manual",
        )))
        ventana._proceso_reinicio = proceso

        ventana._poll_reinicio()

        self.assertTrue(ventana._reinicio_requerido)
        self.assertEqual(ventana._gestor_reintento, "manual")
        ventana.boton_reiniciar.grid.assert_called()
        self.assertIn(
            "No se pudo reiniciar ParlAR.",
            ventana.estado.set.call_args.args[0],
        )

    def test_settings_invoca_el_mismo_coordinador_cli(self):
        ventana = self._ventana_reinicio()
        proceso = mock.Mock()
        proceso.poll.return_value = None
        ventana._popen = mock.Mock(return_value=proceso)
        ventana._programar_poll_reinicio = mock.Mock()
        ventana._actualizar_boton_reinicio = mock.Mock()
        with mock.patch.object(
                settings_window_mod, "comando_reinicio",
                return_value=["coordinador"]) as construir:
            ventana._iniciar_reinicio()

        construir.assert_called_once_with(
            terminar_dictado=False,
            reintentar_gestor=None,
        )
        ventana._popen.assert_called_once()
        self.assertEqual(
            ventana._popen.call_args.args[0], ["coordinador"])

    def test_mode_usa_por_frases_incremental_sin_continuo(self):
        capacidades = SettingsCapabilities(
            devices=(), compute_types=(),
            modes=("utterance", "streaming"),
            rewrite_modes=(), injectors=(), session_type="x11",
            overlay_positions=(),
        )
        opciones = construir_selectores(capacidades)["mode"]

        self.assertEqual(
            tuple(opcion.etiqueta for opcion in opciones),
            ("Por frases", "Incremental"),
        )
        self.assertNotIn("Continuo", {
            opcion.etiqueta for opcion in opciones})
        self.assertEqual(
            valor_opcion_selector(opciones, 1, "utterance"), "streaming")

    def test_cierre_limpio_no_pide_confirmacion(self):
        self.assertEqual(
            accion_cierre_settings(sucio=False), "cerrar")

    def test_cierre_sucio_requiere_confirmacion(self):
        self.assertEqual(
            accion_cierre_settings(sucio=True), "confirmar")

    def test_seguir_editando_cancela_el_descarte(self):
        self.assertEqual(
            accion_cierre_settings(
                sucio=True, descartar_confirmado=False),
            "seguir_editando",
        )

    def test_aceptar_descarte_cierra(self):
        self.assertEqual(
            accion_cierre_settings(
                sucio=True, descartar_confirmado=True),
            "cerrar",
        )

    def test_guionar_desactivado_conserva_socket(self):
        inicial = snapshot_configuracion(Config(
            guionar=True,
            guionar_socket="/run/user/1000/guionar.sock",
        ))
        valores = dataclasses.replace(
            valores_desde_snapshot(inicial), guionar=False)

        candidata = snapshot_desde_valores(inicial, valores)

        self.assertFalse(candidata.guionar)
        self.assertEqual(
            candidata.guionar_socket, "/run/user/1000/guionar.sock")


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

        with self.assertLogs("parlar.settings_window", level="INFO"):
            resultado = cargar_inventario_entradas(fallar)

        self.assertEqual(resultado.dispositivos, ())
        self.assertEqual(resultado.error, "PortAudio no disponible")

    def test_error_de_inventario_se_presenta_sin_detalle_tecnico(self):
        resolucion = resolver_dispositivo_entrada("default", ())

        mensaje = mensaje_resolucion_entrada(
            resolucion,
            error_inventario="PortAudio -9985: Device unavailable",
        )

        self.assertIn("lista de micrófonos", mensaje)
        self.assertIn("Actualizar", mensaje)
        self.assertNotIn("PortAudio", mensaje)
        self.assertNotIn("-9985", mensaje)

    def test_error_de_prueba_se_presenta_con_recuperacion_humana(self):
        mensaje = mensaje_error_prueba_audio(
            "PortAudio -9985: Device unavailable")

        self.assertIn("dispositivo esté disponible", mensaje)
        self.assertIn("no esté en uso", mensaje)
        self.assertNotIn("PortAudio", mensaje)
        self.assertNotIn("-9985", mensaje)

    def test_autostart_explica_que_no_depende_de_guardar(self):
        mensaje = mensaje_autostart_inmediato()

        self.assertIn("de inmediato", mensaje)
        self.assertIn("no depende de Guardar cambios", mensaje)

    def test_enter_invoca_la_accion_del_boton_enfocado(self):
        boton = mock.Mock()
        accion = mock.Mock()

        VentanaSettings._vincular_enter(boton, accion)
        evento = boton.bind.call_args.args[1]

        self.assertEqual(evento(object()), "break")
        accion.assert_called_once_with()
        boton.bind.assert_called_once_with("<Return>", evento)

    def test_refresh_conserva_seleccion_por_identidad(self):
        resultado = refrescar_entradas(
            self.identidad,
            lambda: (self.mic_otro, self.mic),
        )

        self.assertEqual(resultado.seleccion, self.identidad)
        self.assertEqual(
            resultado.opciones[resultado.indice_seleccionado].valor,
            self.identidad,
        )
        self.assertIsNone(resultado.error)

    def test_refresh_conserva_ausente_como_no_disponible(self):
        resultado = refrescar_entradas(
            self.identidad,
            lambda: (self.mic_otro,),
        )

        opcion = resultado.opciones[resultado.indice_seleccionado]
        self.assertEqual(opcion.valor, self.identidad)
        self.assertFalse(opcion.disponible)
        self.assertIn("no disponible", opcion.etiqueta)

    def test_refresh_no_modifica_otros_campos_del_formulario(self):
        valores = ValoresFormulario(
            model_size="medium",
            device="cpu",
            compute_type="int8",
            language="es",
            context_terms="ParlAR\nINTA",
            mode="streaming",
            rewrite_mode="formal",
            injector="clipboard",
            hotkey_toggle="<ctrl>+d",
            overlay=False,
            guionar=True,
            guionar_socket="/tmp/guionar.sock",
            guardar_sesion=True,
            audio_input_device=self.identidad,
            overlay_position="top-right",
        )

        refrescar_entradas(
            valores.audio_input_device, lambda: (self.mic,))

        self.assertEqual(valores.language, "es")
        self.assertEqual(valores.context_terms, "ParlAR\nINTA")
        self.assertEqual(valores.overlay_position, "top-right")

    def test_refresh_error_conserva_seleccion_y_deja_opciones_usables(self):
        def fallar():
            raise ErrorDispositivosAudio("PortAudio no disponible")

        resultado = refrescar_entradas(self.identidad, fallar)

        self.assertEqual(resultado.error, "PortAudio no disponible")
        self.assertGreaterEqual(resultado.indice_seleccionado, 0)
        self.assertEqual(
            resultado.opciones[resultado.indice_seleccionado].valor,
            self.identidad,
        )

    def test_control_actualiza_inventario_solo_en_reposo(self):
        prueba = PruebaAudioFalsa()
        control = self.crear_control(prueba)

        self.assertTrue(control.actualizar_inventario((self.mic_otro,)))
        self.assertTrue(control.iniciar(self.otro))
        self.assertTrue(control.esperar_fase("active"))
        self.assertFalse(control.actualizar_inventario((self.mic,)))

        self.assertEqual(prueba.inicios, [11])

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
