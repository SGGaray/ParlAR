"""Primitivas de seguridad para los sockets Unix de control de ParlAR.

Reúne tres controles que comparten el daemon y sus clientes:

- Resolución de la ruta del socket: ``$XDG_RUNTIME_DIR`` sólo se usa si es un
  directorio real (no symlink), propio y sin acceso de grupo/otros. Si no,
  ``/run/user/<uid>`` con los mismos requisitos y, como último recurso, un
  directorio privado ``/tmp/parlar-<uid>/`` (nunca un socket suelto en /tmp).
- Directorio privado: se crea con 0700 y se valida por descriptor abierto con
  ``O_NOFOLLOW`` (propietario, tipo y permisos) antes de crear locks o sockets.
- Credenciales del peer: ``SO_PEERCRED`` del kernel identifica al proceso del
  otro extremo; sólo se acepta el mismo UID efectivo.

El módulo no tiene efectos al importarse más allá de leer el entorno.
"""

from __future__ import annotations

import os
import socket
import stat
import struct
from pathlib import Path

NOMBRE_SOCKET_CONTROL = "parlar.sock"
# sockaddr_un.sun_path mide 108 bytes en Linux, incluido el NUL final.
MAX_BYTES_RUTA_SOCKET = 107
_RAIZ_RUN_USER = Path("/run/user")
_RAIZ_TMP = Path("/tmp")
_UCRED = struct.Struct("=iII")  # Linux: pid_t, uid_t, gid_t


class ErrorDirectorioInseguro(RuntimeError):
    """El directorio de runtime no garantiza aislamiento entre usuarios."""


class PeerNoAutorizadoError(PermissionError):
    """El otro extremo del socket pertenece a un usuario distinto."""


def longitud_socket_valida(ruta) -> bool:
    try:
        return 0 < len(os.fsencode(os.fspath(ruta))) <= MAX_BYTES_RUTA_SOCKET
    except (TypeError, ValueError):
        return False


def _directorio_privado_existente(ruta: Path, uid: int) -> bool:
    """Validación sin efectos: directorio real, propio y 0700 o más estricto."""
    try:
        info = os.lstat(ruta)
    except (OSError, ValueError):
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) & 0o077 == 0
    )


def directorio_fallback(uid: int | None = None, *, raiz_tmp=_RAIZ_TMP) -> Path:
    uid = os.getuid() if uid is None else uid
    return Path(raiz_tmp) / f"parlar-{uid}"


def resolver_ruta_socket(
        nombre: str = NOMBRE_SOCKET_CONTROL, *, entorno=None,
        uid: int | None = None, raiz_run_user=_RAIZ_RUN_USER,
        raiz_tmp=_RAIZ_TMP) -> Path:
    """Elige un directorio privado del usuario para el socket ``nombre``.

    Nunca devuelve una ruta directamente dentro de un directorio compartido:
    el respaldo es un subdirectorio por UID que se valida al usarse.
    """
    entorno = os.environ if entorno is None else entorno
    uid = os.getuid() if uid is None else uid
    candidatos = []
    runtime = entorno.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isabs(runtime):
        candidatos.append(Path(runtime))
    candidatos.append(Path(raiz_run_user) / str(uid))
    for directorio in candidatos:
        ruta = directorio / nombre
        if (_directorio_privado_existente(directorio, uid)
                and longitud_socket_valida(ruta)):
            return ruta
    return directorio_fallback(uid, raiz_tmp=raiz_tmp) / nombre


def ruta_socket_legada(*, entorno=None, uid: int | None = None) -> Path:
    """Ruta que calculaban las versiones previas (sólo para compatibilidad)."""
    entorno = os.environ if entorno is None else entorno
    uid = os.getuid() if uid is None else uid
    runtime = entorno.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / NOMBRE_SOCKET_CONTROL
    return _RAIZ_TMP / f"parlar-{uid}.sock"


def asegurar_directorio_privado(ruta) -> None:
    """Crea (0700) o valida el directorio que contendrá sockets y locks.

    La validación usa un descriptor abierto con ``O_NOFOLLOW`` para que un
    symlink plantado no redirija la comprobación. Falla cerrado.
    """
    ruta = Path(ruta)
    try:
        os.mkdir(ruta, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ErrorDirectorioInseguro(
            f"no se pudo crear el directorio de runtime: {ruta}") from exc
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(ruta, flags)
    except OSError as exc:
        raise ErrorDirectorioInseguro(
            f"el directorio de runtime no es un directorio real: {ruta}"
        ) from exc
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ErrorDirectorioInseguro(
            f"el directorio de runtime no es un directorio: {ruta}")
    if info.st_uid != os.getuid():
        raise ErrorDirectorioInseguro(
            f"el directorio de runtime pertenece a otro usuario: {ruta}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ErrorDirectorioInseguro(
            "el directorio de runtime permite acceso a otros usuarios "
            f"(se requiere 0700): {ruta}")


def uid_peer(sock) -> int:
    """UID del proceso remoto según el kernel (Linux ``SO_PEERCRED``)."""
    opcion = getattr(socket, "SO_PEERCRED", None)
    if opcion is None:
        raise PeerNoAutorizadoError("SO_PEERCRED no disponible")
    try:
        datos = sock.getsockopt(socket.SOL_SOCKET, opcion, _UCRED.size)
        if len(datos) != _UCRED.size:
            raise ValueError("credenciales de peer malformadas")
        _, uid, _ = _UCRED.unpack(datos)
    except (OSError, TypeError, ValueError, struct.error) as exc:
        raise PeerNoAutorizadoError(
            "no se pudieron leer las credenciales del peer") from exc
    return uid


def verificar_peer(sock, uid_esperado: int | None = None) -> None:
    """Rechaza el socket si el peer no es el mismo usuario."""
    esperado = os.getuid() if uid_esperado is None else uid_esperado
    if uid_peer(sock) != esperado:
        raise PeerNoAutorizadoError("peer de control con UID inesperado")
