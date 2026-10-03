"""Contrato headless del tray, la pausa y su IPC local."""

import io
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from parlar.app import EstadoApp
from parlar.estado_operativo import (
    EstadoComponente,
    EstadoOperativoStore,
    EstadoRuntime,
    ProblemaOperativo,
    Severidad,
    puede_dictar,
    resumen_humano,
    serializar_estado,
)
from parlar.tray import (
    TrayLinux,
    resumir_estado_tray,
    serializar_estado_tray,
)
from parlar.tray_gtk import (
    ETIQUETA_CONFIGURACION,
    ETIQUETA_SALIR,
    TITULO_MENU,
    comando_abrir_configuracion,
    validar_estado,
)
from tests.test_lifecycle import FrasesFalsas, UIFalsa, crear_app


def store_listo():
    store = EstadoOperativoStore()
    for componente in ("stt", "audio", "hotkey", "control", "output"):
        store.fijar_componente(componente, EstadoComponente.READY)
    store.actualizar_runtime(EstadoRuntime.READY)
    return store


def marcar_app_lista(app):
    for componente in ("stt", "audio", "hotkey", "control", "output"):
        app.estado_operativo.fijar_componente(
            componente, EstadoComponente.READY)
    app.estado_operativo.actualizar_runtime(EstadoRuntime.READY)


class ProcesoFalso:
    def __init__(self, *, timeout=False):
        self.stdout = io.StringIO("")
        self._vivo = True
        self.timeout = timeout
        self.terminaciones = 0
        self.kills = 0
        self.esperas = 0

    def poll(self):
        return None if self._vivo else 0

    def terminate(self):
        self.terminaciones += 1

    def kill(self):
        self.kills += 1
        self._vivo = False

    def wait(self, timeout=None):
        self.esperas += 1
        if self.timeout and self.esperas == 1:
            raise subprocess.TimeoutExpired("tray", timeout)
        self._vivo = False
        return 0


class TrayFalso:
    def __init__(self, *, fallar=False):
        self.fallar = fallar
        self.inicios = 0
        self.detenciones = 0

    def iniciar(self):
        self.inicios += 1
        if self.fallar:
            raise RuntimeError("sin host")
        return True

    def detener(self):
        self.detenciones += 1


class UIEjecutable(UIFalsa):
    def ejecutar(self):
        return None


