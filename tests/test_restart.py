"""Contrato headless del coordinador externo de reinicio seguro."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from parlar.app import EstadoApp
from parlar.restart import (
    ContextoRuntime,
    CoordinadorReinicio,
    EstadoResultadoReinicio,
    EstadoSystemd,
    GuardiaCoordinadorReinicio,
    comando_reinicio,
    runtime_administrado_por_systemd,
)
from tests.test_lifecycle import FrasesFalsas, crear_app


TOKEN = "token-reinicio-seguro-1234"


def comando(accion):
    return f"reiniciar v1 {TOKEN} {accion}"


class GuardiaFalsa:
    def __init__(self, adquirida=True):
        self.adquirida = adquirida
        self.liberaciones = 0

    def adquirir(self):
        return self.adquirida

    def liberar(self):
        self.liberaciones += 1


class RuntimeCoordinable:
    def __init__(self, *, systemd=False, listo=True):
        self.systemd = systemd
        self.listo = listo
        self.comandos = []

    def __call__(self, comando_runtime):
        self.comandos.append(comando_runtime)
        if comando_runtime == "contexto-reinicio":
            return json.dumps({
                "schema_version": 1,
                "pid": 4242,
                "systemd_invocation": self.systemd,
            })
        if comando_runtime.startswith("reiniciar v1"):
            return json.dumps({
                "schema_version": 1,
                "status": "accepted",
                "runtime": "idle",
                "safe_to_stop": True,
                "needs_stop": False,
            })
        if comando_runtime == "salir":
            return "OK chau"
        if comando_runtime == "estado-operativo" and self.listo:
            return '{"runtime":"ready"}'
        raise FileNotFoundError


class PruebasContratoRuntimeReinicio(unittest.TestCase):
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

    def test_idle_acepta_y_bloquea_start_nuevo(self):
        app, mic, *_ = self.app()

        respuesta = json.loads(app._atender_comando(comando("solicitar")))

        self.assertEqual(respuesta["status"], "accepted")
        self.assertTrue(respuesta["safe_to_stop"])
        self.assertFalse(app.iniciar_grabacion())
        self.assertEqual(mic.inicios, 0)

    def test_recording_requiere_decision_sin_alterar_captura(self):
        app, mic, *_ = self.app()
        app.iniciar_grabacion()

        respuesta = json.loads(app._atender_comando(comando("solicitar")))

        self.assertEqual(respuesta["status"], "confirmation_required")
        self.assertEqual(app.estado, EstadoApp.RECORDING)
        self.assertEqual(mic.detenciones, 0)
        self.assertTrue(app.iniciar_grabacion())

    def test_terminar_recording_hace_stop_y_preserva_texto(self):
        app, mic, _, _, _, _, sesion, fabrica = self.app()
        app.iniciar_grabacion()
        mic.enviar(17)
        self.assertTrue(fabrica.creada.wait(2))
        self.assertTrue(fabrica.instancias[-1].voz_iniciada.wait(2))

        respuesta = json.loads(app._atender_comando(comando("terminar")))

        self.assertEqual(respuesta["status"], "deferred")
        self.assertTrue(sesion.escrito.wait(2))
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(sesion.textos, ["U:17"])
        estado = json.loads(app._atender_comando(comando("estado")))
        self.assertTrue(estado["safe_to_stop"])
        self.assertFalse(app.iniciar_grabacion())

    def test_processing_difiere_y_termina_trabajo_valido(self):
        frases = FrasesFalsas(bloquear=True)
        app, mic, _, _, _, _, sesion, _ = self.app(frases=frases)
        app.iniciar_grabacion()
        mic.enviar(19)
        mic.enviar(0)
        self.assertTrue(frases.inferencia_iniciada.wait(2))
        app.detener_grabacion()

        respuesta = json.loads(app._atender_comando(comando("solicitar")))

        self.assertEqual(respuesta["status"], "deferred")
        self.assertFalse(respuesta["safe_to_stop"])
        self.assertFalse(app.iniciar_grabacion())
        frases.liberar.set()
        self.assertTrue(sesion.escrito.wait(2))
        self.assertTrue(app.esperar_estado(EstadoApp.IDLE))
        self.assertEqual(sesion.textos, ["U:19"])
        self.assertTrue(json.loads(
            app._atender_comando(comando("estado")))["safe_to_stop"])

    def test_paused_es_seguro_y_no_se_persiste_en_otra_instancia(self):
        app, *_ = self.app()
        app.pausar()
        respuesta = json.loads(app._atender_comando(comando("solicitar")))
        self.assertTrue(respuesta["safe_to_stop"])

        nueva, *_ = self.app()
        self.assertFalse(nueva.pausado)

    def test_reinicio_doble_se_deduplica_y_cancelar_rehabilita(self):
        app, *_ = self.app()
        primera = json.loads(app._atender_comando(comando("solicitar")))
        segunda = json.loads(app._atender_comando(
            "reiniciar v1 otro-token-seguro-5678 solicitar"))
        cancelada = json.loads(app._atender_comando(comando("cancelar")))

        self.assertEqual(primera["status"], "accepted")
        self.assertEqual(segunda["status"], "already_restarting")
        self.assertEqual(cancelada["status"], "accepted")
        self.assertTrue(app.iniciar_grabacion())

    def test_coordinador_caido_no_bloquea_start_para_siempre(self):
        app, mic, *_ = self.app()
        json.loads(app._atender_comando(comando("solicitar")))
        with app._estado_cv:
            app._reinicio_expira_en = 0.0

        self.assertTrue(app.iniciar_grabacion())
        self.assertEqual(mic.inicios, 1)

    def test_shutdown_historico_no_marca_reinicio(self):
        app, *_ = self.app()
        app.salir()
        self.assertEqual(app.estado, EstadoApp.CLOSED)
        self.assertFalse(app._reinicio_pendiente)


class PruebasCoordinadorExterno(unittest.TestCase):
    def coordinador(self, runtime, **cambios):
        proceso = SimpleNamespace(poll=lambda: None)
        parametros = dict(
            enviar=runtime,
            guardia=GuardiaFalsa(),
            consultar_systemd=lambda: EstadoSystemd(
                False, False, "dead", 0),
            iniciar_manual=lambda: proceso,
            lock_ocupado=lambda: False,
            socket_existe=lambda: False,
            pid_vivo=lambda _pid: False,
            esperar=lambda _segundos: None,
        )
        parametros.update(cambios)
        return CoordinadorReinicio(**parametros)

    def test_manual_apaga_limpia_inicia_una_vez_y_verifica_ready(self):
        runtime = RuntimeCoordinable()
        iniciar = mock.Mock(return_value=SimpleNamespace(poll=lambda: None))
        coordinador = self.coordinador(runtime, iniciar_manual=iniciar)

        resultado = coordinador.ejecutar()

        self.assertEqual(resultado.estado, EstadoResultadoReinicio.LISTO)
        iniciar.assert_called_once_with()
        self.assertEqual(runtime.comandos.count("salir"), 1)
        self.assertIn("estado-operativo", runtime.comandos)

    def test_systemd_managed_usa_restart_y_no_spawn_manual(self):
        runtime = RuntimeCoordinable(systemd=True)
        estados = iter((
            EstadoSystemd(True, True, "running", 4242),
            EstadoSystemd(True, False, "dead", 0),
        ))
        systemctl = mock.Mock(return_value=SimpleNamespace(returncode=0))
        iniciar = mock.Mock()
        coordinador = self.coordinador(
            runtime,
            consultar_systemd=lambda: next(
                estados, EstadoSystemd(True, True, "running", 5252)),
            ejecutar_systemctl=systemctl,
            iniciar_manual=iniciar,
        )

        resultado = coordinador.ejecutar()

        self.assertEqual(resultado.estado, EstadoResultadoReinicio.LISTO)
        systemctl.assert_called_once_with(
            "restart", "parlar.service", "--no-pager")
        iniciar.assert_not_called()

    def test_config_invalida_systemd_corta_auto_restart(self):
        runtime = RuntimeCoordinable(systemd=True, listo=False)
        estados = iter((
            EstadoSystemd(True, True, "running", 4242),
            EstadoSystemd(True, False, "dead", 0),
            EstadoSystemd(True, True, "auto-restart", 0),
        ))
        systemctl = mock.Mock(return_value=SimpleNamespace(returncode=0))
        coordinador = self.coordinador(
            runtime,
            consultar_systemd=lambda: next(
                estados, EstadoSystemd(True, True, "auto-restart", 0)),
            ejecutar_systemctl=systemctl,
        )

        resultado = coordinador.ejecutar()

        self.assertEqual(resultado.estado, EstadoResultadoReinicio.FALLIDO)
        self.assertEqual(resultado.gestor, "systemd")
        self.assertEqual(systemctl.call_args_list, [
            mock.call("restart", "parlar.service", "--no-pager"),
            mock.call("stop", "parlar.service", "--no-pager"),
        ])

    def test_guardia_ocupada_deduplica_sin_tocar_runtime(self):
        runtime = mock.Mock()
        guardia = GuardiaFalsa(adquirida=False)
        coordinador = self.coordinador(runtime, guardia=guardia)

        resultado = coordinador.ejecutar()

        self.assertEqual(
            resultado.estado, EstadoResultadoReinicio.YA_EN_CURSO)
        runtime.assert_not_called()
        self.assertEqual(guardia.liberaciones, 0)

    def test_deteccion_systemd_exige_claim_activo_y_pid_exacto(self):
        estado = EstadoSystemd(True, True, "running", 100)
        self.assertTrue(runtime_administrado_por_systemd(
            ContextoRuntime(100, True), estado))
        self.assertFalse(runtime_administrado_por_systemd(
            ContextoRuntime(101, True), estado))
        self.assertFalse(runtime_administrado_por_systemd(
            ContextoRuntime(100, False), estado))

    def test_guardia_real_libera_para_otro_coordinador(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "restart.lock"
            primera = GuardiaCoordinadorReinicio(ruta)
            segunda = GuardiaCoordinadorReinicio(ruta)
            self.assertTrue(primera.adquirir())
            self.assertFalse(segunda.adquirir())
            primera.liberar()
            self.assertTrue(segunda.adquirir())
            segunda.liberar()

    def test_comando_cli_es_explicito_y_compatible(self):
        self.assertEqual(
            comando_reinicio(executable="/opt/parlar/python"),
            ["/opt/parlar/python", "-m", "parlar", "--reiniciar"],
        )
        self.assertEqual(
            comando_reinicio(
                executable="/opt/parlar/python", terminar_dictado=True),
            [
                "/opt/parlar/python", "-m", "parlar", "--reiniciar",
                "--terminar-dictado",
            ],
        )
        self.assertEqual(
            comando_reinicio(
                executable="/opt/parlar/python",
                reintentar_gestor="systemd",
            ),
            [
                "/opt/parlar/python", "-m", "parlar", "--reiniciar",
                "--reintentar-reinicio", "systemd",
            ],
        )

    def test_reintento_manual_sin_runtime_inicia_y_verifica_ready(self):
        runtime = RuntimeCoordinable()
        iniciar = mock.Mock(return_value=SimpleNamespace(poll=lambda: None))
        coordinador = self.coordinador(runtime, iniciar_manual=iniciar)

        resultado = coordinador.reintentar("manual")

        self.assertEqual(resultado.estado, EstadoResultadoReinicio.LISTO)
        self.assertEqual(resultado.gestor, "manual")
        iniciar.assert_called_once_with()
        self.assertNotIn("contexto-reinicio", runtime.comandos)


if __name__ == "__main__":
    unittest.main(verbosity=2)
