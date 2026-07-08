"""Canal de control por socket Unix.

El daemon escucha en $XDG_RUNTIME_DIR/parlar.sock. `parlarctl <cmd>` envía
comandos de una línea. Este es el camino canónico para atajos en Wayland:
asigná `parlarctl alternar` a un atajo de teclado del compositor/DE.

Comandos (español primero, alias en inglés entre paréntesis):
  alternar (toggle) | iniciar (start) | detener (stop) | estado (status)
  modo <utterance|frase|streaming> (mode) | salir (quit)
  reescritura <none|ninguna|formal|concise|conciso|email|correo> (rewrite)
"""

import os
import socket
import sys
import threading
from typing import Callable

from .config import SOCKET_PATH

# alias inglés -> canónico español (UX para quien prefiera comandos en inglés)
ALIAS_COMANDOS = {
    "toggle": "alternar", "start": "iniciar", "stop": "detener",
    "status": "estado", "mode": "modo", "rewrite": "reescritura",
    "quit": "salir",
}

# alias de valores (español -> valor interno estable)
ALIAS_VALORES = {
    "frase": "utterance", "ninguna": "none", "conciso": "concise",
    "correo": "email",
}


def normalizar_comando(cmd: str) -> list[str]:
    partes = cmd.strip().split()
    if not partes:
        return []
    partes[0] = ALIAS_COMANDOS.get(partes[0].lower(), partes[0].lower())
    if len(partes) > 1:
        partes[1] = ALIAS_VALORES.get(partes[1].lower(), partes[1].lower())
    return partes


class ServidorControl:
    def __init__(self, manejador: Callable[[str], str]):
        self.manejador = manejador
        self._sock: socket.socket | None = None
        self._hilo: threading.Thread | None = None
        self._corriendo = False

    def iniciar(self):
        try:
            if SOCKET_PATH.exists():
                SOCKET_PATH.unlink()
        except OSError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self._corriendo = True
        self._hilo = threading.Thread(target=self._bucle, name="control", daemon=True)
        self._hilo.start()
        print(f"[control] escuchando en {SOCKET_PATH}")

    def _bucle(self):
        while self._corriendo:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(2.0)
                data = conn.recv(4096).decode(errors="replace").strip()
                respuesta = self.manejador(data) if data else "ERR vacío"
                conn.sendall((respuesta + "\n").encode())
            except Exception as e:
                print(f"[control] error de cliente: {e}", file=sys.stderr)
            finally:
                conn.close()

    def detener(self):
        self._corriendo = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass


def enviar_comando(cmd: str) -> str:
    """Lado cliente, usado por el punto de entrada parlarctl."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(str(SOCKET_PATH))
        s.sendall(cmd.encode())
        return s.recv(4096).decode(errors="replace").strip()
    finally:
        s.close()


def parlarctl_main():
    if len(sys.argv) < 2:
        print("uso: parlarctl <alternar|iniciar|detener|estado|modo M|reescritura M|salir>")
        print("     (los comandos en inglés también funcionan)")
        sys.exit(2)
    cmd = " ".join(sys.argv[1:])
    try:
        print(enviar_comando(cmd))
    except (ConnectionRefusedError, FileNotFoundError):
        print("el daemon de parlar no está corriendo", file=sys.stderr)
        sys.exit(1)
