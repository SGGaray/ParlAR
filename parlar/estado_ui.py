"""Estado de presentación independiente del toolkit gráfico.

Este módulo no conoce Tk ni el lifecycle de App. Mantiene únicamente trabajo
de usuario todavía vigente: captura, entregas pendientes y errores relevantes.
"""

from dataclasses import dataclass, field, replace
import math
import threading
import time

CANTIDAD_BARRAS_VISUALES = 17
ENVOLVENTE_REPOSO = (0.0,) * CANTIDAD_BARRAS_VISUALES
DURACION_ERROR_VISUAL_S = 2.0


def _normalizar_valor(valor) -> float:
    try:
        normalizado = float(valor)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(normalizado):
        return 0.0
    return min(1.0, max(0.0, normalizado))


def _normalizar_envolvente(valores) -> tuple[float, ...]:
    try:
        envolvente = tuple(valores)
    except (TypeError, ValueError):
        return ENVOLVENTE_REPOSO
    if len(envolvente) != CANTIDAD_BARRAS_VISUALES:
        return ENVOLVENTE_REPOSO
    return tuple(_normalizar_valor(valor) for valor in envolvente)


@dataclass(frozen=True, slots=True)
class EstadoUI:
    """Snapshot inmutable con precedencia visual derivada, no inferida."""

    generacion: int | None = None
    generacion_valida: bool = False
    capturando: bool = False
    trabajos_pendientes: int = 0
    error_visual: bool = False
    continuo_activo: bool = False
    presionado: bool = False
    nivel_visual: float = 0.0
    envolvente_visual: tuple[float, ...] = ENVOLVENTE_REPOSO
    operativo: str = field(init=False)

    def __post_init__(self):
        pendientes = max(0, int(self.trabajos_pendientes))
        object.__setattr__(self, "trabajos_pendientes", pendientes)
        if self.capturando and self.generacion_valida:
            operativo = "recording"
        elif self.error_visual:
            operativo = "error"
        elif pendientes and self.generacion_valida:
            operativo = "transcribing"
        else:
            operativo = "idle"
        object.__setattr__(self, "operativo", operativo)

    @property
    def trabajo_pendiente(self) -> bool:
        return self.trabajos_pendientes > 0

    @property
    def visible(self) -> bool:
        return self.operativo != "idle"


