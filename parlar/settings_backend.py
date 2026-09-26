"""Frontera segura y sin UI para editar Settings de ParlAR.

Este módulo sólo prepara, valida y persiste configuración para el próximo
inicio. No conoce ``App`` ni aplica cambios sobre una sesión activa.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .config import Config


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """Opciones públicas que puede presentar una futura ventana Settings."""

    model_size: str
    device: str
    compute_type: str
    language: str
    context_terms: tuple[str, ...]
    mode: str
    rewrite_mode: str
    injector: str
    hotkey_toggle: str
    overlay: bool
    guionar: bool
    guionar_socket: str
    guardar_sesion: bool


@dataclass(frozen=True, slots=True)
class ResultadoPersistencia:
    """Resultado explícito de guardar Settings para un próximo inicio."""

    snapshot: SettingsSnapshot
    requires_restart: bool


@dataclass(frozen=True, slots=True)
class DispositivoEntrada:
    """Vista estable de un dispositivo de entrada publicado por PortAudio."""

    indice: int
    nombre: str
    canales_entrada: int
    predeterminado: bool


@dataclass(frozen=True, slots=True)
class SettingsCapabilities:
    """Opciones estáticas y entorno liviano útiles para Settings."""

    devices: tuple[str, ...]
    compute_types: tuple[str, ...]
    modes: tuple[str, ...]
    rewrite_modes: tuple[str, ...]
    injectors: tuple[str, ...]
    session_type: str


class ErrorDispositivosAudio(RuntimeError):
    """No fue posible obtener un inventario confiable de entradas de audio."""


def cargar_configuracion_actual() -> Config:
    """Carga la base persistida sin exponer ``Config`` a la capa de UI."""
    return Config.load()


def snapshot_configuracion(cfg: Config) -> SettingsSnapshot:
    """Copia sólo opciones públicas; nunca retiene listas mutables de ``cfg``."""
    return SettingsSnapshot(
        model_size=cfg.model_size,
        device=cfg.device,
        compute_type=cfg.compute_type,
        language=cfg.language,
        context_terms=tuple(cfg.context_terms),
        mode=cfg.mode,
        rewrite_mode=cfg.rewrite_mode,
        injector=cfg.injector,
        hotkey_toggle=cfg.hotkey_toggle,
        overlay=cfg.overlay,
        guionar=cfg.guionar,
        guionar_socket=cfg.guionar_socket,
        guardar_sesion=cfg.guardar_sesion,
    )


def construir_configuracion_candidata(
        base: Config, snapshot: SettingsSnapshot) -> Config:
    """Construye y valida una copia sin modificar la configuración activa."""
    datos = asdict(base)
    datos.update(asdict(snapshot))
    datos["context_terms"] = list(snapshot.context_terms)
    candidata = Config(**datos)
    candidata.validate()
    return candidata


def requiere_reinicio(
        actual: SettingsSnapshot, candidata: SettingsSnapshot) -> bool:
    """Settings no hace hot reload: cualquier cambio efectivo exige reinicio."""
    return actual != candidata


def persistir_configuracion(
        base: Config, snapshot: SettingsSnapshot) -> ResultadoPersistencia:
    """Valida y guarda mediante ``Config.save()``, sin tocar ``base``."""
    actual = snapshot_configuracion(base)
    candidata = construir_configuracion_candidata(base, snapshot)
    candidata.save()
    guardada = snapshot_configuracion(candidata)
    return ResultadoPersistencia(
        snapshot=guardada,
        requires_restart=requiere_reinicio(actual, guardada),
    )


def normalizar_dispositivos_entrada(
        dispositivos: Iterable[Mapping[str, Any]],
        indice_predeterminado: int | None = None,
) -> tuple[DispositivoEntrada, ...]:
    """Normaliza datos de ``query_devices`` sin acceder a hardware."""
    resultado = []
    for posicion, datos in enumerate(dispositivos):
        if not isinstance(datos, Mapping):
            raise ErrorDispositivosAudio(
                "sounddevice devolvió un dispositivo con formato inválido")
        try:
            indice = int(datos.get("index", posicion))
            canales = int(datos["max_input_channels"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ErrorDispositivosAudio(
                "sounddevice devolvió un dispositivo de entrada inválido"
            ) from exc
        if canales <= 0:
            continue
        nombre = str(datos.get("name", "")).strip() or f"Dispositivo {indice}"
        resultado.append(DispositivoEntrada(
            indice=indice,
            nombre=nombre,
            canales_entrada=canales,
            predeterminado=indice == indice_predeterminado,
        ))
    return tuple(resultado)


def _indice_entrada_predeterminado(sounddevice) -> int | None:
    try:
        valor = sounddevice.default.device

        if isinstance(valor, (str, bytes)):
            entrada = valor
        else:
            try:
                entrada = valor[0]
            except (IndexError, KeyError, TypeError):
                entrada = valor

        indice = int(entrada)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None

    return indice if indice >= 0 else None


def listar_dispositivos_entrada(
        sounddevice=None) -> tuple[DispositivoEntrada, ...]:
    """Enumera entradas con un import diferido y sin abrir streams."""
    try:
        if sounddevice is None:
            import sounddevice as sounddevice_runtime
            sounddevice = sounddevice_runtime
        dispositivos = sounddevice.query_devices()
        predeterminado = _indice_entrada_predeterminado(sounddevice)
        return normalizar_dispositivos_entrada(dispositivos, predeterminado)
    except ErrorDispositivosAudio:
        raise
    except Exception as exc:
        raise ErrorDispositivosAudio(
            "no se pudieron enumerar dispositivos de entrada con sounddevice"
        ) from exc


def obtener_capacidades(
        entorno: Mapping[str, str] | None = None) -> SettingsCapabilities:
    """Publica contratos estáticos sin sondear CUDA, audio, hotkeys ni modelos."""
    variables = os.environ if entorno is None else entorno
    sesion = variables.get("XDG_SESSION_TYPE", "").strip().lower() or "desconocida"
    return SettingsCapabilities(
        devices=tuple(sorted(Config.DISPOSITIVOS)),
        compute_types=tuple(sorted(Config.COMPUTE_TYPES)),
        modes=tuple(sorted(Config.MODOS)),
        rewrite_modes=tuple(sorted(Config.REESCRITURAS)),
        injectors=tuple(sorted(Config.INYECTORES)),
        session_type=sesion,
    )
