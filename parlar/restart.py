"""Coordinación externa y acotada del reinicio seguro de ParlAR.

El runtime sólo prepara una frontera segura y se apaga ordenadamente. Este
módulo, ejecutado desde Settings o desde el CLI, espera la limpieza completa y
es el único responsable de iniciar el reemplazo.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .config import SOCKET_PATH
from .control import enviar_comando, lock_instancia_ocupado


UNIDAD_SYSTEMD = "parlar.service"
CODIGO_REQUIERE_CONFIRMACION = 3
CODIGO_YA_EN_CURSO = 4

_TIMEOUT_PREPARACION_S = 120.0
_TIMEOUT_SHUTDOWN_S = 12.0
_TIMEOUT_START_S = 60.0
_INTERVALO_SONDEO_S = 0.05


class EstadoResultadoReinicio(str, Enum):
    LISTO = "ready"
    REQUIERE_CONFIRMACION = "confirmation_required"
    YA_EN_CURSO = "already_restarting"
    NO_DISPONIBLE = "unavailable"
    FALLIDO = "failed"


@dataclass(frozen=True, slots=True)
class ResultadoReinicio:
    estado: EstadoResultadoReinicio
    mensaje: str
    gestor: str | None = None

    @property
    def exitoso(self) -> bool:
        return self.estado == EstadoResultadoReinicio.LISTO


@dataclass(frozen=True, slots=True)
class ContextoRuntime:
    pid: int
    invocacion_systemd: bool


@dataclass(frozen=True, slots=True)
class EstadoSystemd:
    cargada: bool
    activa: bool
    subestado: str
    pid_principal: int


def serializar_resultado(resultado: ResultadoReinicio) -> str:
    return json.dumps({
        "schema_version": 1,
        "result": resultado.estado.value,
        "message": resultado.mensaje,
        "manager": resultado.gestor,
    }, ensure_ascii=False, separators=(",", ":"))


def interpretar_resultado(payload: str) -> ResultadoReinicio:
    try:
        datos = json.loads(payload)
        estado = EstadoResultadoReinicio(datos["result"])
        mensaje = datos["message"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("respuesta de reinicio inválida") from exc
    if datos.get("schema_version") != 1 or not isinstance(mensaje, str):
        raise ValueError("respuesta de reinicio incompatible")
    gestor = datos.get("manager")
    if gestor not in {None, "systemd", "manual"}:
        raise ValueError("gestor de reinicio inválido")
    return ResultadoReinicio(estado, mensaje, gestor)


class GuardiaCoordinadorReinicio:
    """Deduplica coordinadores sin convertir el archivo en estado durable."""

    def __init__(self, ruta=None):
        self.ruta = Path(ruta or f"{SOCKET_PATH}.restart.lock")
        self._fd: int | None = None

    def adquirir(self) -> bool:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.ruta, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("el lock de reinicio no es un archivo propio")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self._fd = fd
            fd = None
            return True
        finally:
            if fd is not None:
                os.close(fd)

    def liberar(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _ejecutar_systemctl(*argumentos: str):
    return subprocess.run(
        ["systemctl", "--user", *argumentos],
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )


def consultar_estado_systemd(
        ejecutar: Callable = _ejecutar_systemctl) -> EstadoSystemd:
    try:
        resultado = ejecutar(
            "show", UNIDAD_SYSTEMD,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--no-pager",
        )
    except (OSError, subprocess.SubprocessError):
        return EstadoSystemd(False, False, "unavailable", 0)
    if resultado.returncode != 0:
        return EstadoSystemd(False, False, "unavailable", 0)
    valores = {}
    for linea in resultado.stdout.splitlines():
        clave, separador, valor = linea.partition("=")
        if separador:
            valores[clave] = valor
    try:
        pid = int(valores.get("MainPID", "0"))
    except ValueError:
        pid = 0
    return EstadoSystemd(
        cargada=valores.get("LoadState") == "loaded",
        activa=valores.get("ActiveState") in {"active", "activating"},
        subestado=valores.get("SubState", "unknown"),
        pid_principal=max(0, pid),
    )


def runtime_administrado_por_systemd(
        contexto: ContextoRuntime, estado: EstadoSystemd) -> bool:
    return (
        contexto.invocacion_systemd
        and estado.cargada
        and estado.activa
        and estado.pid_principal == contexto.pid
    )


def _pid_vivo(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CoordinadorReinicio:
    """Ejecuta un reinicio completo fuera del proceso que será reemplazado."""

    def __init__(
            self, *, enviar: Callable[[str], str] | None = None,
            guardia=None, consultar_systemd: Callable = consultar_estado_systemd,
            ejecutar_systemctl: Callable = _ejecutar_systemctl,
            iniciar_manual: Callable | None = None,
            lock_ocupado: Callable[[], bool] = lock_instancia_ocupado,
            socket_existe: Callable[[], bool] | None = None,
            pid_vivo: Callable[[int], bool] = _pid_vivo,
            reloj: Callable[[], float] = time.monotonic,
            esperar: Callable[[float], object] | None = None):
        self._enviar = enviar or (
            lambda comando: enviar_comando(comando, timeout=0.6))
        self._guardia = guardia or GuardiaCoordinadorReinicio()
        self._consultar_systemd = consultar_systemd
        self._ejecutar_systemctl = ejecutar_systemctl
        self._iniciar_manual = iniciar_manual or self._iniciar_manual_real
        self._lock_ocupado = lock_ocupado
        self._socket_existe = socket_existe or Path(SOCKET_PATH).exists
        self._pid_vivo = pid_vivo
        self._reloj = reloj
        self._esperar = esperar or threading.Event().wait
        self._token = secrets.token_urlsafe(24)

    @staticmethod
    def _iniciar_manual_real():
        from .launcher import iniciar_runtime

        return iniciar_runtime()

    def ejecutar(self, *, terminar_dictado: bool = False) -> ResultadoReinicio:
        if not self._guardia.adquirir():
            return ResultadoReinicio(
                EstadoResultadoReinicio.YA_EN_CURSO,
                "ParlAR ya se está reiniciando.",
            )
        try:
            return self._ejecutar_adquirido(
                terminar_dictado=terminar_dictado)
        finally:
            self._guardia.liberar()

    def reintentar(self, gestor: str) -> ResultadoReinicio:
        """Recupera un arranque fallido sin requerir al runtime ya detenido."""
        if gestor not in {"systemd", "manual"}:
            return ResultadoReinicio(
                EstadoResultadoReinicio.FALLIDO,
                "No se pudo iniciar ParlAR nuevamente.",
            )
        if not self._guardia.adquirir():
            return ResultadoReinicio(
                EstadoResultadoReinicio.YA_EN_CURSO,
                "ParlAR ya se está reiniciando.",
                gestor,
            )
        try:
            if self._lock_ocupado() or self._socket_existe():
                return ResultadoReinicio(
                    EstadoResultadoReinicio.YA_EN_CURSO,
                    "ParlAR ya está iniciándose o ejecutándose.",
                    gestor,
                )
            return self._iniciar_reemplazo(gestor)
        finally:
            self._guardia.liberar()

    def _ejecutar_adquirido(
            self, *, terminar_dictado: bool) -> ResultadoReinicio:
        try:
            contexto = self._consultar_contexto()
        except (OSError, ValueError):
            return ResultadoReinicio(
                EstadoResultadoReinicio.NO_DISPONIBLE,
                "ParlAR no está ejecutándose.",
            )
        estado_systemd = self._consultar_systemd()
        administrado = runtime_administrado_por_systemd(
            contexto, estado_systemd)
        accion = "terminar" if terminar_dictado else "solicitar"
        try:
            respuesta = self._comando_runtime(accion)
        except (OSError, ValueError):
            return ResultadoReinicio(
                EstadoResultadoReinicio.NO_DISPONIBLE,
                "No se pudo solicitar el reinicio de ParlAR.",
            )
        estado = respuesta.get("status")
        if estado == "confirmation_required":
            return ResultadoReinicio(
                EstadoResultadoReinicio.REQUIERE_CONFIRMACION,
                "Hay un dictado en curso.",
            )
        if estado == "already_restarting":
            return ResultadoReinicio(
                EstadoResultadoReinicio.YA_EN_CURSO,
                "ParlAR ya se está reiniciando.",
            )
        if estado not in {"accepted", "deferred"}:
            return ResultadoReinicio(
                EstadoResultadoReinicio.NO_DISPONIBLE,
                "No se pudo solicitar el reinicio de ParlAR.",
            )

        if not self._esperar_trabajo_seguro():
            self._cancelar_preparacion()
            return ResultadoReinicio(
                EstadoResultadoReinicio.FALLIDO,
                "No se pudo reiniciar ParlAR: el trabajo pendiente no terminó.",
            )
        try:
            self._enviar("salir")
        except OSError:
            pass
        if not self._esperar_shutdown(contexto.pid, administrado):
            return ResultadoReinicio(
                EstadoResultadoReinicio.FALLIDO,
                "No se pudo cerrar ParlAR de forma segura.",
                "systemd" if administrado else "manual",
            )

        return self._iniciar_reemplazo(
            "systemd" if administrado else "manual")

    def _iniciar_reemplazo(self, gestor: str) -> ResultadoReinicio:
        administrado = gestor == "systemd"
        proceso = None
        if administrado:
            if not self._reiniciar_systemd():
                return ResultadoReinicio(
                    EstadoResultadoReinicio.FALLIDO,
                    "No se pudo iniciar ParlAR nuevamente.",
                    gestor,
                )
        else:
            try:
                proceso = self._iniciar_manual()
            except OSError:
                return ResultadoReinicio(
                    EstadoResultadoReinicio.FALLIDO,
                    "No se pudo iniciar ParlAR nuevamente.",
                    gestor,
                )

        if not self._esperar_runtime_listo(
                proceso=proceso, administrado=administrado):
            if administrado:
                self._detener_systemd_fallido()
            return ResultadoReinicio(
                EstadoResultadoReinicio.FALLIDO,
                "No se pudo reiniciar ParlAR. Revisá la configuración.",
                gestor,
            )
        return ResultadoReinicio(
            EstadoResultadoReinicio.LISTO,
            "Listo",
            gestor,
        )

    def _consultar_contexto(self) -> ContextoRuntime:
        datos = json.loads(self._enviar("contexto-reinicio"))
        if not isinstance(datos, dict) or datos.get("schema_version") != 1:
            raise ValueError("contexto de reinicio incompatible")
        pid = datos.get("pid")
        invocacion = datos.get("systemd_invocation")
        if not isinstance(pid, int) or pid <= 1 or not isinstance(invocacion, bool):
            raise ValueError("contexto de reinicio inválido")
        return ContextoRuntime(pid, invocacion)

    def _comando_runtime(self, accion: str) -> dict:
        respuesta = self._enviar(
            f"reiniciar v1 {self._token} {accion}")
        datos = json.loads(respuesta)
        if not isinstance(datos, dict) or datos.get("schema_version") != 1:
            raise ValueError("estado de reinicio incompatible")
        return datos

    def _esperar_trabajo_seguro(self) -> bool:
        limite = self._reloj() + _TIMEOUT_PREPARACION_S
        stop_solicitado = False
        while self._reloj() < limite:
            try:
                datos = self._comando_runtime("estado")
            except (OSError, ValueError):
                return False
            if datos.get("safe_to_stop") is True:
                return True
            if datos.get("needs_stop") is True and not stop_solicitado:
                try:
                    self._enviar("detener")
                except OSError:
                    return False
                stop_solicitado = True
            self._esperar(_INTERVALO_SONDEO_S)
        return False

    def _cancelar_preparacion(self) -> None:
        try:
            self._comando_runtime("cancelar")
        except (OSError, ValueError):
            pass

    def _esperar_shutdown(self, pid: int, administrado: bool) -> bool:
        limite = self._reloj() + _TIMEOUT_SHUTDOWN_S
        while self._reloj() < limite:
            recursos_libres = (
                not self._lock_ocupado()
                and not self._socket_existe()
                and not self._pid_vivo(pid)
            )
            if administrado:
                estado = self._consultar_systemd()
                recursos_libres = recursos_libres and not estado.activa
            if recursos_libres:
                return True
            self._esperar(_INTERVALO_SONDEO_S)
        return False

    def _reiniciar_systemd(self) -> bool:
        try:
            resultado = self._ejecutar_systemctl(
                "restart", UNIDAD_SYSTEMD, "--no-pager")
        except (OSError, subprocess.SubprocessError):
            return False
        return resultado.returncode == 0

    def _detener_systemd_fallido(self) -> None:
        try:
            self._ejecutar_systemctl(
                "stop", UNIDAD_SYSTEMD, "--no-pager")
        except (OSError, subprocess.SubprocessError):
            pass

    def _esperar_runtime_listo(self, *, proceso, administrado: bool) -> bool:
        limite = self._reloj() + _TIMEOUT_START_S
        while self._reloj() < limite:
            try:
                datos = json.loads(self._enviar("estado-operativo"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                datos = None
            if isinstance(datos, dict) and datos.get("runtime") == "ready":
                return True
            if proceso is not None and proceso.poll() is not None:
                return False
            if administrado:
                estado = self._consultar_systemd()
                if estado.subestado == "auto-restart" \
                        or (estado.cargada and not estado.activa):
                    return False
            self._esperar(_INTERVALO_SONDEO_S)
        return False


def codigo_salida(resultado: ResultadoReinicio) -> int:
    if resultado.estado == EstadoResultadoReinicio.LISTO:
        return 0
    if resultado.estado == EstadoResultadoReinicio.REQUIERE_CONFIRMACION:
        return CODIGO_REQUIERE_CONFIRMACION
    if resultado.estado == EstadoResultadoReinicio.YA_EN_CURSO:
        return CODIGO_YA_EN_CURSO
    return 1


def reiniciar_main(
        *, terminar_dictado: bool = False,
        reintentar_gestor: str | None = None) -> int:
    coordinador = CoordinadorReinicio()
    resultado = (
        coordinador.reintentar(reintentar_gestor)
        if reintentar_gestor is not None
        else coordinador.ejecutar(terminar_dictado=terminar_dictado)
    )
    try:
        print(serializar_resultado(resultado), flush=True)
    except BrokenPipeError:
        pass
    return codigo_salida(resultado)


def comando_reinicio(
        *, executable: str | None = None,
        terminar_dictado: bool = False,
        reintentar_gestor: str | None = None) -> list[str]:
    comando = [executable or sys.executable, "-m", "parlar", "--reiniciar"]
    if reintentar_gestor is not None:
        if reintentar_gestor not in {"systemd", "manual"}:
            raise ValueError("gestor de reinicio inválido")
        comando.extend(("--reintentar-reinicio", reintentar_gestor))
    elif terminar_dictado:
        comando.append("--terminar-dictado")
    return comando
