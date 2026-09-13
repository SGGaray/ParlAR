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
import fcntl
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .config import SOCKET_PATH

_MAX_COMANDO = 4096
_TIMEOUT_CLIENTE = 0.35
_DEADLINE_CLIENTE = 1.0
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
        self._ruta_lock = Path(f"{self.ruta}.lock")
        self._sock: socket.socket | None = None
        self._hilo: threading.Thread | None = None
        self._corriendo = False
        self._identidad = None
        self._lock_fd = None
        self._lock_identidad = None
        self._clientes = threading.BoundedSemaphore(_MAX_CLIENTES)
        self._hilos_clientes = set()
        self._conexiones_activas = set()
        self._hilos_lock = threading.Lock()
        self._detener_lock = threading.Lock()

    def iniciar(self):
        with self._detener_lock:
            self._adquirir_lock_instancia()
            try:
                self._preparar_ruta()
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.bind(str(self.ruta))
                info = os.lstat(self.ruta)
                self._identidad = (info.st_dev, info.st_ino)
                os.chmod(self.ruta, 0o600)
                self._activar_listener()
                self._sock.settimeout(0.5)
                with self._hilos_lock:
                    self._corriendo = True
                self._hilo = threading.Thread(
                    target=self._bucle, name="control", daemon=True)
                self._hilo.start()
            except BaseException as error_inicio:
                with self._hilos_lock:
                    self._corriendo = False
                self._cerrar_listener()
                try:
                    self._limpiar_recursos_instancia()
                except BaseException as error_cleanup:
                    detalle = (
                        "el rollback de control también falló: "
                        f"{type(error_cleanup).__name__}: {error_cleanup}"
                    )
                    anotar = getattr(error_inicio, "add_note", None)
                    if anotar is not None:
                        anotar(detalle)
                    print(f"[control] {detalle}", file=sys.stderr)
                self._hilo = None
                raise
        print(f"[control] escuchando en {self.ruta}")

    def _activar_listener(self):
        """Frontera separada para probar la ventana bind→listen."""
        self._sock.listen(_MAX_CLIENTES)

    def _adquirir_lock_instancia(self):
        if self._lock_fd is not None:
            raise RuntimeError("la instancia de control ya posee su lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._ruta_lock, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(
                f"no se pudo abrir el lock de control: {self._ruta_lock}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("el lock de control no es un archivo propio")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "ya existe una instancia activa de ParlAR") from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd
        self._lock_identidad = (info.st_dev, info.st_ino)

    def _liberar_lock_instancia(self):
        fd, self._lock_fd = self._lock_fd, None
        self._lock_identidad = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _limpiar_recursos_instancia(self):
        """Intenta endpoint y flock completos, preservando el primer error."""
        primer_error = None
        try:
            self._limpiar_endpoint_propio()
        except BaseException as exc:
            primer_error = exc
        try:
            self._liberar_lock_instancia()
        except BaseException as exc:
            if primer_error is None:
                primer_error = exc
            else:
                detalle = (
                    "la liberación del lock también falló: "
                    f"{type(exc).__name__}: {exc}"
                )
                anotar = getattr(primer_error, "add_note", None)
                if anotar is not None:
                    anotar(detalle)
                print(f"[control] {detalle}", file=sys.stderr)
        if primer_error is not None:
            raise primer_error

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
        while True:
            with self._hilos_lock:
                if not self._corriendo:
                    break
                sock = self._sock
            if sock is None:
                break
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            aceptado_en = time.monotonic()
            if not self._clientes.acquire(blocking=False):
                conn.close()
                continue
            hilo = threading.Thread(
                target=self._atender_cliente, args=(conn, aceptado_en),
                name="control-cliente", daemon=True)
            with self._hilos_lock:
                if not self._corriendo:
                    conn.close()
                    self._clientes.release()
                    continue
                self._hilos_clientes.add(hilo)
                self._conexiones_activas.add(conn)
                try:
                    hilo.start()
                except BaseException:
                    self._hilos_clientes.discard(hilo)
                    self._conexiones_activas.discard(conn)
                    conn.close()
                    self._clientes.release()
                    raise

    def _leer_comando(self, conn, aceptado_en=None):
        aceptado_en = aceptado_en or time.monotonic()
        deadline = aceptado_en + _DEADLINE_CLIENTE
        datos = bytearray()
        while len(datos) <= _MAX_COMANDO:
            restante = deadline - time.monotonic()
            if restante <= 0:
                raise socket.timeout("deadline total agotado")
            conn.settimeout(min(_TIMEOUT_CLIENTE, restante))
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

    def _atender_cliente(self, conn, aceptado_en):
        try:
            comando, error = self._leer_comando(conn, aceptado_en)
            with self._hilos_lock:
                if not self._corriendo:
                    return
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
            with self._hilos_lock:
                corriendo = self._corriendo
            if corriendo:
                print(f"[control] error de cliente: {exc}", file=sys.stderr)
        finally:
            conn.close()
            with self._hilos_lock:
                self._hilos_clientes.discard(threading.current_thread())
                self._conexiones_activas.discard(conn)
            self._clientes.release()

    def detener(self):
        with self._detener_lock:
            with self._hilos_lock:
                self._corriendo = False
                conexiones = list(self._conexiones_activas)
                hilos = list(self._hilos_clientes)
            hilo_servidor = self._hilo
            self._cerrar_listener()
        for conn in conexiones:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        actual = threading.current_thread()
        if hilo_servidor is not None and hilo_servidor is not actual:
            hilo_servidor.join()
        for hilo in hilos:
            if hilo is not actual:
                hilo.join()
        with self._detener_lock:
            self._hilo = None
            self._limpiar_recursos_instancia()

    def _cerrar_listener(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _limpiar_endpoint_propio(self):
        identidad_propia, self._identidad = self._identidad, None
        if identidad_propia is None:
            return
        try:
            info = os.lstat(self.ruta)
            identidad = (info.st_dev, info.st_ino)
            if stat.S_ISSOCK(info.st_mode) and identidad == identidad_propia:
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
