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
import stat
import sys
import threading
from typing import Callable

from .config import SOCKET_PATH

_MAX_COMANDO = 4096
_TIMEOUT_CLIENTE = 0.35
_MAX_CLIENTES = 8

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
    def __init__(self, manejador: Callable[[str], str], ruta=None):
        self.manejador = manejador
        self.ruta = ruta or SOCKET_PATH
        self._sock: socket.socket | None = None
        self._hilo: threading.Thread | None = None
        self._corriendo = False
        self._identidad = None
        self._clientes = threading.BoundedSemaphore(_MAX_CLIENTES)
        self._hilos_clientes = set()
        self._hilos_lock = threading.Lock()

    def iniciar(self):
        self._preparar_ruta()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.ruta))
        info = os.lstat(self.ruta)
        self._identidad = (info.st_dev, info.st_ino)
        os.chmod(self.ruta, 0o600)
        self._sock.listen(_MAX_CLIENTES)
        self._sock.settimeout(0.5)
        self._corriendo = True
        self._hilo = threading.Thread(target=self._bucle, name="control", daemon=True)
        self._hilo.start()
        print(f"[control] escuchando en {self.ruta}")

    def _preparar_ruta(self):
        try:
            info = os.lstat(self.ruta)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(f"la ruta de control existe y no es socket: {self.ruta}")
        prueba = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        prueba.settimeout(0.1)
        try:
            prueba.connect(str(self.ruta))
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(self.ruta)
            return
        finally:
            prueba.close()
        raise RuntimeError("ya existe una instancia activa de ParlAR")

    def _bucle(self):
        while self._corriendo:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._clientes.acquire(blocking=False):
                conn.close()
                continue
            hilo = threading.Thread(
                target=self._atender_cliente, args=(conn,),
                name="control-cliente", daemon=True)
            with self._hilos_lock:
                self._hilos_clientes.add(hilo)
            hilo.start()

    def _leer_comando(self, conn):
        conn.settimeout(_TIMEOUT_CLIENTE)
        datos = bytearray()
        while len(datos) <= _MAX_COMANDO:
            trozo = conn.recv(min(1024, _MAX_COMANDO + 1 - len(datos)))
            if not trozo:
                break
            datos.extend(trozo)
            if b"\n" in trozo:
                break
        if len(datos) > _MAX_COMANDO:
            return None, "ERR demasiado largo"
        if not datos:
            return None, "ERR vacío"
        if b"\n" in datos:
            linea, resto = bytes(datos).split(b"\n", 1)
            if resto:
                return None, "ERR múltiples líneas"
        else:
            linea = bytes(datos)  # compatibilidad: comando terminado por EOF
        try:
            comando = linea.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return None, "ERR UTF-8 inválido"
        return (comando, None) if comando else (None, "ERR vacío")

    def _atender_cliente(self, conn):
        try:
            comando, error = self._leer_comando(conn)
            respuesta = error or self.manejador(comando)
            conn.sendall((respuesta + "\n").encode("utf-8"))
        except socket.timeout:
            try:
                conn.sendall(b"ERR incompleto\n")
            except OSError:
                pass
        except BrokenPipeError:
            pass
        except (OSError, ValueError) as exc:
            print(f"[control] error de cliente: {exc}", file=sys.stderr)
        finally:
            conn.close()
            with self._hilos_lock:
                self._hilos_clientes.discard(threading.current_thread())
            self._clientes.release()

    def detener(self):
        self._corriendo = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            info = os.lstat(self.ruta)
            identidad = (info.st_dev, info.st_ino)
            if stat.S_ISSOCK(info.st_mode) and identidad == self._identidad:
                os.unlink(self.ruta)
        except FileNotFoundError:
            pass


def enviar_comando(cmd: str) -> str:
    """Lado cliente, usado por el punto de entrada parlarctl."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(str(SOCKET_PATH))
        s.sendall((cmd + "\n").encode("utf-8"))
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