class PruebasModeloTray(unittest.TestCase):
    def test_disponible(self):
        estado = resumir_estado_tray("idle", store_listo().snapshot())
        self.assertEqual(estado.categoria, "available")
        self.assertEqual(estado.estado, "Disponible")
        self.assertEqual(estado.tooltip, "ParlAR — Disponible")
        self.assertEqual(estado.accion_pausa, "Pausar")

    def test_pausado(self):
        store = store_listo()
        store.actualizar_pausa(True)
        estado = resumir_estado_tray("idle", store.snapshot())
        self.assertEqual(estado.categoria, "paused")
        self.assertEqual(estado.accion_pausa, "Reanudar")
        self.assertEqual(estado.comando_pausa, "reanudar")

    def test_grabando_es_ocupado_y_detener_pausar(self):
        estado = resumir_estado_tray("recording", store_listo().snapshot())
        self.assertEqual(estado.categoria, "busy")
        self.assertEqual(estado.estado, "Escuchando")
        self.assertEqual(estado.accion_pausa, "Detener y pausar")

    def test_procesando_es_ocupado_y_difiere_pausa(self):
        estado = resumir_estado_tray("stopping", store_listo().snapshot())
        self.assertEqual(estado.tooltip, "ParlAR — Procesando")
        self.assertEqual(estado.accion_pausa, "Pausar al terminar")

    def test_pausa_pendiente_durante_processing_es_explicita(self):
        store = store_listo()
        store.actualizar_pausa(True)
        estado = resumir_estado_tray(
            "stopping", store.snapshot(), pausa_pendiente=True)
        self.assertEqual(estado.estado, "Procesando · pausa pendiente")
        self.assertEqual(estado.accion_pausa, "Cancelar pausa")

    def test_warning_es_requiere_atencion_sin_bloquear(self):
        store = store_listo()
        store.reportar_problema(ProblemaOperativo(
            "tray", "tray", Severidad.WARNING, "Tray ausente"))
        self.assertTrue(puede_dictar(store.snapshot()))
        estado = resumir_estado_tray("idle", store.snapshot())
        self.assertEqual(estado.categoria, "attention")
        self.assertEqual(estado.tooltip, "ParlAR — Requiere atención")

    def test_error_operativo_es_no_disponible(self):
        store = store_listo()
        store.fijar_componente(
            "audio", EstadoComponente.ERROR,
            problema=ProblemaOperativo(
                "audio", "audio", Severidad.BLOCKING, "Sin audio"))
        estado = resumir_estado_tray("idle", store.snapshot())
        self.assertEqual(estado.categoria, "unavailable")
        self.assertFalse(estado.pausa_habilitada)

    def test_starting_es_preparando(self):
        estado = resumir_estado_tray(
            "idle", EstadoOperativoStore().snapshot())
        self.assertEqual(estado.estado, "Preparando…")

    def test_serializacion_es_versionada_y_acotada(self):
        payload = serializar_estado_tray(
            resumir_estado_tray("idle", store_listo().snapshot()))
        datos = json.loads(payload)
        self.assertEqual(datos["schema_version"], 1)
        self.assertEqual(set(datos), {
            "schema_version", "category", "status", "tooltip",
            "pause_action", "pause_command", "pause_enabled",
        })

    def test_serializacion_no_expone_contenido_ni_dispositivos(self):
        payload = serializar_estado_tray(
            resumir_estado_tray("idle", store_listo().snapshot())).lower()
        for prohibido in (
                "transcript", "context", "audio_device", "microfono",
                "model", "path"):
            self.assertNotIn(prohibido, payload)

    def test_menu_estatico_es_minimo(self):
        self.assertEqual(
            (TITULO_MENU, ETIQUETA_CONFIGURACION, ETIQUETA_SALIR),
            ("ParlAR", "Configuración…", "Salir"))

    def test_menu_no_incluye_ajustes_ni_historial(self):
        menu = " ".join(
            (TITULO_MENU, ETIQUETA_CONFIGURACION, ETIQUETA_SALIR)).lower()
        for prohibido in ("modelo", "micrófono", "idioma", "historial"):
            self.assertNotIn(prohibido, menu)

    def test_configuracion_reutiliza_launcher_ux05(self):
        self.assertEqual(
            comando_abrir_configuracion("/opt/parlar/python"),
            ["/opt/parlar/python", "-m", "parlar", "--abrir-configuracion"])

    def test_helper_valida_payload(self):
        payload = serializar_estado_tray(
            resumir_estado_tray("idle", store_listo().snapshot()))
        self.assertEqual(validar_estado(payload)["status"], "Disponible")

    def test_helper_rechaza_version_y_accion_invalidas(self):
        base = json.loads(serializar_estado_tray(
            resumir_estado_tray("idle", store_listo().snapshot())))
        for cambio in (
                {"schema_version": 2}, {"pause_command": "borrar"}):
            with self.subTest(cambio=cambio), self.assertRaises(ValueError):
                validar_estado(json.dumps(base | cambio))


