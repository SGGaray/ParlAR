"""Preparación temprana del runtime NVIDIA empaquetado en el entorno Python.

Los wheels ``nvidia-*`` instalan bibliotecas compartidas bajo
``site-packages/nvidia/*/lib``. El loader dinámico de Linux necesita conocer
esas rutas desde el arranque del proceso, antes de que CTranslate2 intente
cargar cuBLAS/cuDNN.

ParlAR reejecuta una sola vez su entry point con esas rutas incorporadas a
LD_LIBRARY_PATH. No se modifican el shell del usuario ni archivos globales.
"""

from __future__ import annotations

import os
import site
import sys
import sysconfig
from pathlib import Path


_MARCA_BOOTSTRAP = "PARLAR_NVIDIA_RUNTIME_BOOTSTRAPPED"


def _bases_site_packages() -> list[Path]:
    candidatas: list[Path] = []

    try:
        candidatas.extend(Path(ruta) for ruta in site.getsitepackages())
    except Exception:
        pass

    rutas_sysconfig = sysconfig.get_paths()
    for clave in ("purelib", "platlib"):
        ruta = rutas_sysconfig.get(clave)
        if ruta:
            candidatas.append(Path(ruta))

    unicas: list[Path] = []
    vistas: set[str] = set()

    for ruta in candidatas:
        texto = str(ruta)
        if texto not in vistas:
            vistas.add(texto)
            unicas.append(ruta)

    return unicas


def _directorios_runtime_nvidia() -> list[Path]:
    """Devuelve directorios de bibliotecas NVIDIA del intérprete actual."""

    encontrados: list[Path] = []
    vistos: set[str] = set()

    for base in _bases_site_packages():
        raiz = base / "nvidia"
        if not raiz.is_dir():
            continue

        for directorio in sorted(raiz.glob("*/lib")):
            if not directorio.is_dir():
                continue
            if not any(directorio.glob("*.so*")):
                continue

            resuelto = directorio.resolve()
            texto = str(resuelto)

            if texto not in vistos:
                vistos.add(texto)
                encontrados.append(resuelto)

    return encontrados


def preparar_runtime_nvidia(dispositivo: str) -> bool:
    """Reejecuta ParlAR una vez si debe exponer librerías NVIDIA del venv.

    Devuelve False cuando no hace falta reejecutar. En el caso de reexec,
    ``os.execve`` reemplaza el proceso actual y esta función no retorna.
    """

    if dispositivo not in {"auto", "cuda"}:
        return False

    if os.environ.get(_MARCA_BOOTSTRAP) == "1":
        return False

    directorios = [str(ruta) for ruta in _directorios_runtime_nvidia()]
    if not directorios:
        return False

    actuales = [
        ruta
        for ruta in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if ruta
    ]

    combinados: list[str] = []
    vistos: set[str] = set()

    for ruta in directorios + actuales:
        if ruta not in vistos:
            vistos.add(ruta)
            combinados.append(ruta)

    # Si todas las rutas NVIDIA ya estaban presentes al arrancar, no hay nada
    # que preparar.
    if all(ruta in actuales for ruta in directorios):
        return False

    entorno = os.environ.copy()
    entorno["LD_LIBRARY_PATH"] = os.pathsep.join(combinados)
    entorno[_MARCA_BOOTSTRAP] = "1"

    print(
        "[runtime] preparando bibliotecas NVIDIA del entorno Python; "
        "reiniciando ParlAR una vez",
        file=sys.stderr,
        flush=True,
    )

    os.execve(
        sys.executable,
        [sys.executable, "-m", "parlar", *sys.argv[1:]],
        entorno,
    )

    raise RuntimeError("os.execve retornó inesperadamente")
