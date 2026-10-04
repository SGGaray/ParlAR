"""Modelo puro y lifecycle del tray complementario de Linux.

GTK vive en ``tray_gtk.py``, ejecutado con el Python del sistema. El runtime
principal no importa bindings gráficos: el helper usa exclusivamente el socket
Unix de control y desaparece con él.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable

from .config import SOCKET_PATH
from .estado_operativo import (
    EstadoOperativo,
    EstadoRuntime,
    Severidad,
    puede_dictar,
    severidad_global,
)


@dataclass(frozen=True, slots=True)
class EstadoTray:
    categoria: str
    estado: str
    tooltip: str
    accion_pausa: str
    comando_pausa: str
    pausa_habilitada: bool = True
    reinicio_pendiente: bool = False


def resumir_estado_tray(
        estado_app: str,
        operativo: EstadoOperativo,
        *,
        pausa_pendiente: bool = False,
        reinicio_pendiente: bool = False) -> EstadoTray:
    """Reduce lifecycle y salud a metadatos breves, sin contenido dictado."""
    if operativo.runtime != EstadoRuntime.READY:
        if operativo.runtime == EstadoRuntime.STARTING:
            estado = "Preparando…"
        else:
            estado = "No disponible"
        return EstadoTray(
            "unavailable", estado, f"ParlAR — {estado}",
            "Pausar", "pausar", False, reinicio_pendiente)

    if reinicio_pendiente:
        estado = (
            "Procesando · reinicio pendiente"
            if estado_app == "stopping"
            else "Reiniciando…"
        )
        return EstadoTray(
            "busy", estado, f"ParlAR — {estado}",
            "Pausar", "pausar", False, True)

    if pausa_pendiente:
        estado = (
            "Procesando · pausa pendiente"
            if estado_app == "stopping"
            else "Deteniendo · pausa pendiente"
        )
        return EstadoTray(
            "busy", estado, "ParlAR — Pausa pendiente",
            "Cancelar pausa", "reanudar")

    if operativo.pausado:
        return EstadoTray(
            "paused", "Pausado", "ParlAR — Pausado",
            "Reanudar", "reanudar")

    if estado_app in {"starting", "recording"}:
        return EstadoTray(
            "busy", "Escuchando", "ParlAR — Escuchando",
            "Detener y pausar", "pausar")
    if estado_app == "stopping":
        return EstadoTray(
            "busy", "Procesando", "ParlAR — Procesando",
            "Pausar al terminar", "pausar")

    if severidad_global(operativo) == Severidad.WARNING:
        return EstadoTray(
            "attention", "Requiere atención",
            "ParlAR — Requiere atención", "Pausar", "pausar")
    if not puede_dictar(operativo):
        return EstadoTray(
            "unavailable", "No disponible", "ParlAR — No disponible",
            "Pausar", "pausar", False)
    return EstadoTray(
        "available", "Disponible", "ParlAR — Disponible",
        "Pausar", "pausar")


def serializar_estado_tray(estado: EstadoTray) -> str:
    """Contrato IPC v1 limitado a estado y acciones del tray."""
    return json.dumps({
        "schema_version": 1,
        "category": estado.categoria,
        "status": estado.estado,
        "tooltip": estado.tooltip,
        "pause_action": estado.accion_pausa,
        "pause_command": estado.comando_pausa,
        "pause_enabled": estado.pausa_habilitada,
        "restart_pending": estado.reinicio_pendiente,
    }, ensure_ascii=False, separators=(",", ":"))


class TrayLinux:
    """Administra un único helper GTK sin volverlo requisito del runtime."""

    def __init__(
            self, *, ruta_socket=SOCKET_PATH, python_launcher=None,
            helper_python="/usr/bin/python3", popen=subprocess.Popen,
            al_fallo: Callable[[str], object] | None = None,
            entorno=None):
        self.ruta_socket = Path(ruta_socket)
        self.python_launcher = python_launcher or sys.executable
        self.helper_python = helper_python
        self._popen = popen
        self._al_fallo = al_fallo
        self._entorno = entorno
        self._lock = threading.RLock()
        self._proceso = None
        self._monitor = None
        self._deteniendo = False
        self._fallo_reportado = False

    @property
    def iniciado(self) -> bool:
        with self._lock:
            return self._proceso is not None and self._proceso.poll() is None

    def iniciar(self) -> bool:
        with self._lock:
            if self._proceso is not None and self._proceso.poll() is None:
                return True
            entorno = dict(os.environ if self._entorno is None else self._entorno)
            if sys.platform != "linux" or not entorno.get("DISPLAY"):
                self._reportar_fallo("no hay una sesión X11 disponible")
                return False
            helper = Path(__file__).with_name("tray_gtk.py")
            icono = Path(__file__).with_name("assets") / "parlar.svg"
            comando = [
                self.helper_python,
                str(helper),
                "--socket", str(self.ruta_socket),
                "--launcher-python", str(self.python_launcher),
                "--icon", str(icono),
            ]
            try:
                proceso = self._popen(
                    comando,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    env=entorno,
                )
            except (OSError, ValueError) as exc:
                self._reportar_fallo(
                    f"no se pudo iniciar el helper GTK ({type(exc).__name__})")
                return False
            self._proceso = proceso
            self._deteniendo = False
            monitor = threading.Thread(
                target=self._vigilar, args=(proceso,),
                name="tray-monitor", daemon=True)
            self._monitor = monitor
            monitor.start()
            return True

    def _vigilar(self, proceso) -> None:
        detalle = None
        salida = proceso.stdout
        if salida is not None:
            try:
                for linea in salida:
                    linea = linea.strip()
                    if linea.startswith("ERROR "):
                        detalle = linea[6:]
            except (OSError, ValueError):
                detalle = detalle or "se perdió la comunicación con el helper"
        try:
            codigo = proceso.wait()
        except (OSError, ValueError):
            codigo = -1
        with self._lock:
            esperado = self._deteniendo or self._proceso is not proceso
            if self._proceso is proceso:
                self._proceso = None
        if not esperado:
            self._reportar_fallo(
                detalle or f"el helper GTK terminó (código {codigo})")

    def _reportar_fallo(self, detalle: str) -> None:
        with self._lock:
            if self._fallo_reportado:
                return
            self._fallo_reportado = True
        if self._al_fallo is not None:
            self._al_fallo(detalle)

    def detener(self) -> None:
        with self._lock:
            proceso = self._proceso
            self._proceso = None
            self._deteniendo = True
        if proceso is None:
            return
        try:
            if proceso.poll() is None:
                proceso.terminate()
                try:
                    proceso.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proceso.kill()
                    proceso.wait(timeout=1.0)
        finally:
            salida = getattr(proceso, "stdout", None)
            if salida is not None:
                try:
                    salida.close()
                except OSError:
                    pass


def crear_tray(*, al_fallo=None, ruta_socket=SOCKET_PATH) -> TrayLinux:
    return TrayLinux(al_fallo=al_fallo, ruta_socket=ruta_socket)