class PruebasLifecycleTray(unittest.TestCase):
    def test_sin_display_no_inicia_y_reporta_una_vez(self):
        fallos = []
        popen = mock.Mock()
        tray = TrayLinux(
            entorno={}, popen=popen, al_fallo=fallos.append)
        self.assertFalse(tray.iniciar())
        self.assertFalse(tray.iniciar())
        popen.assert_not_called()
        self.assertEqual(len(fallos), 1)

    def test_tray_disponible_se_inicia_una_vez(self):
        proceso = ProcesoFalso()
        popen = mock.Mock(return_value=proceso)
        with mock.patch("parlar.tray.threading.Thread") as hilo:
            tray = TrayLinux(entorno={"DISPLAY": ":1"}, popen=popen)
            self.assertTrue(tray.iniciar())
            self.assertTrue(tray.iniciar())
        popen.assert_called_once()
        hilo.assert_called_once()
        tray.detener()

    def test_spawn_usa_helper_socket_icono_y_python_del_launcher(self):
        proceso = ProcesoFalso()
        popen = mock.Mock(return_value=proceso)
        with mock.patch("parlar.tray.threading.Thread"):
            tray = TrayLinux(
                ruta_socket="/tmp/parlar-prueba.sock",
                python_launcher="/opt/parlar/venv/bin/python",
                helper_python="/usr/bin/python3",
                entorno={"DISPLAY": ":1"}, popen=popen)
            tray.iniciar()
        comando = popen.call_args.args[0]
        self.assertEqual(comando[0], "/usr/bin/python3")
        self.assertIn("tray_gtk.py", comando[1])
        self.assertIn("/tmp/parlar-prueba.sock", comando)
        self.assertIn("/opt/parlar/venv/bin/python", comando)
        self.assertTrue(comando[-1].endswith("parlar/assets/parlar.svg"))
        tray.detener()

    def test_detener_retira_helper_ordenadamente(self):
        proceso = ProcesoFalso()
        with mock.patch("parlar.tray.threading.Thread"):
            tray = TrayLinux(
                entorno={"DISPLAY": ":1"}, popen=lambda *_a, **_k: proceso)
            tray.iniciar()
        tray.detener()
        self.assertEqual(proceso.terminaciones, 1)
        self.assertEqual(proceso.kills, 0)
        self.assertFalse(tray.iniciado)

    def test_helper_colgado_tiene_fallback_acotado(self):
        proceso = ProcesoFalso(timeout=True)
        with mock.patch("parlar.tray.threading.Thread"):
            tray = TrayLinux(
                entorno={"DISPLAY": ":1"}, popen=lambda *_a, **_k: proceso)
            tray.iniciar()
        tray.detener()
        self.assertEqual(proceso.terminaciones, 1)
        self.assertEqual(proceso.kills, 1)

    def test_import_no_carga_gtk_tk_ni_hardware(self):
        codigo = """
import sys
import parlar.tray
prohibidos = {'gi', 'tkinter', 'sounddevice', 'faster_whisper'}
assert prohibidos.isdisjoint(sys.modules), prohibidos & set(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo], capture_output=True,
            text=True, timeout=10, check=False)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


class PruebasPausaApp(unittest.TestCase):
    def setUp(self):
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            if app.estado != EstadoApp.CLOSED:
                app.salir()

    def app(self, **kwargs):
        componentes = crear_app(**kwargs)
        self.apps.append(componentes[0])
        return componentes

    def test_pausa_idle_es_inmediata(self):
        app, mic, *_ = self.app()
        self.assertTrue(app.pausar())
        self.assertTrue(app.pausado)
        self.assertEqual(app.estado, EstadoApp.IDLE)
        self.assertEqual(mic.inicios, 0)
        self.assertFalse(puede_dictar(app.estado_operativo.snapshot()))

    def test_pausado_bloquea_start_nuevo(self):
        app, mic, *_ = self.app()
        app.pausar()
        self.assertFalse(app.iniciar_grabacion())
        self.assertEqual(mic.inicios, 0)
        self.assertIn("pausado", app._ultimo_error)

    def test_reanudar_permite_start_sin_iniciarlo_solo(self):
        app, mic, *_ = self.app()
        app.pausar()
        self.assertTrue(app.reanudar())
        self.assertEqual(mic.inicios, 0)
        self.assertTrue(app.iniciar_grabacion())
        self.assertEqual(mic.inicios, 1)

    def test_continuo_no_empieza_mientras_esta_pausado(self):
        app, mic, *_ = self.app()
        app.pausar()
        app.gesto_dictado.presionar()
        app.gesto_dictado.soltar()
        self.assertEqual(mic.inicios, 0)
        self.assertFalse(app.gesto_dictado.continuo_activo)

    def test_pausar_grabando_detiene_y_conserva_texto_valido(self):
        app, mic, _, _, _, _, sesion, fabrica = self.app()
        self.assertTrue(app.iniciar_grabacion())
        mic.enviar(7)
        self.assertTrue(fabrica.creada.wait(2))
        self.assertTrue(fabrica.instancias[-1].voz_iniciada.wait(2))
        self.assertTrue(app.pausar())
        self.assertTrue(sesion.escrito.wait(2))
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(sesion.textos, ["U:7"])
        self.assertTrue(app.pausado)
        self.assertFalse(app.iniciar_grabacion())

    def test_pausar_processing_espera_y_entrega_texto(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, _, _, _, _, sesion, _ = self.app(frases=frases)
        app.iniciar_grabacion()
        mic.enviar(8)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        app.detener_grabacion()
        self.assertEqual(app.estado, EstadoApp.STOPPING)
        self.assertTrue(app.pausar())
        self.assertEqual(sesion.textos, [])
        frases.liberar.set()
        self.assertTrue(sesion.escrito.wait(2))
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(sesion.textos, ["U:8"])
        self.assertTrue(app.pausado)

    def test_reanudar_cancela_pausa_pendiente_sin_start_automatico(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, *_ = self.app(frases=frases)
        marcar_app_lista(app)
        app.iniciar_grabacion()
        mic.enviar(9)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        app.detener_grabacion()
        app.pausar()
        self.assertTrue(app.reanudar())
        self.assertFalse(app.pausado)
        self.assertEqual(app.estado, EstadoApp.STOPPING)
        frases.liberar.set()
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))

    def test_cancelar_processing_pausado_limpia_la_pausa_pendiente(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, *_ = self.app(frases=frases)
        marcar_app_lista(app)
        app.iniciar_grabacion()
        mic.enviar(10)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        app.detener_grabacion()
        app.pausar()
        self.assertTrue(app.cancelar_grabacion())
        datos = json.loads(app._atender_comando("estado-tray"))
        self.assertEqual(datos["status"], "Pausado")
        frases.liberar.set()

    def test_ipc_pausar_reanudar_y_aliases(self):
        app, *_ = self.app()
        self.assertEqual(app._atender_comando("pause"), "OK pausado")
        self.assertTrue(app.pausado)
        self.assertTrue(app._atender_comando("start").startswith("ERR"))
        self.assertEqual(app._atender_comando("resume"), "OK reanudado")

    def test_estado_tray_ipc_no_contiene_datos_sensibles(self):
        app, *_ = self.app()
        marcar_app_lista(app)
        datos = json.loads(app._atender_comando("tray-status"))
        self.assertEqual(datos["status"], "Disponible")
        payload = json.dumps(datos).lower()
        for prohibido in ("texto", "context", "audio_device", "transcript"):
            self.assertNotIn(prohibido, payload)

    def test_estado_operativo_publica_pausa(self):
        app, *_ = self.app()
        marcar_app_lista(app)
        app.pausar()
        datos = json.loads(app._atender_comando("estado-operativo"))
        self.assertTrue(datos["paused"])
        self.assertFalse(datos["can_dictate"])
        self.assertEqual(resumen_humano(
            app.estado_operativo.snapshot())[0], "Pausado")

    def test_fallo_tray_es_warning_y_no_bloquea_dictado(self):
        app, *_ = self.app()
        marcar_app_lista(app)
        app._reportar_fallo_tray("sin host")
        estado = app.estado_operativo.snapshot()
        self.assertTrue(puede_dictar(estado))
        self.assertEqual(estado.warnings[-1].codigo, "tray_no_disponible")

    def test_ejecutar_inicia_y_shutdown_retira_un_tray(self):
        app, *_ = self.app()
        app.ui = UIEjecutable()
        tray = TrayFalso()
        app.tray = tray
        app.ejecutar()
        self.assertEqual(tray.inicios, 1)
        self.assertEqual(tray.detenciones, 1)
        self.assertEqual(app.estado, EstadoApp.CLOSED)

    def test_shutdown_repetido_no_retira_dos_veces(self):
        app, *_ = self.app()
        tray = TrayFalso()
        app.tray = tray
        app.salir()
        app.salir()
        self.assertEqual(tray.detenciones, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
