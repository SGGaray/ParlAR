import unittest
from types import SimpleNamespace
from unittest import mock

from parlar.app import App
from parlar.config import Config
from parlar.daemon_atajos import (
    DaemonAtajos,
    _EstadoCombo,
    _identidad_tecla,
)


CTRL_L = 65507
CTRL_R = 65508
SHIFT_L = 65505
SHIFT_R = 65506
ALT_L = 65513


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
            return [
                KeyCodeFalso(vk=CTRL_R),
                KeyCodeFalso(vk=SHIFT_R),
            ]

        if combo == "<ctrl>+<alt>+q":
            return [
                KeyCodeFalso(vk=CTRL_L),
                KeyCodeFalso(vk=ALT_L),
                KeyCodeFalso(char="q"),
            ]

        raise ValueError(combo)

    def __init__(self, teclas, al_activar):
        self.teclas = teclas
        self.al_activar = al_activar

    def press(self, _tecla):
        pass

    def release(self, _tecla):
        pass


class ListenerFalso:
    def __init__(self, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release
        self.iniciado = False
        self.detenido = False

    def canonical(self, tecla):
        return tecla

    def start(self):
        self.iniciado = True

    def stop(self):
        self.detenido = True


class TecladoFalso:
    HotKey = HotKeyFalso
    Listener = ListenerFalso


class PruebasIdentidadFisica(unittest.TestCase):
    def test_distingue_ctrl_izquierdo_y_derecho(self):
        self.assertNotEqual(
            _identidad_tecla(
                KeyFalsa(CTRL_L)
            ),
            _identidad_tecla(
                KeyFalsa(CTRL_R)
            ),
        )

    def test_distingue_shift_izquierdo_y_derecho(self):
        self.assertNotEqual(
            _identidad_tecla(
                KeyFalsa(SHIFT_L)
            ),
            _identidad_tecla(
                KeyFalsa(SHIFT_R)
            ),
        )


class PruebasEstadoCombo(unittest.TestCase):
    def test_flancos_sin_repeticion(self):
        ctrl = ("vk", CTRL_R)
        shift = ("vk", SHIFT_R)

        estado = _EstadoCombo(
            {ctrl, shift}
        )

        self.assertFalse(
            estado.presionar(ctrl)
        )
        self.assertTrue(
            estado.presionar(shift)
        )

        self.assertFalse(
            estado.presionar(shift)
        )

        self.assertTrue(
            estado.soltar(shift)
        )
        self.assertFalse(
            estado.soltar(shift)
        )

    def test_lado_izquierdo_no_completa_combo_derecho(self):
        estado = _EstadoCombo({
            ("vk", CTRL_R),
            ("vk", SHIFT_R),
        })

        self.assertFalse(
            estado.presionar(
                ("vk", CTRL_L)
            )
        )
        self.assertFalse(
            estado.presionar(
                ("vk", SHIFT_L)
            )
        )
        self.assertFalse(
            estado.activa
        )


class PruebasDaemonAtajos(unittest.TestCase):
    def test_listener_emite_press_release_solo_con_lado_derecho(self):
        eventos = []

        daemon = DaemonAtajos(
            "<ctrl_r>+<shift_r>",
            "<ctrl>+<alt>+q",
            al_presionar=lambda:
                eventos.append("press"),
            al_soltar=lambda:
                eventos.append("release"),
            al_salir=lambda:
                eventos.append("quit"),
        )

        listener = daemon._crear_listener(
            TecladoFalso
        )

        listener.on_press(
            KeyFalsa(CTRL_L)
        )
        listener.on_press(
            KeyFalsa(SHIFT_L)
        )

        self.assertEqual(
            eventos,
            [],
        )

        listener.on_release(
            KeyFalsa(SHIFT_L)
        )
        listener.on_release(
            KeyFalsa(CTRL_L)
        )

        listener.on_press(
            KeyFalsa(CTRL_R)
        )
        listener.on_press(
            KeyFalsa(SHIFT_R)
        )

        self.assertEqual(
            eventos,
            ["press"],
        )

        # Key repeat no debe iniciar otra vez.
        listener.on_press(
            KeyFalsa(SHIFT_R)
        )

        self.assertEqual(
            eventos,
            ["press"],
        )

        listener.on_release(
            KeyFalsa(SHIFT_R)
        )

        self.assertEqual(
            eventos,
            ["press", "release"],
        )

        # Soltar el otro componente después no duplica STOP.
        listener.on_release(
            KeyFalsa(CTRL_R)
        )

        self.assertEqual(
            eventos,
            ["press", "release"],
        )

    def test_app_conecta_inicio_y_detencion_explicitos(self):
        cfg = Config()

        recurso = object()

        with mock.patch(
            "parlar.app.DaemonAtajos"
        ) as constructor:
            app = App(
                cfg,
                motor=recurso,
                frases=recurso,
                streaming=recurso,
                proc=recurso,
                inyector=recurso,
                guionar=recurso,
                sesion=recurso,
                mic=recurso,
                ui=recurso,
                control=recurso,
            )

        constructor.assert_called_once()

        args = constructor.call_args.args
        kwargs = constructor.call_args.kwargs

        self.assertEqual(
            args,
            (
                cfg.hotkey_toggle,
                cfg.hotkey_quit,
            ),
        )

        self.assertIs(
            kwargs["al_presionar"].__self__,
            app,
        )
        self.assertIs(
            kwargs["al_presionar"].__func__,
            App.iniciar_grabacion,
        )

        self.assertIs(
            kwargs["al_soltar"].__self__,
            app,
        )
        self.assertIs(
            kwargs["al_soltar"].__func__,
            App.detener_grabacion,
        )

        self.assertIs(
            kwargs["al_salir"].__self__,
            app,
        )
        self.assertIs(
            kwargs["al_salir"].__func__,
            App.salir,
        )


if __name__ == "__main__":
    unittest.main()
