"""Estado de presentación independiente del toolkit gráfico.

Este módulo no conoce Tk ni el lifecycle de App. Mantiene únicamente el
snapshot que una interfaz Linux puede renderizar desde el hilo principal.
"""

from dataclasses import dataclass
import threading


@dataclass(frozen=True, slots=True)
class EstadoUI:
    """Snapshot inmutable del estado visible de ParlAR."""

    operativo: str = "idle"
    continuo_activo: bool = False


class EstadoInterfaz:
    """Almacén thread-safe del estado de presentación."""

    def __init__(self):
        self._lock = threading.Lock()
        self._estado = EstadoUI()

    def snapshot(self) -> EstadoUI:
        with self._lock:
            return self._estado

    def fijar_operativo(self, operativo: str) -> None:
        with self._lock:
            actual = self._estado
            self._estado = EstadoUI(
                operativo=operativo,
                continuo_activo=actual.continuo_activo,
            )

    def fijar_continuo(self, activo: bool) -> None:
        with self._lock:
            actual = self._estado
            self._estado = EstadoUI(
                operativo=actual.operativo,
                continuo_activo=bool(activo),
            )
