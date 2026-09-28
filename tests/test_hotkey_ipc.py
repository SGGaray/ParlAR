import json
import threading
import unittest
from types import SimpleNamespace

from parlar.app import App, EstadoApp
from parlar.daemon_atajos import DaemonAtajos
from parlar.estado_operativo import (
    EstadoComponente,
    EstadoOperativoStore,
    EstadoRuntime,
)

CTRL_R = 65508
SHIFT_R = 65506
CTRL_L = 65507
ALT_L = 65513
ESC = 65307


class KeyCodeFalso:
    def __init__(self, *, vk=None, char=None):
        self.vk = vk
        self.char = char


class KeyFalsa:
    def __init__(self, vk):
        self.value = SimpleNamespace(vk=vk)


class HotKeyFalso:
    @staticmethod
    def parse(combo):
        if combo == "<ctrl_r>+<shift_r>":
            return [KeyCodeFalso(vk=CTRL_R), KeyCodeFalso(vk=SHIFT_R)]
        return [
            KeyCodeFalso(vk=CTRL_L),
            KeyCodeFalso(vk=ALT_L),
            KeyCodeFalso(char="q"),
        ]

    def __init__(self, _teclas, _callback):
        pass

    def press(self, _tecla):
        pass

    def release(self, _tecla):
        pass


class ListenerFalso:
    def __init__(self, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release

    def canonical(self, tecla):
        return tecla


class TecladoFalso:
    HotKey = HotKeyFalso
    Listener = ListenerFalso
    Key = SimpleNamespace(esc=KeyFalsa(ESC))


def _store_listo():
    store = EstadoOperativoStore()
    for componente in ("stt", "audio", "hotkey", "control", "output"):
        store.fijar_componente(componente, EstadoComponente.READY)
    store.actualizar_runtime(EstadoRuntime.READY)
    return store


class AtajosLeaseFalso:
    def __init__(self):
        self.token = None
        self.suspendido = False

    def suspender(self, token, _duracion):
        if self.suspendido and self.token != token:
            return False
        self.token = token
        self.suspendido = True
        return True

    def renovar_suspension(self, token, _duracion):
        return self.suspendido and token == self.token

    def restaurar(self, token):
        if not self.suspendido or token != self.token:
            return False
        self.suspendido = False
        self.token = None
        return True

    def esta_suspendido(self):
        return self.suspendido


class PruebasSuspensionDaemon(unittest.TestCase):
    def test_suspension_impide_ptt_y_restauracion_lo_rehabilita(self):
        eventos = []
        daemon = DaemonAtajos(
            "<ctrl_r>+<shift_r>",
            "<ctrl>+<alt>+q",
            al_presionar=lambda: eventos.append("press"),
            al_soltar=lambda: eventos.append("release"),
            al_salir=lambda: eventos.append("quit"),
            al_cancelar=lambda: eventos.append("cancel"),
        )
        listener = daemon._crear_listener(TecladoFalso)
        token = "x" * 24
        self.assertTrue(daemon.suspender(token, 5.0))

        listener.on_press(KeyFalsa(CTRL_R))
        listener.on_press(KeyFalsa(SHIFT_R))
        listener.on_release(KeyFalsa(SHIFT_R))
        listener.on_release(KeyFalsa(CTRL_R))
        self.assertEqual(eventos, [])

        self.assertTrue(daemon.restaurar(token))
        listener.on_press(KeyFalsa(CTRL_R))
        listener.on_press(KeyFalsa(SHIFT_R))
        listener.on_release(KeyFalsa(SHIFT_R))
        self.assertEqual(eventos, ["press", "release"])

    def test_lease_ajeno_no_puede_reemplazar_ni_restaurar(self):
        daemon = DaemonAtajos(
            "<ctrl_r>+<shift_r>", "<ctrl>+<alt>+q",
            lambda: None, lambda: None, lambda: None)
        self.assertTrue(daemon.suspender("a" * 24, 5.0))
        self.assertFalse(daemon.suspender("b" * 24, 5.0))
        self.assertFalse(daemon.restaurar("b" * 24))
        self.assertTrue(daemon.esta_suspendido())

    def test_lease_expira_sin_cleanup_del_cliente(self):
        daemon = DaemonAtajos(
            "<ctrl_r>+<shift_r>", "<ctrl>+<alt>+q",
            lambda: None, lambda: None, lambda: None)
        self.assertTrue(daemon.suspender("a" * 24, 1.0))
        daemon._suspension_hasta = 0.0
        self.assertFalse(daemon.esta_suspendido())


class PruebasIPCHotkey(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app._estado_cv = threading.Condition()
        self.app._estado = EstadoApp.IDLE
        self.app.atajos = AtajosLeaseFalso()
        self.app.estado_operativo = _store_listo()
        self.token = "t" * 24

    def test_protocolo_versionado_suspende_renueva_y_restaura(self):
        self.assertEqual(
            self.app._atender_comando(
                f"hotkey-suspender v1 {self.token} 5000"),
            "OK hotkey suspendido v1",
        )
        self.assertEqual(
            self.app.estado_operativo.snapshot().hotkey,
            EstadoComponente.SUSPENDED,
        )
        self.assertEqual(
            self.app._atender_comando(
                f"hotkey-renovar v1 {self.token} 5000"),
            "OK hotkey suspendido v1",
        )
        self.assertEqual(
            self.app._atender_comando(
                f"hotkey-restaurar v1 {self.token}"),
            "OK hotkey restaurado v1",
        )
        self.assertEqual(
            self.app.estado_operativo.snapshot().hotkey,
            EstadoComponente.READY,
        )

    def test_runtime_ocupado_rechaza_suspension(self):
        self.app._estado = EstadoApp.RECORDING
        respuesta = self.app._atender_comando(
            f"hotkey-suspender v1 {self.token} 5000")
        self.assertEqual(respuesta, "ERR hotkey ocupado")
        self.assertFalse(self.app.atajos.suspendido)

    def test_token_y_ttl_estan_acotados(self):
        self.assertIn(
            "protocolo",
            self.app._atender_comando("hotkey-suspender v1 x 5000"),
        )
        self.assertIn(
            "protocolo",
            self.app._atender_comando(
                f"hotkey-suspender v1 {self.token} 60000"),
        )

    def test_estado_suspendido_es_atencion_y_no_error_rojo(self):
        self.app._atender_comando(
            f"hotkey-suspender v1 {self.token} 5000")
        datos = json.loads(self.app._atender_comando("estado-operativo"))
        self.assertEqual(datos["hotkey"], "suspended")
        self.assertEqual(datos["status"], "Requiere atención")
        self.assertEqual(datos["severity"], "info")
        self.assertIsNone(datos["blocking_error"])

    def test_expiracion_se_refleja_en_siguiente_consulta(self):
        self.app._atender_comando(
            f"hotkey-suspender v1 {self.token} 5000")
        self.app.atajos.suspendido = False
        datos = json.loads(self.app._atender_comando("estado-operativo"))
        self.assertEqual(datos["hotkey"], "ready")

    def test_comandos_anteriores_siguen_rechazando_desconocidos_igual(self):
        self.assertEqual(
            self.app._atender_comando("hotkey-legacy"),
            "ERR comando desconocido",
        )


if __name__ == "__main__":
    unittest.main()
