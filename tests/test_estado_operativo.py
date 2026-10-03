"""Contrato del estado operativo, IPC saneado y resumen para Settings."""

import json
import subprocess
import sys
import unittest
from unittest import mock

from parlar.app import App
from parlar.config import Config
from parlar.estado_operativo import (
    EstadoComponente,
    EstadoOperativoStore,
    EstadoRuntime,
    ErrorInicializacionSTT,
    ProblemaOperativo,
    Severidad,
    puede_dictar,
    serializar_estado,
    severidad_global,
)
from parlar.settings_backend import (
    DispositivoEntrada,
    ErrorInicializacionAudio,
    consultar_estado_parlar,
    representar_estado_parlar,
)


def _problema(codigo, componente, severidad, mensaje="Mensaje humano"):
    return ProblemaOperativo(
        codigo, componente, severidad, mensaje, "Detalle técnico")


def _store_listo(*, gesto_requerido=True):
    store = EstadoOperativoStore(gesto_requerido=gesto_requerido)
    for componente in ("stt", "audio", "hotkey", "control", "output"):
        store.fijar_componente(componente, EstadoComponente.READY)
    store.actualizar_runtime(EstadoRuntime.READY)
    return store


class PruebasEstadoOperativo(unittest.TestCase):
    def test_estado_inicial_es_starting_y_stt_no_cargado(self):
        estado = EstadoOperativoStore().snapshot()
        self.assertEqual(estado.runtime, EstadoRuntime.STARTING)
        self.assertEqual(estado.stt, EstadoComponente.NOT_LOADED)
        self.assertFalse(puede_dictar(estado))

    def test_transicion_ready_correcta(self):
        estado = _store_listo().snapshot()
        self.assertEqual(estado.runtime, EstadoRuntime.READY)
        self.assertTrue(puede_dictar(estado))

    def test_stopping_y_stopped_son_terminales(self):
        store = _store_listo()
        self.assertTrue(store.actualizar_runtime(EstadoRuntime.STOPPING))
        self.assertFalse(store.actualizar_runtime(EstadoRuntime.READY))
        self.assertTrue(store.actualizar_runtime(EstadoRuntime.STOPPED))
        self.assertFalse(puede_dictar(store.snapshot()))

    def test_modelo_loading_ready_y_error(self):
        store = EstadoOperativoStore()
        store.fijar_componente("stt", EstadoComponente.LOADING)
        self.assertEqual(store.snapshot().stt, EstadoComponente.LOADING)
        store.fijar_componente("stt", EstadoComponente.READY)
        self.assertEqual(store.snapshot().stt, EstadoComponente.READY)
        store.fijar_componente(
            "stt", EstadoComponente.ERROR,
            problema=_problema(
                "stt", "stt", Severidad.BLOCKING,
                "No se pudo cargar el modelo."))
        self.assertEqual(store.snapshot().stt, EstadoComponente.ERROR)
        self.assertEqual(
            store.snapshot().error_bloqueante.mensaje,
            "No se pudo cargar el modelo.")

    def test_audio_ready_fallback_y_fatal(self):
        store = EstadoOperativoStore()
        store.fijar_componente("audio", EstadoComponente.READY)
        self.assertEqual(store.snapshot().audio, EstadoComponente.READY)
        store.fijar_componente(
            "audio", EstadoComponente.FALLBACK,
            problema=_problema("fallback", "audio", Severidad.WARNING))
        self.assertEqual(severidad_global(store.snapshot()), Severidad.WARNING)
        self.assertIsNone(store.snapshot().error_bloqueante)
        store.fijar_componente(
            "audio", EstadoComponente.ERROR,
            problema=_problema("audio", "audio", Severidad.BLOCKING))
        self.assertEqual(severidad_global(store.snapshot()), Severidad.BLOCKING)

    def test_hotkey_fallido_bloquea_si_el_gesto_es_requerido(self):
        store = _store_listo()
        store.fijar_componente(
            "hotkey", EstadoComponente.ERROR,
            problema=_problema("hotkey", "hotkey", Severidad.BLOCKING))
        self.assertFalse(puede_dictar(store.snapshot()))

    def test_hotkey_no_es_requisito_en_entorno_con_control_externo(self):
        store = _store_listo(gesto_requerido=False)
        store.fijar_componente(
            "hotkey", EstadoComponente.UNAVAILABLE,
            problema=_problema("hotkey", "hotkey", Severidad.WARNING))
        self.assertTrue(puede_dictar(store.snapshot()))

    def test_inyector_degradado_sigue_siendo_usable(self):
        store = _store_listo()
        store.fijar_componente(
            "output", EstadoComponente.DEGRADED,
            problema=_problema("clipboard", "output", Severidad.WARNING))
        self.assertTrue(puede_dictar(store.snapshot()))

    def test_guionar_warning_no_bloquea_salida_principal(self):
        store = _store_listo()
        store.reportar_problema(_problema(
            "guionar", "guionar", Severidad.WARNING))
        self.assertTrue(puede_dictar(store.snapshot()))
        self.assertIsNone(store.snapshot().error_bloqueante)

    def test_cada_dependencia_requerida_participa_en_puede_dictar(self):
        for componente in ("stt", "audio", "hotkey", "control", "output"):
            with self.subTest(componente=componente):
                store = _store_listo()
                store.fijar_componente(
                    componente, EstadoComponente.ERROR,
                    problema=_problema(
                        componente, componente, Severidad.BLOCKING))
                self.assertFalse(puede_dictar(store.snapshot()))

    def test_mensaje_humano_y_detalle_tecnico_son_campos_separados(self):
        problema = _problema(
            "audio", "audio", Severidad.BLOCKING,
            "No se pudo abrir el micrófono seleccionado.")
        self.assertEqual(
            problema.mensaje,
            "No se pudo abrir el micrófono seleccionado.")
        self.assertEqual(problema.detalle, "Detalle técnico")

    def test_serializacion_ipc_es_json_y_no_filtra_contenido(self):
        payload = serializar_estado(_store_listo().snapshot())
        datos = json.loads(payload)
        self.assertTrue(datos["can_dictate"])
        self.assertEqual(datos["status"], "Listo")
        self.assertNotIn("detail", payload)
        for prohibido in ("context_terms", "transcript", "audio_buffer", "text"):
            self.assertNotIn(prohibido, payload)

    def test_app_expone_snapshot_por_comando_ipc_sin_cambiar_estado_legacy(self):
        app = App.__new__(App)
        app.estado_operativo = _store_listo()

        datos = json.loads(app._atender_comando("estado-operativo"))

        self.assertTrue(datos["can_dictate"])
        self.assertEqual(datos["runtime"], "ready")

    def test_evento_stale_no_pisa_estado_actual(self):
        store = EstadoOperativoStore()
        revision = store.snapshot().revision
        store.fijar_componente("stt", EstadoComponente.LOADING)
        self.assertFalse(store.fijar_componente(
            "stt", EstadoComponente.ERROR,
            revision_esperada=revision))
        self.assertEqual(store.snapshot().stt, EstadoComponente.LOADING)

    def test_worker_tardio_no_revive_shutdown(self):
        store = _store_listo()
        store.actualizar_runtime(EstadoRuntime.STOPPING)
        self.assertFalse(store.fijar_componente(
            "stt", EstadoComponente.READY))
        self.assertFalse(store.actualizar_runtime(EstadoRuntime.READY))

    def test_snapshot_es_inmutable(self):
        estado = EstadoOperativoStore().snapshot()
        with self.assertRaises(AttributeError):
            estado.runtime = EstadoRuntime.READY

    def test_import_no_carga_hardware_ni_ui(self):
        codigo = """
import sys
import parlar.estado_operativo
prohibidos = {'sounddevice', 'faster_whisper', 'ctranslate2', 'tkinter'}
assert prohibidos.isdisjoint(sys.modules), prohibidos & set(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)

    def test_app_publica_error_de_modelo_antes_de_propagar(self):
        store = EstadoOperativoStore()
        with (
            mock.patch(
                "parlar.app.MotorWhisper",
                side_effect=RuntimeError("CUDA driver incompatible")),
            self.assertRaises(ErrorInicializacionSTT),
        ):
            App(Config(device="cuda"), mic=object(),
                estado_operativo=store)

        estado = store.snapshot()
        self.assertEqual(estado.stt, EstadoComponente.ERROR)
        self.assertEqual(
            estado.error_bloqueante.mensaje,
            "No se pudo iniciar la aceleración CUDA solicitada.")
        self.assertIn("CUDA driver incompatible", estado.error_bloqueante.detalle)

    def test_app_publica_audio_sin_dispositivo_como_bloqueante(self):
        store = EstadoOperativoStore()
        with self.assertRaises(ErrorInicializacionAudio):
            App(Config(), listar_entradas=lambda: (),
                estado_operativo=store)
        estado = store.snapshot()
        self.assertEqual(estado.audio, EstadoComponente.ERROR)
        self.assertEqual(estado.error_bloqueante.codigo, "audio_no_disponible")

    def test_app_publica_fallback_de_audio_como_warning(self):
        store = EstadoOperativoStore()
        cfg = Config(audio_input_device="audio-input:ALSA:Ausente")
        default = DispositivoEntrada(
            4, "Mic interno", 1, True, "ALSA",
            "audio-input:ALSA:Mic%20interno")
        recurso = object()

        app = App(
            cfg, motor=recurso, frases=recurso, streaming=recurso,
            proc=recurso, inyector=recurso, guionar=recurso, sesion=recurso,
            ui=recurso, control=recurso, atajos=recurso,
            listar_entradas=lambda: (default,), estado_operativo=store)

        estado = app.estado_operativo.snapshot()
        self.assertEqual(estado.audio, EstadoComponente.FALLBACK)
        self.assertEqual(estado.warnings[0].codigo, "audio_fallback")


class PruebasEstadoEnSettings(unittest.TestCase):
    def test_runtime_no_ejecutandose_no_es_error(self):
        estado = representar_estado_parlar(None)
        self.assertEqual(estado.categoria, "stopped")
        self.assertFalse(estado.ejecutandose)
        self.assertIn("no está ejecutándose", estado.mensaje)

    def test_runtime_ready(self):
        estado = representar_estado_parlar({
            "status": "Listo", "message": "ParlAR está listo para dictar."})
        self.assertEqual(estado.categoria, "ready")
        self.assertTrue(estado.ejecutandose)

    def test_runtime_pausado_se_representa_sin_marcarlo_caido(self):
        estado = representar_estado_parlar({
            "status": "Pausado",
            "message": "ParlAR no aceptará nuevos dictados hasta reanudarlo.",
        })
        self.assertEqual(estado.categoria, "paused")
        self.assertEqual(estado.titulo, "Pausado")
        self.assertTrue(estado.ejecutandose)

    def test_runtime_atencion_y_error(self):
        atencion = representar_estado_parlar({
            "status": "Requiere atención", "message": "Usá parlarctl."})
        error = representar_estado_parlar({
            "status": "No disponible", "message": "No hay micrófono."})
        self.assertEqual(atencion.categoria, "attention")
        self.assertEqual(error.categoria, "unavailable")

    def test_consulta_usa_comando_nuevo_y_parsea_json(self):
        comandos = []

        def enviar(comando):
            comandos.append(comando)
            return '{"status":"Listo","message":"Todo bien"}'

        estado = consultar_estado_parlar(enviar)
        self.assertEqual(comandos, ["estado-operativo"])
        self.assertEqual(estado.categoria, "ready")

    def test_daemon_ausente_se_representa_como_detenido(self):
        def enviar(_comando):
            raise FileNotFoundError

        estado = consultar_estado_parlar(enviar)
        self.assertEqual(estado.categoria, "stopped")
        self.assertFalse(estado.ejecutandose)

    def test_respuesta_antigua_o_invalida_es_no_disponible(self):
        estado = consultar_estado_parlar(lambda _comando: "inactivo modo=utterance")
        self.assertEqual(estado.categoria, "unavailable")
        self.assertTrue(estado.ejecutandose)


if __name__ == "__main__":
    unittest.main()
