import unittest
from dataclasses import FrozenInstanceError

from parlar.estado_ui import EstadoInterfaz, EstadoUI
from parlar.indicador import BucleSinUI


class PruebasEstadoInterfaz(unittest.TestCase):
    def test_estado_inicial(self):
        estado = EstadoInterfaz()

        self.assertEqual(
            estado.snapshot(),
            EstadoUI(
                operativo="idle",
                continuo_activo=False,
            ),
        )

    def test_operativo_preserva_continuo(self):
        estado = EstadoInterfaz()

        estado.fijar_continuo(True)
        estado.fijar_operativo("recording")

        self.assertEqual(
            estado.snapshot(),
            EstadoUI(
                operativo="recording",
                continuo_activo=True,
            ),
        )

    def test_continuo_preserva_operativo(self):
        estado = EstadoInterfaz()

        estado.fijar_operativo("transcribing")
        estado.fijar_continuo(True)

        self.assertEqual(
            estado.snapshot(),
            EstadoUI(
                operativo="transcribing",
                continuo_activo=True,
            ),
        )

    def test_snapshot_es_inmutable(self):
        estado = EstadoInterfaz().snapshot()

        with self.assertRaises(FrozenInstanceError):
            estado.operativo = "recording"


class PruebasBucleSinUI(unittest.TestCase):
    def test_expone_el_mismo_contrato_de_estado(self):
        ui = BucleSinUI()

        ui.fijar_estado("error")
        ui.fijar_continuo(True)

        self.assertEqual(
            ui.snapshot_ui(),
            EstadoUI(
                operativo="error",
                continuo_activo=True,
            ),
        )


if __name__ == "__main__":
    unittest.main()
