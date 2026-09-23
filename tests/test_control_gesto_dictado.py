import unittest

from parlar.control_gesto_dictado import (
    ControlGestoDictado,
)


class RelojFalso:
    def __init__(self):
        self.ahora = 0.0

    def __call__(self):
        return self.ahora


class TimerFalso:
    def __init__(self, demora, callback):
        self.demora = demora
        self.callback = callback
        self.daemon = False
        self.iniciado = False
        self.cancelado = False

    def start(self):
        self.iniciado = True

    def cancel(self):
        self.cancelado = True

    def disparar(self):
        if not self.cancelado:
            self.callback()


class FabricaTimers:
    def __init__(self):
        self.timers = []

    def __call__(self, demora, callback):
        timer = TimerFalso(
            demora,
            callback,
        )
        self.timers.append(timer)
        return timer


class PruebasControlGestoDictado(unittest.TestCase):
    def setUp(self):
        self.reloj = RelojFalso()
        self.timers = FabricaTimers()
        self.eventos = []

        self.control = ControlGestoDictado(
            al_iniciar=lambda:
                self.eventos.append("start")
                or True,
            al_detener=lambda:
                self.eventos.append("stop")
                or True,
            reloj=self.reloj,
            timer_factory=self.timers,
        )

    def test_hold_detiene_inmediatamente_sin_timer(self):
        self.reloj.ahora = 1.000
        self.control.presionar()

        self.reloj.ahora = 1.500
        self.control.soltar()

        self.assertEqual(
            self.eventos,
            ["start", "stop"],
        )
        self.assertEqual(
            self.timers.timers,
            [],
        )

    def test_tap_simple_detiene_al_vencer_ventana(self):
        self.reloj.ahora = 1.000
        self.control.presionar()

        self.reloj.ahora = 1.080
        self.control.soltar()

        self.assertEqual(
            self.eventos,
            ["start"],
        )
        self.assertEqual(
            len(self.timers.timers),
            1,
        )

        timer = self.timers.timers[-1]

        self.assertAlmostEqual(
            timer.demora,
            0.300,
            places=6,
        )

        self.reloj.ahora = 1.380
        timer.disparar()

        self.assertEqual(
            self.eventos,
            ["start", "stop"],
        )

    def test_doble_tap_entra_y_sale_continuo_sin_segundo_start(self):
        # Entrada.
        self.reloj.ahora = 1.000
        self.control.presionar()

        self.reloj.ahora = 1.080
        self.control.soltar()

        timer_entrada = self.timers.timers[-1]

        self.reloj.ahora = 1.200
        self.control.presionar()

        self.assertTrue(
            timer_entrada.cancelado
        )
        self.assertTrue(
            self.control.continuo_activo
        )
        self.assertEqual(
            self.eventos,
            ["start"],
        )

        self.reloj.ahora = 1.280
        self.control.soltar()

        self.assertEqual(
            self.eventos,
            ["start"],
        )

        # Salida: doble tap.
        self.reloj.ahora = 2.000
        self.control.presionar()

        self.reloj.ahora = 2.080
        self.control.soltar()

        self.assertTrue(
            self.control.continuo_activo
        )

        self.reloj.ahora = 2.200
        self.control.presionar()

        self.reloj.ahora = 2.280
        self.control.soltar()

        self.assertFalse(
            self.control.continuo_activo
        )
        self.assertEqual(
            self.eventos,
            ["start", "stop"],
        )

    def test_cerrar_cancela_stop_pendiente(self):
        self.reloj.ahora = 1.000
        self.control.presionar()

        self.reloj.ahora = 1.080
        self.control.soltar()

        timer = self.timers.timers[-1]

        self.control.cerrar()

        self.assertTrue(
            timer.cancelado
        )

        self.reloj.ahora = 2.000
        timer.disparar()

        self.assertEqual(
            self.eventos,
            ["start"],
        )


if __name__ == "__main__":
    unittest.main()
