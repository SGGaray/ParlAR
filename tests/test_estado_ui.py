import unittest
from dataclasses import FrozenInstanceError

from parlar.estado_ui import EstadoInterfaz, EstadoUI
from parlar.indicador import (
    ALTO_INDICADOR,
    ANCHO_INDICADOR,
    BucleSinUI,
    calcular_frame_waveform,
    calcular_posicion,
)


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

    def test_conserva_contrato_completo_de_presentacion(self):
        ui = BucleSinUI()

        ui.fijar_estado("recording")
        ui.fijar_continuo(True)
        ui.fijar_presionado(True)

        self.assertEqual(
            ui.snapshot_ui(),
            EstadoUI(
                operativo="recording",
                continuo_activo=True,
                presionado=True,
            ),
        )


class PruebasWaveform(unittest.TestCase):
    def test_frame_es_determinista(self):
        primero = calcular_frame_waveform("recording", fase=3)
        segundo = calcular_frame_waveform("recording", fase=3)

        self.assertEqual(primero, segundo)

    def test_barras_y_geometria_tienen_dimensiones_validas(self):
        for estado in ("idle", "recording", "transcribing", "error"):
            with self.subTest(estado=estado):
                frame = calcular_frame_waveform(estado, fase=5)
                self.assertEqual(len(frame.alturas), 7)
                self.assertTrue(all(
                    isinstance(altura, int) and 4 <= altura <= 18
                    for altura in frame.alturas
                ))

        self.assertEqual(
            calcular_posicion(1920, 1080),
            ((1920 - ANCHO_INDICADOR) // 2,
             1080 - ALTO_INDICADOR - 12),
        )

    def test_recording_y_error_tienen_frames_diferentes(self):
        recording = calcular_frame_waveform("recording", fase=2)
        error = calcular_frame_waveform("error", fase=2)

        self.assertNotEqual(recording.alturas, error.alturas)
        self.assertNotEqual(recording.color, error.color)

    def test_error_es_estatico_entre_fases(self):
        primero = calcular_frame_waveform("error", fase=0)
        despues = calcular_frame_waveform("error", fase=137)

        self.assertEqual(primero, despues)

    def test_calculo_visual_no_modifica_visibilidad(self):
        estado = EstadoInterfaz()
        estado.fijar_presionado(True)
        antes = estado.snapshot()

        calcular_frame_waveform(antes.operativo, fase=4)

        despues = estado.snapshot()
        self.assertEqual(despues, antes)
        self.assertTrue(despues.visible)



class PruebasContratoBucleSinUI(unittest.TestCase):
    def test_presionado_usa_el_mismo_modelo_de_visibilidad(self):
        ui = BucleSinUI()

        ui.fijar_presionado(True)

        snapshot = ui.snapshot_ui()
        self.assertTrue(snapshot.presionado)
        self.assertTrue(snapshot.visible)



class PruebasVisibilidadUI(unittest.TestCase):
    def test_ptt_presionado_hace_visible(self):
        estado = EstadoInterfaz()

        estado.fijar_presionado(True)

        self.assertTrue(estado.snapshot().visible)

    def test_ptt_suelto_oculta_si_no_hay_continuo(self):
        estado = EstadoInterfaz()
        estado.fijar_presionado(True)

        estado.fijar_presionado(False)

        self.assertFalse(estado.snapshot().visible)

    def test_continuo_mantiene_visible_sin_tecla_presionada(self):
        estado = EstadoInterfaz()
        estado.fijar_continuo(True)

        self.assertTrue(estado.snapshot().visible)

        estado.fijar_presionado(True)
        estado.fijar_presionado(False)

        self.assertTrue(estado.snapshot().visible)

if __name__ == "__main__":
    unittest.main()