class EstadoInterfaz:
    """Almacén thread-safe y protegido por generación para el overlay."""

    def __init__(self, *, reloj=None,
                 duracion_error_s: float = DURACION_ERROR_VISUAL_S):
        self._lock = threading.Lock()
        self._estado = EstadoUI()
        self._reloj = reloj or time.monotonic
        self._duracion_error_s = max(0.0, float(duracion_error_s))
        self._error_hasta: float | None = None
        self._cerrado = False

    def _expirar_error_locked(self) -> None:
        if (self._estado.error_visual
                and self._error_hasta is not None
                and self._reloj() >= self._error_hasta):
            self._estado = replace(self._estado, error_visual=False)
            self._error_hasta = None

    def snapshot(self) -> EstadoUI:
        with self._lock:
            self._expirar_error_locked()
            return self._estado

    def preparar_generacion(self, generacion: int) -> bool:
        """Reemplaza todo trabajo visual anterior sin mostrar captura aún."""
        with self._lock:
            if self._cerrado:
                return False
            actual = self._estado.generacion
            if actual is not None and generacion < actual:
                return False
            self._error_hasta = None
            self._estado = EstadoUI(
                generacion=generacion,
                generacion_valida=True,
                continuo_activo=self._estado.continuo_activo,
                presionado=self._estado.presionado,
            )
            return True

    def iniciar_captura(self, generacion: int) -> bool:
        with self._lock:
            if (self._cerrado
                    or self._estado.generacion != generacion
                    or not self._estado.generacion_valida):
                return False
            self._estado = replace(self._estado, capturando=True)
            return True

    def cerrar_captura(
            self, generacion: int, *, trabajo_pendiente: bool = False) -> bool:
        """Cierra listening y, si corresponde, instala la barrera de STOP."""
        with self._lock:
            if (self._cerrado
                    or self._estado.generacion != generacion
                    or not self._estado.generacion_valida):
                return False
            pendientes = self._estado.trabajos_pendientes
            if trabajo_pendiente:
                pendientes += 1
            self._estado = replace(
                self._estado,
                capturando=False,
                trabajos_pendientes=pendientes,
            )
            return True

    def iniciar_trabajo(self, generacion: int) -> bool:
        with self._lock:
            if (self._cerrado
                    or self._estado.generacion != generacion
                    or not self._estado.generacion_valida):
                return False
            self._estado = replace(
                self._estado,
                trabajos_pendientes=self._estado.trabajos_pendientes + 1,
            )
            return True

    def finalizar_trabajo(self, generacion: int) -> bool:
        """Libera un trabajo vigente; llamadas stale o extra son inertes."""
        with self._lock:
            if (self._cerrado
                    or self._estado.generacion != generacion
                    or not self._estado.generacion_valida
                    or self._estado.trabajos_pendientes <= 0):
                return False
            self._estado = replace(
                self._estado,
                trabajos_pendientes=self._estado.trabajos_pendientes - 1,
            )
            return True

    def invalidar_generacion(self, generacion: int) -> bool:
        """Oculta de inmediato trabajo que ya no puede producir salida."""
        with self._lock:
            if self._cerrado or self._estado.generacion != generacion:
                return False
            self._error_hasta = None
            self._estado = replace(
                self._estado,
                generacion_valida=False,
                capturando=False,
                trabajos_pendientes=0,
                error_visual=False,
                nivel_visual=0.0,
                envolvente_visual=ENVOLVENTE_REPOSO,
            )
            return True

    def completar_generacion(
            self, generacion: int, *, con_error: bool = False) -> bool:
        """Publica el terminal sólo si ninguna generación lo reemplazó."""
        with self._lock:
            if self._cerrado or self._estado.generacion != generacion:
                return False
            self._estado = replace(
                self._estado,
                generacion_valida=False,
                capturando=False,
                trabajos_pendientes=0,
                error_visual=bool(con_error),
                nivel_visual=0.0,
                envolvente_visual=ENVOLVENTE_REPOSO,
            )
            self._error_hasta = (
                self._reloj() + self._duracion_error_s
                if con_error
                else None
            )
            return True

    def fijar_operativo(self, operativo: str) -> None:
        """Compatibilidad para consumidores antiguos; App usa la API causal."""
        with self._lock:
            if self._cerrado:
                return
            generacion = self._estado.generacion
            if generacion is None:
                generacion = 0
            if operativo == "recording":
                cambios = dict(
                    generacion=generacion,
                    generacion_valida=True,
                    capturando=True,
                    trabajos_pendientes=0,
                    error_visual=False,
                )
                self._error_hasta = None
            elif operativo == "transcribing":
                cambios = dict(
                    generacion=generacion,
                    generacion_valida=True,
                    capturando=False,
                    trabajos_pendientes=max(
                        1, self._estado.trabajos_pendientes),
                    error_visual=False,
                )
                self._error_hasta = None
            elif operativo == "error":
                cambios = dict(
                    generacion=generacion,
                    generacion_valida=False,
                    capturando=False,
                    trabajos_pendientes=0,
                    error_visual=True,
                )
                self._error_hasta = self._reloj() + self._duracion_error_s
            else:
                cambios = dict(
                    generacion_valida=False,
                    capturando=False,
                    trabajos_pendientes=0,
                    error_visual=False,
                )
                self._error_hasta = None
            self._estado = replace(self._estado, **cambios)

    def fijar_continuo(self, activo: bool) -> None:
        with self._lock:
            if not self._cerrado:
                self._estado = replace(
                    self._estado, continuo_activo=bool(activo))

    def fijar_presionado(self, activo: bool) -> None:
        with self._lock:
            if not self._cerrado:
                self._estado = replace(
                    self._estado, presionado=bool(activo))

    def fijar_nivel_visual(self, nivel: float) -> None:
        """Publica sólo amplitud normalizada; nunca conserva muestras."""
        normalizado = _normalizar_valor(nivel)
        with self._lock:
            if not self._cerrado:
                self._estado = replace(
                    self._estado, nivel_visual=normalizado)

    def fijar_descriptor_visual(self, nivel, envolvente) -> None:
        """Reemplaza atómicamente el último descriptor; nunca acumula PCM."""
        normalizado = _normalizar_valor(nivel)
        forma = _normalizar_envolvente(envolvente)
        with self._lock:
            if not self._cerrado:
                self._estado = replace(
                    self._estado,
                    nivel_visual=normalizado,
                    envolvente_visual=forma,
                )

    def cerrar(self) -> None:
        """Hace terminal el almacén e invalida cualquier publicación tardía."""
        with self._lock:
            self._cerrado = True
            self._error_hasta = None
            self._estado = EstadoUI(generacion=self._estado.generacion)
