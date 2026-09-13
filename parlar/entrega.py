"""Resultados mínimos y explícitos de los sinks de salida."""

from dataclasses import dataclass
from enum import Enum


class EstadoEntrega(str, Enum):
    INSERTED = "inserted"
    COPIED = "copied"
    MIRRORED = "mirrored"
    PERSISTED = "persisted"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ResultadoSink:
    estado: EstadoEntrega

    def __bool__(self):
        return self.estado not in (EstadoEntrega.FAILED, EstadoEntrega.SKIPPED)


@dataclass(frozen=True)
class ResultadoDistribucion:
    inyector: EstadoEntrega
    guionar: EstadoEntrega
    sesion: EstadoEntrega

    @classmethod
    def omitido(cls):
        return cls(*(EstadoEntrega.SKIPPED,) * 3)
