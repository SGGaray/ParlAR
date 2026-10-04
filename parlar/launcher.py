"""Launcher gráfico liviano para ParlAR.

La intención explícita abre Settings enseguida y deja el runtime en un proceso
independiente. Este módulo no importa Tk, Whisper ni hardware al cargarse.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import SOCKET_PATH
from .control import enviar_comando, lock_instancia_ocupado
from .instalacion import python_estable
from .settings_backend import (
    EstadoParlARSettings,
    consultar_estado_parlar,
    estado_fallo_inicio,
    estado_preparando_parlar,
)


@dataclass(frozen=True, slots=True)
class EstadoApertura:
    """Decisión observable del runtime para una apertura explícita."""

    runtime: str
    permitir_foco: bool


def interpretar_respuesta_apertura(respuesta: str) -> EstadoApertura:
    """Valida la respuesta IPC sin confiar en campos extra del daemon."""
    try:
        datos = json.loads(respuesta)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("respuesta de apertura inválida") from exc
    if not isinstance(datos, dict) or datos.get("schema_version") != 1:
        raise ValueError("respuesta de apertura incompatible")
    runtime = datos.get("runtime")
    foco = datos.get("focus")
    if not isinstance(runtime, str) or foco not in {"allow", "defer"}:
        raise ValueError("respuesta de apertura incompleta")
    return EstadoApertura(runtime, foco == "allow")


def consultar_apertura_runtime(
        *, enviar: Callable[[str], str] | None = None,
        lock_ocupado: Callable[[], bool] = lock_instancia_ocupado,
) -> EstadoApertura:
    """Consulta el daemon o distingue ausencia de startup por el flock."""
    if enviar is None:
        enviar = lambda comando: enviar_comando(comando, timeout=0.35)
    try:
        return interpretar_respuesta_apertura(enviar("abrir-configuracion"))
    except (ConnectionRefusedError, FileNotFoundError):
        return EstadoApertura(
            "starting" if lock_ocupado() else "stopped",
            True,
        )
    except (OSError, ValueError):
        # Hay un endpoint no interpretable: no se crea otro runtime y el foco
        # se difiere hasta poder tomar una decisión segura.
        return EstadoApertura("unavailable", False)


def iniciar_runtime(
        *, popen: Callable = subprocess.Popen,
        executable: str | None = None):
    """Inicia el CLI histórico desacoplado de la vida de Settings."""
    # Instalado: arranca la versión de current aunque este proceso sea viejo.
    python = executable or python_estable()
    return popen(
        [python, "-m", "parlar"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


class ObservadorRuntime:
    """Combina IPC con el único hijo conocido por este lanzamiento."""

    def __init__(
            self, proceso=None, *,
            consultar: Callable[[], EstadoParlARSettings] = consultar_estado_parlar,
            lock_ocupado: Callable[[], bool] = lock_instancia_ocupado):
        self.proceso = proceso
        self._consultar = consultar
        self._lock_ocupado = lock_ocupado

    def consultar(self) -> EstadoParlARSettings:
        estado = self._consultar()
        if estado.ejecutandose:
            return estado
        if self.proceso is not None:
            codigo = self.proceso.poll()
            if codigo is None:
                return estado_preparando_parlar()
            if codigo != 0:
                return estado_fallo_inicio(codigo)
        if self._lock_ocupado():
            return estado_preparando_parlar()
        return estado


class GuardiaVentanaSettings:
    """Singleton local de Settings con pedido de presentación por SIGUSR1."""

    def __init__(self, ruta=None):
        self.ruta = Path(ruta or f"{SOCKET_PATH}.settings.lock")
        self._fd: int | None = None
        self._handler_anterior = None
        self._presentacion_pendiente = threading.Event()

    @property
    def adquirida(self) -> bool:
        return self._fd is not None

    def adquirir_o_solicitar_presentacion(self) -> bool:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.ruta, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("el lock de Settings no es un archivo propio")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._solicitar_presentacion(fd)
                return False
            self.instalar_receptor_presentacion()
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
            self._fd = fd
            fd = None
            return True
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _solicitar_presentacion(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        contenido = os.read(fd, 32)
        try:
            pid = int(contenido.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return
        if pid <= 1:
            return
        try:
            os.kill(pid, signal.SIGUSR1)
        except (ProcessLookupError, PermissionError):
            pass

    def instalar_receptor_presentacion(self) -> None:
        if self._handler_anterior is not None:
            return

        def solicitar(_numero, _frame):
            self._presentacion_pendiente.set()

        self._handler_anterior = signal.signal(signal.SIGUSR1, solicitar)

    def consumir_presentacion(self) -> bool:
        pendiente = self._presentacion_pendiente.is_set()
        if pendiente:
            self._presentacion_pendiente.clear()
        return pendiente

    def liberar(self) -> None:
        if self._handler_anterior is not None:
            signal.signal(signal.SIGUSR1, self._handler_anterior)
            self._handler_anterior = None
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def main() -> int:
    apertura = consultar_apertura_runtime()
    proceso = None
    if apertura.runtime == "stopped":
        try:
            proceso = iniciar_runtime()
        except OSError as exc:
            print(
                "[launcher] no se pudo iniciar el runtime: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            observador = ObservadorRuntime(
                consultar=lambda: estado_fallo_inicio(None),
                lock_ocupado=lambda: False,
            )
        else:
            observador = ObservadorRuntime(proceso)
    else:
        observador = ObservadorRuntime()

    def puede_presentar() -> bool:
        decision = consultar_apertura_runtime()
        return decision.permitir_foco or decision.runtime in {
            "stopped", "unavailable",
        }

    from .settings_window import main as settings_main

    return settings_main(
        consultar_estado=observador.consultar,
        permitir_foco_inicial=apertura.permitir_foco,
        puede_presentar=puede_presentar,
    )
