"""Regresiones de recovery mixto y undo conservador."""

import unittest
import threading
from types import SimpleNamespace
from unittest import mock

from parlar.app import App
from parlar.entrega import EstadoEntrega
from parlar.inyector_salida import Inyector


class PruebasRecoveryUnidad(unittest.TestCase):
    def preparar(self, tipeos, copias=None):
        inyector = Inyector(backend="xdotool", notify=False)
        intentos = []
        portapapeles = []
        resultados_tipeo = iter(tipeos)
        resultados_copia = iter(copias or [])

        def correr(argv):
            intentos.append(argv[-1])
            return next(resultados_tipeo)

        def copiar(texto):
            portapapeles.append(texto)
            try:
                return next(resultados_copia)
            except StopIteration:
                return True

        mock.patch.object(inyector, "_correr", side_effect=correr).start()
        mock.patch.object(inyector, "_portapapeles", side_effect=copiar).start()
        self.addCleanup(mock.patch.stopall)
        inyector.iniciar_unidad(7)
        return inyector, intentos, portapapeles

    def test_todo_inserted_no_crea_recovery(self):
        inyector, intentos, copias = self.preparar([True, True, True])
        estados = [inyector.escribir_texto(t).estado
                   for t in ("uno", " dos", " tres")]
        self.assertEqual(estados, [EstadoEntrega.INSERTED] * 3)
        self.assertEqual(intentos, ["uno", " dos", " tres"])
        self.assertEqual(copias, [])
        estado = inyector.estado_recuperacion_unidad()
        self.assertEqual(estado.texto_logico, "uno dos tres")
        self.assertEqual(estado.prefijo_insertado, "uno dos tres")
        self.assertEqual(estado.texto_recuperacion, "")
        self.assertFalse(estado.degradada)
        self.assertFalse(estado.recuperacion_disponible)

    def test_todo_copied_acumula_unidad_completa(self):
        inyector, intentos, copias = self.preparar([False])
        estados = [inyector.escribir_texto(t).estado
                   for t in ("uno", " dos", " tres")]
        self.assertEqual(estados, [EstadoEntrega.COPIED] * 3)
        self.assertEqual(intentos, ["uno"])
        self.assertEqual(copias, ["uno", "uno dos", "uno dos tres"])
        estado = inyector.estado_recuperacion_unidad()
        self.assertEqual(estado.prefijo_insertado, "")
        self.assertEqual(estado.texto_recuperacion, "uno dos tres")
        self.assertTrue(estado.degradada)
        self.assertTrue(estado.recuperacion_disponible)

    def test_insert_copy_copy_conserva_sufijo_continuo(self):
        inyector, intentos, copias = self.preparar([True, False])
        estados = [inyector.escribir_texto(t).estado
                   for t in ("uno", " dos", " tres")]
        self.assertEqual(estados, [
            EstadoEntrega.INSERTED, EstadoEntrega.COPIED,
            EstadoEntrega.COPIED])
        self.assertEqual(intentos, ["uno", " dos"])
        self.assertEqual(copias, [" dos", " dos tres"])
        estado = inyector.estado_recuperacion_unidad()
        self.assertEqual(estado.texto_logico, "uno dos tres")
        self.assertEqual(estado.prefijo_insertado, "uno")
        self.assertEqual(estado.texto_recuperacion, " dos tres")

    def test_insert_copy_insert_copy_no_fabrica_huecos(self):
        inyector, intentos, copias = self.preparar([True, False, True, False])
        estados = [inyector.escribir_texto(t).estado
                   for t in ("uno", " dos", " tres", " cuatro")]
        self.assertEqual(estados, [
            EstadoEntrega.INSERTED, EstadoEntrega.COPIED,
            EstadoEntrega.COPIED, EstadoEntrega.COPIED])
        # Tras el primer fallo no hay retries ni nuevos intentos físicos.
        self.assertEqual(intentos, ["uno", " dos"])
        self.assertEqual(
            copias, [" dos", " dos tres", " dos tres cuatro"])
        self.assertNotIn(" dos cuatro", copias)
        estado = inyector.estado_recuperacion_unidad()
        self.assertEqual(estado.prefijo_insertado, "uno")
        self.assertEqual(estado.texto_recuperacion, " dos tres cuatro")
        self.assertTrue(estado.degradada)

    def test_fallo_de_copy_queda_explicito_y_puede_recuperarse_despues(self):
        inyector, intentos, copias = self.preparar([False], [False, True])
        self.assertEqual(
            inyector.escribir_texto("uno").estado, EstadoEntrega.FAILED)
        estado = inyector.estado_recuperacion_unidad()
        self.assertEqual(estado.texto_recuperacion, "uno")
        self.assertFalse(estado.recuperacion_disponible)

        self.assertEqual(
            inyector.escribir_texto(" dos").estado, EstadoEntrega.COPIED)
        self.assertEqual(intentos, ["uno"])
        self.assertEqual(copias, ["uno", "uno dos"])
        self.assertTrue(
            inyector.estado_recuperacion_unidad().recuperacion_disponible)

    def test_fronteras_resetean_estado_acotado(self):
        inyector, _, _ = self.preparar([False, False, False, False])
        inyector.escribir_texto("vieja")

        inyector.iniciar_unidad(8)
        estado = inyector.estado_recuperacion_unidad()
        self.assertEqual(estado.generacion, 8)
        self.assertEqual(estado.texto_logico, "")
        self.assertFalse(estado.degradada)
        inyector.escribir_texto("generación")

        inyector.cancelar_unidad()  # misma frontera usada ante un gap
        self.assertIsNone(inyector.estado_recuperacion_unidad().generacion)
        self.assertEqual(inyector.estado_recuperacion_unidad().texto_logico, "")

        inyector.iniciar_unidad(9)
        inyector.escribir_texto("cierre")
        inyector.finalizar_unidad()
        self.assertEqual(inyector.estado_recuperacion_unidad().texto_logico, "")

        inyector.iniciar_unidad(10)
        inyector.escribir_texto("shutdown")
        inyector.cerrar()
        self.assertEqual(inyector.estado_recuperacion_unidad().texto_logico, "")

    def test_gap_de_app_cancela_recovery_de_la_generacion(self):
        inyector, _, _ = self.preparar([False])
        inyector.escribir_texto("unidad incompleta")
        app = App.__new__(App)
        app._salida_lock = threading.RLock()
        app._puede_emit = lambda generacion: generacion == 7
        app.inyector = inyector
        app.guionar = SimpleNamespace(enviar_parcial=lambda texto: None)
        app.streaming = SimpleNamespace(reiniciar=lambda: None)
        app._modo_de = lambda generacion: "streaming"
        segmentador = SimpleNamespace(discontinuidad=lambda: [])

        app._resolver_discontinuidad(segmentador, "streaming", 7)
        estado = inyector.estado_recuperacion_unidad()
        self.assertIsNone(estado.generacion)
        self.assertEqual(estado.texto_logico, "")
        self.assertFalse(estado.degradada)


