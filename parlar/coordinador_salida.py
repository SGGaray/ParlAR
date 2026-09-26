"""Frontera síncrona para coordinar los efectos de salida de ParlAR.

No posee lifecycle ni generación. ``App`` valida ownership inmediatamente
antes de llamar a esta clase y conserva ``_salida_lock`` durante el efecto.
"""

import sys

from .cliente_guionar import crear_cliente
from .entrega import EstadoEntrega, ResultadoDistribucion, ResultadoSink
from .inyector_salida import Inyector
from .sesion import crear_salida_sesion


class CoordinadorSalida:
    def __init__(self, inyector, guionar, sesion):
        self.inyector = inyector
        self.guionar = guionar
        self.sesion = sesion
        self._salidas = (inyector, guionar, sesion)
        self.ultima_entrega = ResultadoDistribucion.omitido()
        self.necesita_espacio = False

    def iniciar_generacion(self, *, limpiar_parcial: bool):
        if limpiar_parcial:
            self.guionar.enviar_parcial("")
        self.inyector.reiniciar_registro()
        self.necesita_espacio = False

    def cancelar_generacion(self):
        self.guionar.enviar_parcial("")
        self.cancelar_unidad()

    def iniciar_unidad(self, generacion: int):
        iniciar = getattr(self.inyector, "iniciar_unidad", None)
        if iniciar:
            iniciar(generacion)

    def finalizar_unidad(self):
        finalizar = getattr(self.inyector, "finalizar_unidad", None)
        if finalizar:
            finalizar()

    def cancelar_unidad(self):
        cancelar = getattr(self.inyector, "cancelar_unidad", None)
        if cancelar:
            cancelar()

    def enviar_parcial(self, texto: str):
        return self.guionar.enviar_parcial(texto)

    def evento_vad(self, hablando: bool):
        for salida in self._salidas:
            salida.evento_vad(hablando)

    def omitir(self) -> ResultadoDistribucion:
        self.ultima_entrega = ResultadoDistribucion.omitido()
        return self.ultima_entrega

    def entregar_fragmento(self, texto: str) -> ResultadoDistribucion:
        return self._distribuir(texto, texto, registrar=False)

    def entregar_texto(self, texto: str) -> ResultadoDistribucion:
        agregar_espacio = (
            self.necesita_espacio
            and self._admite_separador_prosa(texto)
        )
        texto_inyector = (" " + texto) if agregar_espacio else texto
        resultado = self._distribuir(texto_inyector, texto)
        if resultado.inyector == EstadoEntrega.INSERTED:
            self.necesita_espacio = True
        return resultado

    def nueva_linea(self, cantidad: int):
        resultado = self.inyector.nueva_linea(cantidad)
        self.necesita_espacio = False
        return resultado

    def borrar_ultima_oracion(self):
        return self.inyector.borrar_ultima_oracion()

    def presionar_enter(self):
        resultado = self.inyector.presionar_enter()
        self.necesita_espacio = False
        return resultado

    def cerrar(self):
        for nombre, salida in zip(
                ("inyector", "guionar", "sesion"), self._salidas):
            try:
                salida.cerrar()
            except BaseException as exc:
                # El consumidor itera de forma síncrona: reporta este fallo
                # antes de avanzar al siguiente cierre, como hacía App.
                yield nombre, exc

    @staticmethod
    def _resultado_inyector(valor) -> EstadoEntrega:
        if isinstance(valor, ResultadoSink):
            return valor.estado
        return EstadoEntrega.INSERTED if valor else EstadoEntrega.FAILED

    @staticmethod
    def _intentar_sink(funcion, exito: EstadoEntrega) -> EstadoEntrega:
        try:
            resultado = funcion()
            if isinstance(resultado, ResultadoSink):
                return resultado.estado
            return exito if resultado is not False else EstadoEntrega.FAILED
        except Exception as exc:
            print(f"[app] salida {exito.value} falló: {type(exc).__name__}",
                  file=sys.stderr)
            return EstadoEntrega.FAILED

    def _distribuir(self, texto_inyector: str, texto_confirmado: str,
                    *, registrar: bool = True) -> ResultadoDistribucion:
        try:
            inyector = self._resultado_inyector(
                self.inyector.escribir_texto(
                    texto_inyector, registrar=registrar))
        except Exception as exc:
            print(f"[app] salida inserted falló: {type(exc).__name__}",
                  file=sys.stderr)
            inyector = EstadoEntrega.FAILED
        guionar = (
            EstadoEntrega.SKIPPED
            if getattr(self.guionar, "es_nulo", False)
            else self._intentar_sink(
                lambda: self.guionar.escribir_texto(texto_confirmado),
                EstadoEntrega.MIRRORED,
            )
        )
        sesion = (
            EstadoEntrega.SKIPPED
            if getattr(self.sesion, "es_nulo", False)
            else self._intentar_sink(
                lambda: self.sesion.escribir_texto(texto_confirmado),
                EstadoEntrega.PERSISTED,
            )
        )
        self.ultima_entrega = ResultadoDistribucion(inyector, guionar, sesion)
        return self.ultima_entrega

    @staticmethod
    def _admite_separador_prosa(texto: str) -> bool:
        return not any(marca in texto for marca in ("\n", "\r", "\t"))


def crear_coordinador_salida(cfg, *, inyector=None, guionar=None, sesion=None):
    """Compone los sinks concretos en una sola frontera para ``App``."""
    return CoordinadorSalida(
        inyector or Inyector(
            cfg.injector,
            cfg.type_delay_ms,
            cfg.notify,
            cfg.comando_enviar,
        ),
        guionar or crear_cliente(cfg.guionar, cfg.guionar_socket),
        sesion or crear_salida_sesion(cfg.guardar_sesion),
    )
