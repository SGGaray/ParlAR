"""Integra el gesto de dictado con callbacks reales y temporización."""

from __future__ import annotations

import threading
import time
from typing import Callable

from .gesto_dictado import (
    CANCELAR_STOP,
    CONTINUO_OFF,
    CONTINUO_ON,
    DETENER,
    INICIAR,
    PROGRAMAR_STOP,
    GestorDobleToque,
)


class ControlGestoDictado:
    """Hold-to-talk inmediato + doble toque para modo continuo."""

    def __init__(
        self,
        al_iniciar: Callable[[], object],
        al_detener: Callable[[], object],
        *,
        reloj: Callable[[], float] | None = None,
        timer_factory=None,
    ):
        self.al_iniciar = al_iniciar
        self.al_detener = al_detener

        self._reloj = (
            reloj
            if reloj is not None
            else time.monotonic
        )
        self._timer_factory = (
            timer_factory
            if timer_factory is not None
            else threading.Timer
        )

        self._gestor = GestorDobleToque(
            ventana_doble_ms=300,
            toque_max_ms=220,
        )

        # Serializa eventos del listener y del Timer.
        self._evento_lock = threading.Lock()

        # cerrar() no espera callbacks externos de App.
        self._estado_lock = threading.RLock()

        self._timer_stop = None
        self._timer_generacion = 0
        self._cerrado = False

    @property
    def continuo_activo(self) -> bool:
        with self._estado_lock:
            return self._gestor.continuo

    def _cancelar_timer_locked(self) -> None:
        # cancel() no garantiza que un callback que ya arrancó no termine
        # ejecutándose. La generación invalida lógicamente callbacks viejos.
        self._timer_generacion += 1
        timer = self._timer_stop
        self._timer_stop = None

        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _programar_timer_locked(self) -> None:
        self._cancelar_timer_locked()

        hasta = self._gestor.stop_pendiente_hasta
        if hasta is None:
            return

        demora = max(
            0.0,
            hasta - self._reloj(),
        )

        generacion_timer = self._timer_generacion

        timer = self._timer_factory(
            demora,
            lambda generacion=generacion_timer:
                self._vencer_stop_pendiente(generacion),
        )

        try:
            timer.daemon = True
        except Exception:
            pass

        self._timer_stop = timer
        timer.start()

    def _preparar_acciones_locked(
        self,
        acciones: tuple[str, ...],
    ) -> None:
        if CANCELAR_STOP in acciones:
            self._cancelar_timer_locked()

        if PROGRAMAR_STOP in acciones:
            self._programar_timer_locked()

    def _ejecutar(
        self,
        acciones: tuple[str, ...],
    ) -> None:
        for accion in acciones:
            with self._estado_lock:
                if self._cerrado:
                    return

            if accion == INICIAR:
                resultado = self.al_iniciar()

                if resultado is False:
                    with self._estado_lock:
                        self._cancelar_timer_locked()
                        self._gestor.reiniciar()

            elif accion == DETENER:
                self.al_detener()

            elif accion == CONTINUO_ON:
                print(
                    "[atajos] modo continuo activado"
                )

            elif accion == CONTINUO_OFF:
                print(
                    "[atajos] modo continuo desactivado"
                )

    def presionar(self) -> None:
        with self._evento_lock:
            with self._estado_lock:
                if self._cerrado:
                    return

                acciones = self._gestor.presionar(
                    self._reloj()
                )
                self._preparar_acciones_locked(
                    acciones
                )

            self._ejecutar(acciones)

    def soltar(self) -> None:
        with self._evento_lock:
            with self._estado_lock:
                if self._cerrado:
                    return

                acciones = self._gestor.soltar(
                    self._reloj()
                )
                self._preparar_acciones_locked(
                    acciones
                )

            self._ejecutar(acciones)

    def _vencer_stop_pendiente(
        self,
        generacion_timer: int,
    ) -> None:
        with self._evento_lock:
            with self._estado_lock:
                if (
                    self._cerrado
                    or generacion_timer != self._timer_generacion
                ):
                    return

                self._timer_stop = None

                acciones = self._gestor.vencer(
                    self._reloj()
                )

                # Un Timer puede despertarse unas fracciones antes.
                if (
                    not acciones
                    and self._gestor.stop_pendiente
                ):
                    self._programar_timer_locked()

            self._ejecutar(acciones)

    def reiniciar(self) -> None:
        """Vuelve a PTT normal sin cerrar permanentemente el controlador."""
        with self._evento_lock:
            with self._estado_lock:
                if self._cerrado:
                    return

                self._cancelar_timer_locked()
                self._gestor.reiniciar()

    def cerrar(self) -> None:
        """Cancela timers y evita callbacks posteriores al shutdown."""
        with self._estado_lock:
            if self._cerrado:
                return

            self._cerrado = True
            self._cancelar_timer_locked()
            self._gestor.reiniciar()