class PruebasUndoConservador(unittest.TestCase):
    def test_fallos_preservan_tope_y_exito_avanza(self):
        inyector = Inyector(backend="xdotool", notify=False)
        inyector._registro_oraciones[:] = ["A", "B"]
        intentos = []
        resultados = iter([False, False, True, True])

        def borrar(cantidad):
            intentos.append(cantidad)
            return next(resultados)

        with mock.patch.object(inyector, "retroceso", side_effect=borrar):
            self.assertFalse(inyector.borrar_ultima_oracion())
            self.assertEqual(inyector._registro_oraciones, ["A", "B"])
            self.assertFalse(inyector.borrar_ultima_oracion())
            self.assertEqual(inyector._registro_oraciones, ["A", "B"])
            self.assertTrue(inyector.borrar_ultima_oracion())
            self.assertEqual(inyector._registro_oraciones, ["A"])
            self.assertTrue(inyector.borrar_ultima_oracion())
            self.assertEqual(inyector._registro_oraciones, [])
        self.assertEqual(intentos, [1, 1, 1, 1])

    def test_unicode_usa_longitud_python_y_copied_no_entra(self):
        inyector = Inyector(backend="xdotool", notify=False)
        with mock.patch.object(inyector, "_tipear", return_value=True):
            inyector.escribir_texto("á🙂")
        with mock.patch.object(inyector, "retroceso", return_value=True) as borrar:
            self.assertTrue(inyector.borrar_ultima_oracion())
        borrar.assert_called_once_with(len("á🙂"))

        with (mock.patch.object(inyector, "_tipear", return_value=False),
              mock.patch.object(inyector, "_portapapeles", return_value=True)):
            self.assertEqual(
                inyector.escribir_texto("solo copiado").estado,
                EstadoEntrega.COPIED)
        self.assertEqual(inyector._registro_oraciones, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
