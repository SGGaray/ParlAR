import unittest

from parlar.gesto_dictado import (
    CANCELAR_STOP,
    CONTINUO_OFF,
    CONTINUO_ON,
    DETENER,
    INICIAR,
    PROGRAMAR_STOP,
    GestorDobleToque,
)


class PruebasGestorDobleToque(unittest.TestCase):
    def setUp(self):
        self.gestor = GestorDobleToque(
            ventana_doble_ms=300,
            toque_max_ms=220,
        )

    def test_hold_normal_inicia_y_detiene_sin_demora(self):
        self.assertEqual(
            self.gestor.presionar(1.000),
            (INICIAR,),
        )

        self.assertEqual(
            self.gestor.soltar(1.500),
            (DETENER,),
        )

        self.assertFalse(
            self.gestor.continuo
        )

    def test_tap_simple_programa_stop_y_luego_vence(self):
        self.assertEqual(
            self.gestor.presionar(1.000),
            (INICIAR,),
        )

        self.assertEqual(
            self.gestor.soltar(1.100),
            (PROGRAMAR_STOP,),
        )

        self.assertTrue(
            self.gestor.stop_pendiente
        )

        self.assertEqual(
            self.gestor.vencer(1.399),
            (),
        )

        self.assertEqual(
            self.gestor.vencer(1.400),
            (DETENER,),
        )

        self.assertFalse(
            self.gestor.stop_pendiente
        )

    def test_doble_tap_activa_continuo_sin_segundo_start(self):
        self.assertEqual(
            self.gestor.presionar(1.000),
            (INICIAR,),
        )
        self.assertEqual(
            self.gestor.soltar(1.080),
            (PROGRAMAR_STOP,),
        )

        self.assertEqual(
            self.gestor.presionar(1.200),
            (
                CANCELAR_STOP,
                CONTINUO_ON,
            ),
        )

        self.assertTrue(
            self.gestor.continuo
        )

        # Release del segundo tap: mantiene la misma sesión.
        self.assertEqual(
            self.gestor.soltar(1.280),
            (),
        )

        self.assertTrue(
            self.gestor.continuo
        )

    def test_doble_tap_en_continuo_lo_desactiva(self):
        # Entrar en continuo.
        self.gestor.presionar(1.000)
        self.gestor.soltar(1.080)
        self.gestor.presionar(1.200)
        self.gestor.soltar(1.280)

        self.assertTrue(
            self.gestor.continuo
        )

        # Primer tap de salida.
        self.assertEqual(
            self.gestor.presionar(2.000),
            (),
        )
        self.assertEqual(
            self.gestor.soltar(2.080),
            (),
        )

        self.assertTrue(
            self.gestor.continuo
        )

        # Segundo tap de salida.
        self.assertEqual(
            self.gestor.presionar(2.200),
            (),
        )
        self.assertEqual(
            self.gestor.soltar(2.280),
            (
                CONTINUO_OFF,
                DETENER,
            ),
        )

        self.assertFalse(
            self.gestor.continuo
        )

    def test_tap_de_salida_vencido_no_apaga_continuo(self):
        self.gestor.presionar(1.000)
        self.gestor.soltar(1.080)
        self.gestor.presionar(1.200)
        self.gestor.soltar(1.280)

        # Primer tap.
        self.gestor.presionar(2.000)
        self.gestor.soltar(2.080)

        # Demasiado tarde: pasa a ser un nuevo primer tap.
        self.assertEqual(
            self.gestor.presionar(2.500),
            (),
        )
        self.assertEqual(
            self.gestor.soltar(2.580),
            (),
        )

        self.assertTrue(
            self.gestor.continuo
        )

    def test_hold_en_continuo_no_lo_desactiva(self):
        self.gestor.presionar(1.000)
        self.gestor.soltar(1.080)
        self.gestor.presionar(1.200)
        self.gestor.soltar(1.280)

        self.assertEqual(
            self.gestor.presionar(2.000),
            (),
        )
        self.assertEqual(
            self.gestor.soltar(2.500),
            (),
        )

        self.assertTrue(
            self.gestor.continuo
        )

    def test_reiniciar_limpia_todo_estado(self):
        self.gestor.presionar(1.000)
        self.gestor.soltar(1.080)

        self.assertTrue(
            self.gestor.stop_pendiente
        )

        self.gestor.reiniciar()

        self.assertFalse(
            self.gestor.continuo
        )
        self.assertFalse(
            self.gestor.stop_pendiente
        )
        self.assertEqual(
            self.gestor.vencer(10.0),
            (),
        )


if __name__ == "__main__":
    unittest.main()
