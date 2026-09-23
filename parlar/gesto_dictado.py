"""Máquina de estados para hold-to-talk + doble toque continuo.

No conoce pynput, audio ni App. Recibe tiempos monotónicos y devuelve
acciones que la capa de atajos debe ejecutar.
"""

from __future__ import annotations


INICIAR = "iniciar"
DETENER = "detener"
PROGRAMAR_STOP = "programar_stop"
CANCELAR_STOP = "cancelar_stop"
CONTINUO_ON = "continuo_on"
CONTINUO_OFF = "continuo_off"

# Margen exclusivo para errores de representación de float.
# No altera perceptiblemente las ventanas del gesto.
_EPSILON_TIEMPO_S = 1e-9


class GestorDobleToque:
    def __init__(
        self,
        *,
        ventana_doble_ms: int = 300,
        toque_max_ms: int = 220,
    ):
        if type(ventana_doble_ms) is not int \
                or ventana_doble_ms <= 0:
            raise ValueError(
                "ventana_doble_ms debe ser entero positivo"
            )

        if type(toque_max_ms) is not int \
                or toque_max_ms <= 0:
            raise ValueError(
                "toque_max_ms debe ser entero positivo"
            )

        if toque_max_ms >= ventana_doble_ms:
            raise ValueError(
                "toque_max_ms debe ser menor que "
                "ventana_doble_ms"
            )

        self.ventana_doble_s = (
            ventana_doble_ms / 1000.0
        )
        self.toque_max_s = (
            toque_max_ms / 1000.0
        )

        self.continuo = False

        self._presionado_desde: float | None = None

        # Primer tap corto en modo normal. Mientras exista,
        # mantenemos abierta la sesión por si llega el segundo tap.
        self._stop_pendiente_hasta: float | None = None

        # El release que completa el segundo tap de entrada no debe
        # empezar inmediatamente el gesto de salida.
        self._ignorar_release_entrada = False

        # Primer tap corto mientras ya estamos en continuo.
        self._tap_salida_en: float | None = None

    @property
    def stop_pendiente(self) -> bool:
        return self._stop_pendiente_hasta is not None

    @property
    def stop_pendiente_hasta(self) -> float | None:
        return self._stop_pendiente_hasta

    def reiniciar(self) -> None:
        self.continuo = False
        self._presionado_desde = None
        self._stop_pendiente_hasta = None
        self._ignorar_release_entrada = False
        self._tap_salida_en = None

    def vencer(
        self,
        ahora: float,
    ) -> tuple[str, ...]:
        """Materializa un STOP corto cuyo doble-toque no llegó."""
        if (
            self._stop_pendiente_hasta is not None
            and ahora + _EPSILON_TIEMPO_S >= self._stop_pendiente_hasta
        ):
            self._stop_pendiente_hasta = None
            return (DETENER,)

        return ()

    def presionar(
        self,
        ahora: float,
    ) -> tuple[str, ...]:
        acciones = list(
            self.vencer(ahora)
        )

        # Repetición de tecla: no genera otro flanco lógico.
        if self._presionado_desde is not None:
            return tuple(acciones)

        self._presionado_desde = ahora

        if self.continuo:
            return tuple(acciones)

        if self._stop_pendiente_hasta is not None:
            # El segundo press llegó antes de vencer la ventana.
            self._stop_pendiente_hasta = None
            self.continuo = True
            self._ignorar_release_entrada = True

            acciones.extend(
                (
                    CANCELAR_STOP,
                    CONTINUO_ON,
                )
            )
            return tuple(acciones)

        acciones.append(INICIAR)
        return tuple(acciones)

    def soltar(
        self,
        ahora: float,
    ) -> tuple[str, ...]:
        acciones = list(
            self.vencer(ahora)
        )

        if self._presionado_desde is None:
            return tuple(acciones)

        duracion = max(
            0.0,
            ahora - self._presionado_desde,
        )
        self._presionado_desde = None

        if self.continuo:
            if self._ignorar_release_entrada:
                self._ignorar_release_entrada = False
                return tuple(acciones)

            if duracion > self.toque_max_s:
                # Un hold accidental en continuo no lo apaga.
                self._tap_salida_en = None
                return tuple(acciones)

            if (
                self._tap_salida_en is not None
                and ahora - self._tap_salida_en
                <= self.ventana_doble_s + _EPSILON_TIEMPO_S
            ):
                self._tap_salida_en = None
                self.continuo = False
                acciones.extend(
                    (
                        CONTINUO_OFF,
                        DETENER,
                    )
                )
                return tuple(acciones)

            self._tap_salida_en = ahora
            return tuple(acciones)

        if duracion <= self.toque_max_s:
            self._stop_pendiente_hasta = (
                ahora + self.ventana_doble_s
            )
            acciones.append(PROGRAMAR_STOP)
            return tuple(acciones)

        acciones.append(DETENER)
        return tuple(acciones)
