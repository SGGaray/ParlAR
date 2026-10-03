"""Estado de salud y capacidad de ParlAR, independiente de la UI transitoria."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from enum import Enum


class EstadoRuntime(str, Enum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class EstadoComponente(str, Enum):
    UNAVAILABLE = "unavailable"
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    READY = "ready"
    SUSPENDED = "suspended"
    FALLBACK = "fallback"
    DEGRADED = "degraded"
    ERROR = "error"


class Severidad(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class ErrorInicializacionSTT(RuntimeError):
    """El motor de reconocimiento no pudo quedar operativo al iniciar."""


@dataclass(frozen=True, slots=True)
class ProblemaOperativo:
    codigo: str
    componente: str
    severidad: Severidad
    mensaje: str
    detalle: str | None = None


@dataclass(frozen=True, slots=True)
class EstadoOperativo:
    revision: int = 0
    runtime: EstadoRuntime = EstadoRuntime.STARTING
    stt: EstadoComponente = EstadoComponente.NOT_LOADED
    audio: EstadoComponente = EstadoComponente.UNAVAILABLE
    hotkey: EstadoComponente = EstadoComponente.UNAVAILABLE
    control: EstadoComponente = EstadoComponente.UNAVAILABLE
    output: EstadoComponente = EstadoComponente.UNAVAILABLE
    gesto_requerido: bool = True
    backend_salida: str | None = None
    dispositivo_audio: str | None = None
    problemas: tuple[ProblemaOperativo, ...] = ()

    @property
    def warnings(self) -> tuple[ProblemaOperativo, ...]:
        return tuple(
            problema for problema in self.problemas
            if problema.severidad == Severidad.WARNING
        )

    @property
    def error_bloqueante(self) -> ProblemaOperativo | None:
        return next((
            problema for problema in self.problemas
            if problema.severidad == Severidad.BLOCKING
        ), None)


_COMPONENTES = frozenset({"stt", "audio", "hotkey", "control", "output"})
_TERMINALES = frozenset({EstadoRuntime.STOPPING, EstadoRuntime.STOPPED})


def puede_dictar(estado: EstadoOperativo) -> bool:
    """True sólo si están listas todas las dependencias de dictado requeridas."""
    hotkey_ok = (
        not estado.gesto_requerido
        or estado.hotkey == EstadoComponente.READY
    )
    return (
        estado.runtime == EstadoRuntime.READY
        and estado.stt == EstadoComponente.READY
        and estado.audio in {
            EstadoComponente.READY, EstadoComponente.FALLBACK}
        and estado.control == EstadoComponente.READY
        and estado.output in {
            EstadoComponente.READY, EstadoComponente.DEGRADED}
        and hotkey_ok
        and estado.error_bloqueante is None
    )


def severidad_global(estado: EstadoOperativo) -> Severidad:
    if estado.error_bloqueante is not None:
        return Severidad.BLOCKING
    if estado.warnings:
        return Severidad.WARNING
    return Severidad.INFO


def resumen_humano(estado: EstadoOperativo) -> tuple[str, str]:
    """Devuelve título y explicación breve, sin detalles técnicos."""
    if estado.runtime == EstadoRuntime.STOPPED:
        return "No disponible", "ParlAR no está ejecutándose."
    if estado.runtime == EstadoRuntime.STARTING:
        return "Preparando ParlAR…", "ParlAR se está preparando."
    if estado.runtime in {EstadoRuntime.STOPPING, EstadoRuntime.ERROR}:
        problema = estado.error_bloqueante
        return (
            "No disponible",
            problema.mensaje if problema else "ParlAR no está disponible.",
        )
    if puede_dictar(estado):
        if estado.warnings:
            return "Requiere atención", estado.warnings[0].mensaje
        return "Listo", "ParlAR está listo para dictar."
    if estado.hotkey == EstadoComponente.SUSPENDED:
        return (
            "Requiere atención",
            "El atajo está suspendido mientras configurás una combinación.",
        )
    problema = estado.error_bloqueante
    if problema is not None:
        return "No disponible", problema.mensaje
    return "Requiere atención", "ParlAR requiere atención antes de dictar."


def serializar_estado(estado: EstadoOperativo) -> str:
    """Serializa únicamente metadatos operativos aptos para IPC local."""
    titulo, mensaje = resumen_humano(estado)
    problema = estado.error_bloqueante
    datos = {
        "schema_version": 1,
        "revision": estado.revision,
        "runtime": estado.runtime.value,
        "stt": estado.stt.value,
        "audio": estado.audio.value,
        "hotkey": estado.hotkey.value,
        "control": estado.control.value,
        "output": estado.output.value,
        "can_dictate": puede_dictar(estado),
        "severity": severidad_global(estado).value,
        "status": titulo,
        "message": mensaje,
        "output_backend": estado.backend_salida,
        "audio_device": estado.dispositivo_audio,
        "warnings": [
            {
                "code": aviso.codigo,
                "component": aviso.componente,
                "message": aviso.mensaje,
            }
            for aviso in estado.warnings
        ],
        "blocking_error": None if problema is None else {
            "code": problema.codigo,
            "component": problema.componente,
            "message": problema.mensaje,
        },
    }
    return json.dumps(datos, ensure_ascii=False, separators=(",", ":"))


class EstadoOperativoStore:
    """Publica snapshots atómicos e impide revivir un runtime en shutdown."""

    def __init__(self, *, gesto_requerido: bool = True):
        self._lock = threading.Lock()
        self._estado = EstadoOperativo(gesto_requerido=gesto_requerido)

    def snapshot(self) -> EstadoOperativo:
        with self._lock:
            return self._estado

    def actualizar_runtime(
            self, runtime: EstadoRuntime, *,
            problema: ProblemaOperativo | None = None,
            revision_esperada: int | None = None) -> bool:
        with self._lock:
            actual = self._estado
            if revision_esperada is not None \
                    and revision_esperada != actual.revision:
                return False
            if actual.runtime in _TERMINALES \
                    and runtime not in _TERMINALES:
                return False
            problemas = (
                actual.problemas
                if problema is None
                else self._reemplazar_problema(
                    actual.problemas, "runtime", problema)
            )
            self._estado = replace(
                actual, revision=actual.revision + 1,
                runtime=runtime, problemas=problemas)
            return True

    def fijar_componente(
            self, componente: str, estado: EstadoComponente, *,
            problema: ProblemaOperativo | None = None,
            revision_esperada: int | None = None,
            backend_salida: str | None = None,
            dispositivo_audio: str | None = None) -> bool:
        if componente not in _COMPONENTES:
            raise ValueError(f"componente operativo desconocido: {componente}")
        with self._lock:
            actual = self._estado
            if revision_esperada is not None \
                    and revision_esperada != actual.revision:
                return False
            if actual.runtime in _TERMINALES:
                return False
            problemas = self._reemplazar_problema(
                actual.problemas, componente, problema)
            cambios = {
                "revision": actual.revision + 1,
                componente: estado,
                "problemas": problemas,
            }
            if backend_salida is not None:
                cambios["backend_salida"] = backend_salida
            if dispositivo_audio is not None:
                cambios["dispositivo_audio"] = dispositivo_audio
            self._estado = replace(actual, **cambios)
            return True

    def reportar_problema(
            self, problema: ProblemaOperativo, *,
            revision_esperada: int | None = None) -> bool:
        """Añade/reemplaza un diagnóstico por código sin cambiar componentes."""
        with self._lock:
            actual = self._estado
            if revision_esperada is not None \
                    and revision_esperada != actual.revision:
                return False
            if actual.runtime in _TERMINALES:
                return False
            problemas = tuple(
                item for item in actual.problemas
                if item.codigo != problema.codigo
            ) + (problema,)
            self._estado = replace(
                actual, revision=actual.revision + 1,
                problemas=problemas)
            return True

    def resolver_problema(self, codigo: str) -> bool:
        with self._lock:
            actual = self._estado
            problemas = tuple(
                item for item in actual.problemas if item.codigo != codigo)
            if problemas == actual.problemas:
                return False
            self._estado = replace(
                actual, revision=actual.revision + 1,
                problemas=problemas)
            return True

    @staticmethod
    def _reemplazar_problema(problemas, componente, nuevo):
        restantes = tuple(
            problema for problema in problemas
            if problema.componente != componente
        )
        return restantes if nuevo is None else restantes + (nuevo,)
