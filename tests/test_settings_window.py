"""Lógica de Settings testeable sin crear una ventana ni requerir display."""

import dataclasses
import unittest
from unittest import mock

from parlar.config import Config, ErrorConfiguracion
from parlar.settings_backend import (
    ResultadoPersistencia,
    snapshot_configuracion,
)
from parlar.settings_window import (
    ControlSettings,
    ValoresFormulario,
    mensaje_persistencia,
    parsear_context_terms,
    snapshot_desde_valores,
    valores_desde_snapshot,
)


class PruebasLogicaSettings(unittest.TestCase):
    def setUp(self):
        self.base = Config(
            model_size="base",
            device="cpu",
            compute_type="int8",
            language="es",
            context_terms=["COBIT", "Acme Corporation"],
            mode="streaming",
            rewrite_mode="formal",
            injector="clipboard",
            hotkey_toggle="<ctrl>+<alt>+d",
            overlay=False,
            guionar=True,
            guionar_socket="/tmp/guionar.sock",
            guardar_sesion=True,
        )
        self.snapshot = snapshot_configuracion(self.base)

    def test_snapshot_se_mapea_a_valores_de_formulario(self):
        valores = valores_desde_snapshot(self.snapshot)

        self.assertIsInstance(valores, ValoresFormulario)
        self.assertEqual(valores.model_size, "base")
        self.assertEqual(valores.context_terms, "COBIT\nAcme Corporation")
        self.assertFalse(valores.overlay)
        self.assertTrue(valores.guionar)

    def test_formulario_se_mapea_a_snapshot_sin_mutar_inicial(self):
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            language="en",
            context_terms="OWASP\nParlAR",
            overlay=True,
        )

        candidata = snapshot_desde_valores(self.snapshot, valores)

        self.assertEqual(candidata.language, "en")
        self.assertEqual(candidata.context_terms, ("OWASP", "ParlAR"))
        self.assertTrue(candidata.overlay)
        self.assertEqual(self.snapshot.language, "es")

    def test_context_terms_usa_un_termino_por_linea(self):
        self.assertEqual(
            parsear_context_terms("  COBIT  \n\nAcme   Corporation\r\n OWASP "),
            ("COBIT", "Acme   Corporation", "OWASP"),
        )
        self.assertEqual(parsear_context_terms("\n  \n"), ())

    def test_detecta_cambio_y_sin_cambio(self):
        control = ControlSettings(self.base, self.snapshot)
        iguales = valores_desde_snapshot(self.snapshot)
        cambiados = dataclasses.replace(iguales, mode="utterance")

        self.assertFalse(control.esta_sucio(iguales))
        self.assertTrue(control.esta_sucio(cambiados))

    def test_validacion_invalida_no_persiste(self):
        persistir = mock.Mock()
        control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=persistir,
        )
        invalidos = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            device="tpu",
        )

        with self.assertRaisesRegex(ErrorConfiguracion, "device"):
            control.guardar(invalidos)

        persistir.assert_not_called()

    def test_persistencia_valida_usa_backend_y_actualiza_inicial(self):
        persistir = mock.Mock()
        control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=persistir,
        )
        valores = dataclasses.replace(
            valores_desde_snapshot(self.snapshot),
            mode="utterance",
        )
        candidata = snapshot_desde_valores(self.snapshot, valores)
        persistir.return_value = ResultadoPersistencia(candidata, True)

        resultado = control.guardar(valores)

        persistir.assert_called_once_with(self.base, candidata)
        self.assertIs(resultado, persistir.return_value)
        self.assertFalse(control.esta_sucio(valores))

    def test_resultado_refleja_requires_restart(self):
        con_reinicio = ResultadoPersistencia(self.snapshot, True)
        sin_reinicio = ResultadoPersistencia(self.snapshot, False)

        self.assertEqual(
            mensaje_persistencia(con_reinicio),
            "Configuración guardada. Los cambios se aplicarán al reiniciar ParlAR.",
        )
        self.assertEqual(
            mensaje_persistencia(sin_reinicio),
            "Configuración guardada. No es necesario reiniciar ParlAR.",
        )

    def test_cancelar_cierra_sin_persistir(self):
        persistir = mock.Mock()
        cerrar = mock.Mock()
        control = ControlSettings(
            self.base,
            self.snapshot,
            persistir=persistir,
        )

        control.cancelar(cerrar)

        cerrar.assert_called_once_with()
        persistir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
