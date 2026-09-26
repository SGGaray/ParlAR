"""Contrato de la frontera síncrona de efectos de salida."""

import unittest

from parlar.coordinador_salida import CoordinadorSalida
from parlar.entrega import EstadoEntrega, ResultadoSink


class InyectorFalso:
    es_nulo = False

    def __init__(self, *, estado=EstadoEntrega.INSERTED, error=False,
                 error_cierre=False):
        self.estado = estado
        self.error = error
        self.error_cierre = error_cierre
        self.textos = []
        self.unidades = []
        self.vad = []
        self.reinicios = 0
        self.cerrado = False

    def escribir_texto(self, texto, registrar=True):
        self.textos.append((texto, registrar))
        if self.error:
            raise RuntimeError("inyector")
        return ResultadoSink(self.estado)

    def reiniciar_registro(self):
        self.reinicios += 1

    def iniciar_unidad(self, generacion):
        self.unidades.append(("iniciar", generacion))

    def finalizar_unidad(self):
        self.unidades.append(("finalizar", None))

    def cancelar_unidad(self):
        self.unidades.append(("cancelar", None))

    def nueva_linea(self, cantidad):
        self.unidades.append(("linea", cantidad))
        return True

    def presionar_enter(self):
        self.unidades.append(("enter", None))
        return True

    def borrar_ultima_oracion(self):
        self.unidades.append(("undo", None))
        return True

    def evento_vad(self, hablando):
        self.vad.append(hablando)

    def cerrar(self):
        self.cerrado = True
        if self.error_cierre:
            raise RuntimeError("cierre inyector")


class SinkFalso:
    es_nulo = False

    def __init__(self, *, error=False, error_cierre=False):
        self.error = error
        self.error_cierre = error_cierre
        self.textos = []
        self.parciales = []
        self.vad = []
        self.cerrado = False

    def escribir_texto(self, texto):
        self.textos.append(texto)
        if self.error:
            raise OSError("sink")
        return True

    def enviar_parcial(self, texto):
        self.parciales.append(texto)

    def evento_vad(self, hablando):
        self.vad.append(hablando)

    def cerrar(self):
        self.cerrado = True
        if self.error_cierre:
            raise OSError("cierre sink")


class PruebasCoordinadorSalida(unittest.TestCase):
    def crear(self, inyector=None, guionar=None, sesion=None):
        return CoordinadorSalida(
            inyector or InyectorFalso(),
            guionar or SinkFalso(),
            sesion or SinkFalso(),
        )

    def test_entrega_normal_y_espaciado_sin_objeto_app(self):
        coordinador = self.crear()

        primero = coordinador.entregar_texto("hola")
        segundo = coordinador.entregar_texto("mundo")

        self.assertEqual(primero.inyector, EstadoEntrega.INSERTED)
        self.assertEqual(segundo.guionar, EstadoEntrega.MIRRORED)
        self.assertEqual(coordinador.inyector.textos, [
            ("hola", True), (" mundo", True),
        ])
        self.assertEqual(coordinador.guionar.textos, ["hola", "mundo"])
        self.assertEqual(coordinador.sesion.textos, ["hola", "mundo"])
        for argumentos in coordinador.inyector.textos:
            self.assertTrue(all(
                isinstance(valor, (str, bool)) for valor in argumentos))

    def test_fallos_de_sinks_siguen_siendo_independientes(self):
        casos = (
            (InyectorFalso(error=True), SinkFalso(), SinkFalso(),
             (EstadoEntrega.FAILED, EstadoEntrega.MIRRORED,
              EstadoEntrega.PERSISTED)),
            (InyectorFalso(), SinkFalso(error=True), SinkFalso(),
             (EstadoEntrega.INSERTED, EstadoEntrega.FAILED,
              EstadoEntrega.PERSISTED)),
            (InyectorFalso(), SinkFalso(), SinkFalso(error=True),
             (EstadoEntrega.INSERTED, EstadoEntrega.MIRRORED,
              EstadoEntrega.FAILED)),
        )
        for inyector, guionar, sesion, esperado in casos:
            with self.subTest(esperado=esperado):
                resultado = self.crear(
                    inyector, guionar, sesion).entregar_fragmento("texto")
                self.assertEqual(
                    (resultado.inyector, resultado.guionar, resultado.sesion),
                    esperado,
                )
                self.assertEqual(len(inyector.textos), 1)
                self.assertEqual(guionar.textos, ["texto"])
                self.assertEqual(sesion.textos, ["texto"])

    def test_parciales_vad_y_unidad_conservan_caminos_separados(self):
        coordinador = self.crear()

        coordinador.iniciar_generacion(limpiar_parcial=True)
        coordinador.iniciar_unidad(7)
        coordinador.enviar_parcial("hipótesis")
        coordinador.evento_vad(True)
        coordinador.cancelar_unidad()
        coordinador.entregar_fragmento("confirmado")

        self.assertEqual(coordinador.guionar.parciales, ["", "hipótesis"])
        self.assertEqual(coordinador.guionar.textos, ["confirmado"])
        self.assertEqual(coordinador.inyector.textos, [
            ("confirmado", False),
        ])
        self.assertEqual(coordinador.inyector.unidades, [
            ("iniciar", 7), ("cancelar", None),
        ])
        self.assertEqual(coordinador.inyector.vad, [True])
        self.assertEqual(coordinador.guionar.vad, [True])
        self.assertEqual(coordinador.sesion.vad, [True])

    def test_cierre_intenta_todos_los_sinks_y_reporta_por_nombre(self):
        inyector = InyectorFalso(error_cierre=True)
        guionar = SinkFalso(error_cierre=True)
        sesion = SinkFalso()
        coordinador = self.crear(inyector, guionar, sesion)

        errores = list(coordinador.cerrar())

        self.assertEqual([nombre for nombre, _ in errores], [
            "inyector", "guionar",
        ])
        self.assertTrue(inyector.cerrado)
        self.assertTrue(guionar.cerrado)
        self.assertTrue(sesion.cerrado)


if __name__ == "__main__":
    unittest.main()
