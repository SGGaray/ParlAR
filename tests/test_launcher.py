"""Contrato del launcher gráfico sin requerir display ni hardware."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import parlar.launcher as launcher
import parlar.settings_window as settings_window
from parlar import __main__ as entrada
from parlar.app import App, EstadoApp, foco_settings_seguro
from parlar.config import Config, ErrorConfiguracion
from parlar.control import normalizar_comando
from parlar.settings_backend import (
    EstadoParlARSettings,
    CargaConfiguracionSettings,
    cargar_configuracion_recuperable,
    estado_fallo_inicio,
    estado_preparando_parlar,
    representar_estado_parlar,
)
from parlar.settings_window import ControlSettings, VentanaSettings, valores_desde_snapshot
from tests.test_lifecycle import crear_app


class ProcesoFalso:
    def __init__(self, codigo=None):
        self.codigo = codigo

    def poll(self):
        return self.codigo


class PruebasDecisionLauncher(unittest.TestCase):
    def test_respuesta_ipc_allow(self):
        estado = launcher.interpretar_respuesta_apertura(
            '{"schema_version":1,"runtime":"idle","focus":"allow"}')
        self.assertEqual(estado.runtime, "idle")
        self.assertTrue(estado.permitir_foco)

    def test_respuesta_ipc_defer(self):
        estado = launcher.interpretar_respuesta_apertura(
            '{"schema_version":1,"runtime":"recording","focus":"defer"}')
        self.assertFalse(estado.permitir_foco)

    def test_respuesta_ipc_invalida_falla_cerrado(self):
        for respuesta in ("no-json", "[]", '{"schema_version":2}'):
            with self.subTest(respuesta=respuesta), self.assertRaises(ValueError):
                launcher.interpretar_respuesta_apertura(respuesta)

    def test_runtime_activo_usa_ipc_y_no_consulta_lock(self):
        lock = mock.Mock(side_effect=AssertionError("lock innecesario"))
        estado = launcher.consultar_apertura_runtime(
            enviar=lambda _cmd: (
                '{"schema_version":1,"runtime":"idle","focus":"allow"}'),
            lock_ocupado=lock,
        )
        self.assertEqual(estado.runtime, "idle")
        lock.assert_not_called()

    def test_runtime_ausente_se_distingue_de_starting(self):
        def ausente(_cmd):
            raise FileNotFoundError

        detenido = launcher.consultar_apertura_runtime(
            enviar=ausente, lock_ocupado=lambda: False)
        iniciando = launcher.consultar_apertura_runtime(
            enviar=ausente, lock_ocupado=lambda: True)
        self.assertEqual(detenido.runtime, "stopped")
        self.assertEqual(iniciando.runtime, "starting")

    def test_endpoint_incompatible_no_autorizaria_segundo_runtime(self):
        estado = launcher.consultar_apertura_runtime(
            enviar=lambda _cmd: "respuesta vieja",
            lock_ocupado=lambda: False,
        )
        self.assertEqual(estado.runtime, "unavailable")
        self.assertFalse(estado.permitir_foco)

    def test_alias_ipc_es_backward_compatible(self):
        self.assertEqual(
            normalizar_comando("open-settings"),
            ["abrir-configuracion"],
        )

    def test_politica_de_foco_cubre_dictado_completo(self):
        for estado in (EstadoApp.STARTING, EstadoApp.RECORDING, EstadoApp.STOPPING):
            with self.subTest(estado=estado):
                self.assertFalse(foco_settings_seguro(estado))
        for estado in (
            EstadoApp.IDLE, EstadoApp.ERROR,
            EstadoApp.SHUTTING_DOWN, EstadoApp.CLOSED,
        ):
            with self.subTest(estado=estado):
                self.assertTrue(foco_settings_seguro(estado))

    def test_app_publica_decision_de_foco_sin_paths_ni_texto(self):
        app, *_ = crear_app()
        for estado, foco in (
            (EstadoApp.IDLE, "allow"),
            (EstadoApp.RECORDING, "defer"),
            (EstadoApp.STOPPING, "defer"),
        ):
            with self.subTest(estado=estado):
                with app._estado_cv:
                    app._estado = estado
                datos = json.loads(app._atender_comando("abrir-configuracion"))
                self.assertEqual(datos, {
                    "schema_version": 1,
                    "runtime": estado.value,
                    "focus": foco,
                })


class PruebasProcesoLauncher(unittest.TestCase):
    def test_spawn_usa_cli_historico_desacoplado(self):
        popen = mock.Mock(return_value=ProcesoFalso())
        proceso = launcher.iniciar_runtime(
            popen=popen, executable="/opt/parlar/bin/python")
        self.assertIs(proceso, popen.return_value)
        self.assertEqual(
            popen.call_args.args[0],
            ["/opt/parlar/bin/python", "-m", "parlar"],
        )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_observador_muestra_preparando_mientras_hijo_vive(self):
        observador = launcher.ObservadorRuntime(
            ProcesoFalso(None),
            consultar=lambda: representar_estado_parlar(None),
            lock_ocupado=lambda: False,
        )
        estado = observador.consultar()
        self.assertEqual(estado.categoria, "starting")
        self.assertIn("Preparando", estado.titulo)

    def test_observador_prefiere_estado_real_del_socket(self):
        listo = EstadoParlARSettings("ready", "Listo", "Listo.", True)
        estado = launcher.ObservadorRuntime(
            ProcesoFalso(None), consultar=lambda: listo).consultar()
        self.assertIs(estado, listo)

    def test_fallo_pre_socket_se_vuelve_recuperable(self):
        observador = launcher.ObservadorRuntime(
            ProcesoFalso(7),
            consultar=lambda: representar_estado_parlar(None),
            lock_ocupado=lambda: False,
        )
        estado = observador.consultar()
        self.assertFalse(estado.ejecutandose)
        self.assertIn("código 7", estado.mensaje)
        self.assertIn("configuración", estado.mensaje)

    def test_otro_runtime_starting_no_dispara_otro_hijo(self):
        observador = launcher.ObservadorRuntime(
            consultar=lambda: representar_estado_parlar(None),
            lock_ocupado=lambda: True,
        )
        self.assertEqual(observador.consultar().categoria, "starting")

    def test_launcher_ausente_inicia_una_vez_y_abre_settings(self):
        proceso = ProcesoFalso(None)
        with (
            mock.patch.object(
                launcher, "consultar_apertura_runtime",
                return_value=launcher.EstadoApertura("stopped", True)),
            mock.patch.object(
                launcher, "iniciar_runtime", return_value=proceso) as iniciar,
            mock.patch("parlar.settings_window.main", return_value=19) as settings,
        ):
            self.assertEqual(launcher.main(), 19)
        iniciar.assert_called_once_with()
        self.assertTrue(settings.call_args.kwargs["permitir_foco_inicial"])

    def test_launcher_activo_no_inicia_segunda_app(self):
        with (
            mock.patch.object(
                launcher, "consultar_apertura_runtime",
                return_value=launcher.EstadoApertura("idle", True)),
            mock.patch.object(launcher, "iniciar_runtime") as iniciar,
            mock.patch("parlar.settings_window.main", return_value=0),
        ):
            self.assertEqual(launcher.main(), 0)
        iniciar.assert_not_called()

    def test_cerrar_settings_no_termina_el_runtime_iniciado(self):
        proceso = mock.Mock()
        proceso.poll.return_value = None
        with (
            mock.patch.object(
                launcher, "consultar_apertura_runtime",
                return_value=launcher.EstadoApertura("stopped", True)),
            mock.patch.object(
                launcher, "iniciar_runtime", return_value=proceso),
            mock.patch("parlar.settings_window.main", return_value=0),
        ):
            self.assertEqual(launcher.main(), 0)
        proceso.terminate.assert_not_called()
        proceso.kill.assert_not_called()

    def test_runtime_detenido_se_publica_al_reabrir(self):
        detenido = representar_estado_parlar(None)
        observador = launcher.ObservadorRuntime(
            consultar=lambda: detenido,
            lock_ocupado=lambda: False,
        )
        self.assertEqual(observador.consultar().categoria, "stopped")

    def test_cli_grafico_evade_config_invalida_y_runtime_pesado(self):
        with (
            mock.patch.object(sys, "argv", ["parlar", "--abrir-configuracion"]),
            mock.patch.object(entrada.Config, "load") as cargar,
            mock.patch("parlar.launcher.main", return_value=23) as grafico,
        ):
            self.assertEqual(entrada.main(), 23)
        cargar.assert_not_called()
        grafico.assert_called_once_with()

    def test_import_launcher_no_carga_tk_whisper_ni_app(self):
        codigo = """
