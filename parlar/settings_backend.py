"""Frontera segura y sin UI para editar Settings de ParlAR.

Este módulo sólo prepara, valida y persiste configuración para el próximo
inicio. No conoce ``App`` ni aplica cambios sobre una sesión activa.
"""

import json
import os
import secrets
import socket
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .config import Config, ErrorConfiguracion


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
    audio_input_device: str = Config.AUDIO_INPUT_DEFAULT
    overlay_position: str = "bottom-center"


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
    host_api: str = ""
    identidad: str = ""


@dataclass(frozen=True, slots=True)
class ResolucionEntrada:
    """Resultado observable de resolver una preferencia contra el inventario."""

    indice: int | None
    seleccion_solicitada: str
    identidad_resuelta: str | None
    dispositivo: DispositivoEntrada | None
    usando_fallback: bool
    motivo: str | None


@dataclass(frozen=True, slots=True)
class SettingsCapabilities:
    """Opciones estáticas y entorno liviano útiles para Settings."""

    devices: tuple[str, ...]
    compute_types: tuple[str, ...]
    modes: tuple[str, ...]
    rewrite_modes: tuple[str, ...]
    injectors: tuple[str, ...]
    session_type: str
    overlay_positions: tuple[str, ...] = tuple(
        sorted(Config.OVERLAY_POSITIONS))


@dataclass(frozen=True, slots=True)
class EstadoParlARSettings:
    """Resumen mínimo del runtime, listo para presentar sin lógica Tk."""

    categoria: str
    titulo: str
    mensaje: str
    ejecutandose: bool


class ErrorSuspensionAtajo(RuntimeError):
    """El runtime no pudo garantizar una captura aislada del hotkey."""


class SuspensionHotkeyProductivo:
    """Lease IPC acotado; el daemon se restaura aunque el cliente desaparezca."""

    VERSION = "v1"
    TTL_MS = 5000

    def __init__(self, *, enviar=None, token: str | None = None):
        if enviar is None:
            from .control import enviar_comando

            enviar = lambda comando: enviar_comando(comando, timeout=0.35)
        self._enviar = enviar
        self.token = token or secrets.token_urlsafe(24)
        self.adquirida = False

    def adquirir(self, *, runtime_activo: bool) -> bool:
        if not runtime_activo:
            return False
        respuesta = self._enviar(
            f"hotkey-suspender {self.VERSION} {self.token} {self.TTL_MS}")
        if respuesta != "OK hotkey suspendido v1":
            raise ErrorSuspensionAtajo(
                "No se pudo suspender el atajo activo para capturarlo "
                f"con seguridad ({respuesta})."
            )
        self.adquirida = True
        return True

    def renovar(self) -> bool:
        if not self.adquirida:
            return True
        try:
            respuesta = self._enviar(
                f"hotkey-renovar {self.VERSION} {self.token} {self.TTL_MS}")
        except OSError:
            self.adquirida = False
            return False
        if respuesta != "OK hotkey suspendido v1":
            self.adquirida = False
            return False
        return True

    def liberar(self) -> bool:
        if not self.adquirida:
            return True
        self.adquirida = False
        try:
            respuesta = self._enviar(
                f"hotkey-restaurar {self.VERSION} {self.token}")
        except OSError:
            # El lease vence solo; cerrar Settings no debe quedar bloqueado.
            return False
        return respuesta == "OK hotkey restaurado v1"


def representar_estado_parlar(
        datos: Mapping[str, Any] | None) -> EstadoParlARSettings:
    if datos is None:
        return EstadoParlARSettings(
            "stopped", "No está ejecutándose",
            "ParlAR no está ejecutándose.", False)
    titulo = str(datos.get("status", "No disponible"))
    mensaje = str(datos.get("message", "ParlAR no está disponible."))
    if titulo == "Listo":
        categoria = "ready"
    elif titulo == "Requiere atención":
        categoria = "attention"
    else:
        categoria = "unavailable"
        titulo = "No disponible"
    return EstadoParlARSettings(categoria, titulo, mensaje, True)


def consultar_estado_parlar(enviar=None) -> EstadoParlARSettings:
    """Consulta puntual y acotada del daemon; su ausencia no es un error."""
    if enviar is None:
        from .control import enviar_comando

        enviar = lambda comando: enviar_comando(comando, timeout=0.35)
    try:
        respuesta = enviar("estado-operativo")
        datos = json.loads(respuesta)
        if not isinstance(datos, Mapping):
            raise ValueError("la respuesta operativa no es un objeto")
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
        return representar_estado_parlar(None)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return EstadoParlARSettings(
            "unavailable", "No disponible",
            "No se pudo consultar el estado de ParlAR.", True)
    return representar_estado_parlar(datos)


class ErrorDispositivosAudio(RuntimeError):
    """No fue posible obtener un inventario confiable de entradas de audio."""


