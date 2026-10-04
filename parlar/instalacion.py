"""Intérprete estable de una instalación versionada.

``install.sh`` deja cada versión en ``<raíz>/versions/<versión>-<id>/venv`` y
publica la vigente como ``<raíz>/current``. Un proceso viejo sigue corriendo
desde su directorio de versión aunque ``current`` ya apunte a otra: todo lo
que relanza ParlAR (reinicio, apertura de Configuración, bandeja) debe usar
``<raíz>/current/venv/bin/python`` para arrancar la versión vigente.

La raíz se deriva de la ubicación real de este paquete, así que respeta
cualquier ``XDG_DATA_HOME`` o ``PARLAR_INSTALL_HOME`` usado al instalar. En un
checkout o en otro entorno se conserva ``sys.executable``.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

PAQUETE = Path(__file__).resolve().parent
_NOMBRE_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+]*-[0-9a-f]{8}$")


def raiz_instalacion(paquete: Path | None = None) -> Path | None:
    """``<raíz>`` si el paquete vive en ``<raíz>/versions/<v>-<id>/venv``."""
    paquete = paquete or PAQUETE
    for directorio in paquete.parents:
        if (directorio.parent.name == "versions"
                and _NOMBRE_VERSION.match(directorio.name)
                and paquete.is_relative_to(directorio / "venv")):
            raiz = directorio.parent.parent
            if (raiz / "current").is_symlink():
                return raiz
            return None
    return None


def python_estable(paquete: Path | None = None) -> str:
    """Python de ``current`` en una instalación versionada; si no, el actual."""
    raiz = raiz_instalacion(paquete)
    if raiz is not None:
        python = raiz / "current" / "venv" / "bin" / "python"
        if python.exists():
            return str(python)
    return sys.executable