import sys
import parlar.launcher
prohibidos = {'tkinter', 'faster_whisper', 'parlar.app', 'sounddevice'}
assert prohibidos.isdisjoint(sys.modules), prohibidos & set(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


class PruebasSingletonSettings(unittest.TestCase):
    def test_lock_impide_duplicado_y_solicita_presentacion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "settings.lock"
            primera = launcher.GuardiaVentanaSettings(ruta)
            segunda = launcher.GuardiaVentanaSettings(ruta)
            try:
                self.assertTrue(primera.adquirir_o_solicitar_presentacion())
                self.assertFalse(segunda.adquirir_o_solicitar_presentacion())
                self.assertTrue(primera.consumir_presentacion())
                self.assertFalse(primera.consumir_presentacion())
            finally:
                segunda.liberar()
                primera.liberar()

    def test_liberar_permite_reapertura(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "settings.lock"
            primera = launcher.GuardiaVentanaSettings(ruta)
            segunda = launcher.GuardiaVentanaSettings(ruta)
            self.assertTrue(primera.adquirir_o_solicitar_presentacion())
            primera.liberar()
            try:
                self.assertTrue(segunda.adquirir_o_solicitar_presentacion())
            finally:
                segunda.liberar()

    def test_lock_rechaza_objeto_que_no_es_archivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "settings.lock"
            ruta.mkdir()
            guardia = launcher.GuardiaVentanaSettings(ruta)
            with self.assertRaises(OSError):
                guardia.adquirir_o_solicitar_presentacion()


class PruebasEstadoSettingsLauncher(unittest.TestCase):
    def test_starting_se_representa_como_preparando(self):
        estado = representar_estado_parlar({
            "runtime": "starting",
            "status": "Requiere atención",
            "message": "viejo",
        })
        self.assertEqual(estado, estado_preparando_parlar())

    def test_fallo_inicio_no_expone_detalle_si_no_se_proporciona(self):
        estado = estado_fallo_inicio(None)
        self.assertEqual(estado.titulo, "No disponible")
        self.assertNotIn("None", estado.mensaje)

    def test_config_invalida_abre_con_defaults_reparables(self):
        with mock.patch.object(
                Config, "load", side_effect=ErrorConfiguracion("device=tpu")):
            carga = cargar_configuracion_recuperable()
        self.assertIsInstance(carga.configuracion, Config)
        self.assertIn("device=tpu", carga.advertencia)

    def test_settings_abre_sin_runtime(self):
        guardia = mock.Mock()
        guardia.adquirir_o_solicitar_presentacion.return_value = True
        root = mock.Mock()
        modulo_tk = SimpleNamespace(Tk=mock.Mock(return_value=root))
        carga = CargaConfiguracionSettings(Config(), None)
        with (
            mock.patch.object(
                launcher, "GuardiaVentanaSettings", return_value=guardia),
            mock.patch.object(
                settings_window, "cargar_configuracion_recuperable",
                return_value=carga),
            mock.patch.object(settings_window, "obtener_capacidades"),
            mock.patch.object(
                settings_window, "cargar_inventario_entradas",
                return_value=SimpleNamespace(dispositivos=(), error=None)),
            mock.patch.object(
                settings_window, "consultar_estado_parlar",
                return_value=representar_estado_parlar(None)),
            mock.patch.object(settings_window, "VentanaSettings") as ventana,
            mock.patch.dict(sys.modules, {"tkinter": modulo_tk}),
        ):
            self.assertEqual(settings_window.main(), 0)
        ventana.assert_called_once()
        root.mainloop.assert_called_once_with()
        guardia.liberar.assert_called_once_with()

    def test_reparacion_habilita_guardado_y_exige_reinicio(self):
        base = Config()
        from parlar.settings_backend import snapshot_configuracion, ResultadoPersistencia
        snapshot = snapshot_configuracion(base)
        persistir = mock.Mock(return_value=ResultadoPersistencia(snapshot, False))
        control = ControlSettings(
            base, snapshot, persistir=persistir, requiere_reparacion=True)
        valores = valores_desde_snapshot(snapshot)
        self.assertTrue(control.esta_sucio(valores))
        resultado = control.guardar(valores)
        self.assertTrue(resultado.requires_restart)
        self.assertFalse(control.esta_sucio(valores))

    def test_presentacion_diferida_no_fuerza_foco(self):
        ventana = VentanaSettings.__new__(VentanaSettings)
        ventana.root = mock.Mock()
        ventana._puede_presentar = lambda: False
        ventana._presentacion_pendiente = False
        ventana._presentacion_visible = False
        self.assertFalse(ventana._presentar())
        self.assertTrue(ventana._presentacion_pendiente)
        ventana.root.focus_force.assert_not_called()

    def test_presentacion_idle_muestra_y_enfoca(self):
        ventana = VentanaSettings.__new__(VentanaSettings)
        ventana.root = mock.Mock()
        ventana._puede_presentar = lambda: True
        ventana._presentacion_pendiente = True
        ventana._presentacion_visible = False
        self.assertTrue(ventana._presentar())
        ventana.root.deiconify.assert_called_once_with()
        ventana.root.focus_force.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