class ErrorInicializacionAudio(ErrorDispositivosAudio):
    """La entrada configurada no pudo resolverse de forma segura al iniciar."""


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
        audio_input_device=cfg.audio_input_device,
        overlay_position=cfg.overlay_position,
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
        host_apis: Mapping[int, str] | None = None,
) -> tuple[DispositivoEntrada, ...]:
    """Normaliza datos de ``query_devices`` sin acceder a hardware."""
    nombres_host = {} if host_apis is None else host_apis
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
        host_api = ""
        if datos.get("hostapi") is not None:
            try:
                indice_host = int(datos["hostapi"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ErrorDispositivosAudio(
                    "sounddevice devolvió un host API inválido"
                ) from exc
            host_api = str(nombres_host.get(indice_host, "")).strip()
        resultado.append(DispositivoEntrada(
            indice=indice,
            nombre=nombre,
            canales_entrada=canales,
            predeterminado=indice == indice_predeterminado,
            host_api=host_api,
            identidad=crear_identidad_entrada(nombre, host_api),
        ))
    return tuple(resultado)


def crear_identidad_entrada(nombre: str, host_api: str) -> str:
    """Crea la clave persistible canónica sin incorporar índices PortAudio."""
    nombre_limpio = str(nombre).strip()
    host_limpio = str(host_api).strip()
    if not nombre_limpio:
        raise ErrorDispositivosAudio(
            "el dispositivo de entrada debe tener un nombre")
    identidad = (
        Config.AUDIO_INPUT_PREFIX
        + urllib.parse.quote(host_limpio, safe="")
        + ":"
        + urllib.parse.quote(nombre_limpio, safe="")
    )
    if not Config.preferencia_entrada_valida(identidad):
        raise ErrorDispositivosAudio(
            "la identidad del dispositivo de entrada no es persistible")
    return identidad


def _normalizar_host_apis(
        host_apis: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    resultado = {}
    for posicion, datos in enumerate(host_apis):
        if not isinstance(datos, Mapping):
            raise ErrorDispositivosAudio(
                "sounddevice devolvió un host API con formato inválido")
        try:
            indice = int(datos.get("index", posicion))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ErrorDispositivosAudio(
                "sounddevice devolvió un índice de host API inválido"
            ) from exc
        resultado[indice] = str(datos.get("name", "")).strip()
    return resultado


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
        consultar_host_apis = getattr(sounddevice, "query_hostapis", None)
        host_apis = (
            _normalizar_host_apis(consultar_host_apis())
            if callable(consultar_host_apis)
            else {}
        )
        return normalizar_dispositivos_entrada(
            dispositivos, predeterminado, host_apis)
    except ErrorDispositivosAudio:
        raise
    except Exception as exc:
        raise ErrorDispositivosAudio(
            "no se pudieron enumerar dispositivos de entrada con sounddevice"
        ) from exc


def _identidad_dispositivo(dispositivo: DispositivoEntrada) -> str:
    return dispositivo.identidad or crear_identidad_entrada(
        dispositivo.nombre, dispositivo.host_api)


def _resolver_predeterminado(
        inventario: tuple[DispositivoEntrada, ...],
        seleccion: str,
        motivo_fallback: str | None = None,
) -> ResolucionEntrada:
    predeterminados = tuple(
        dispositivo for dispositivo in inventario
        if dispositivo.predeterminado
    )
    if len(predeterminados) == 1:
        dispositivo = predeterminados[0]
        return ResolucionEntrada(
            indice=dispositivo.indice,
            seleccion_solicitada=seleccion,
            identidad_resuelta=_identidad_dispositivo(dispositivo),
            dispositivo=dispositivo,
            usando_fallback=motivo_fallback is not None,
            motivo=motivo_fallback,
        )

    if not inventario:
        motivo_default = "inventario_vacio"
    elif len(predeterminados) > 1:
        motivo_default = "default_ambiguo"
    else:
        motivo_default = "default_no_disponible"
    motivo = (
        f"{motivo_fallback}_{motivo_default}"
        if motivo_fallback is not None
        else motivo_default
    )
    return ResolucionEntrada(
        indice=None,
        seleccion_solicitada=seleccion,
        identidad_resuelta=None,
        dispositivo=None,
        usando_fallback=False,
        motivo=motivo,
    )


def resolver_dispositivo_entrada(
        seleccion: str,
        inventario: Iterable[DispositivoEntrada],
) -> ResolucionEntrada:
    """Resuelve una identidad persistida sin modificarla ni elegir al azar."""
    if not Config.preferencia_entrada_valida(seleccion):
        raise ErrorConfiguracion(
            "'audio_input_device' no contiene una identidad válida")
    disponibles = tuple(inventario)
    if seleccion == Config.AUDIO_INPUT_DEFAULT:
        return _resolver_predeterminado(disponibles, seleccion)

    coincidencias = tuple(
        dispositivo for dispositivo in disponibles
        if _identidad_dispositivo(dispositivo) == seleccion
    )
    if len(coincidencias) == 1:
        dispositivo = coincidencias[0]
        return ResolucionEntrada(
            indice=dispositivo.indice,
            seleccion_solicitada=seleccion,
            identidad_resuelta=_identidad_dispositivo(dispositivo),
            dispositivo=dispositivo,
            usando_fallback=False,
            motivo=None,
        )
    motivo = "seleccion_ausente" if not coincidencias else "seleccion_ambigua"
    return _resolver_predeterminado(disponibles, seleccion, motivo)


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
        overlay_positions=tuple(sorted(Config.OVERLAY_POSITIONS)),
    )
