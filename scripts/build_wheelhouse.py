#!/usr/bin/env python3
"""Construye el wheelhouse nativo del release Linux x86_64 de ParlAR.

Para cada intérprete CPython recibido revisa en PyPI cada versión fijada en
``constraints.txt``. Sólo las que no publican un wheel instalable en
Linux x86_64 para ese CPython (por ejemplo evdev, o webrtcvad-wheels en
versiones nuevas) se compilan aquí y se reparan con ``auditwheel`` a un tag
manylinux. El resto lo descarga pip como wheel normal durante la
instalación, respetando las constraints.

Uso de mantenimiento (requiere red, compilador y headers del kernel):

    scripts/build_wheelhouse.py --out dist/wheelhouse \\
        --python /ruta/python3.12 --python /ruta/python3.13 \\
        --python /ruta/python3.14 --auditwheel /ruta/auditwheel
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PYPI = "https://pypi.org/pypi/{nombre}/{version}/json"
_TAG_WHEEL = re.compile(
    r"^(?P<dist>.+?)-(?P<ver>[^-]+)(?:-\d[^-]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)"
    r"-(?P<plat>[^-]+)\.whl$")


def leer_constraints(ruta: Path) -> dict[str, str]:
    pins = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip()
        if not linea:
            continue
        nombre, separador, version = linea.partition("==")
        if not separador or not version or any(c in version for c in "<>=!~*,; "):
            raise ValueError(f"constraint sin versión exacta: {linea!r}")
        pins[normalizar(nombre)] = version
    return pins


def normalizar(nombre: str) -> str:
    return re.sub(r"[-_.]+", "-", nombre).lower()


def plataforma_linux_x86_64(plataforma: str) -> bool:
    return any(
        parte == "any"
        or (parte.endswith("_x86_64")
            and (parte.startswith("manylinux") or parte == "linux_x86_64"))
        for parte in plataforma.split(".")
    )


def wheel_compatible(nombre_archivo: str, minor: int) -> bool:
    """¿Instala este wheel en CPython 3.<minor> Linux x86_64 (glibc)?"""
    coincidencia = _TAG_WHEEL.match(nombre_archivo)
    if not coincidencia:
        return False
    py = coincidencia["py"].split(".")
    abi = coincidencia["abi"].split(".")
    if not plataforma_linux_x86_64(coincidencia["plat"]):
        return False
    cp = f"cp3{minor}"
    if cp in py and cp in abi:
        return True
    if "abi3" in abi:
        return any(
            p.startswith("cp3") and p[3:].isdigit() and int(p[3:]) <= minor
            for p in py)
    return "none" in abi and "py3" in py


def requiere_compilacion(nombre: str, version: str, minor: int) -> bool:
    with urllib.request.urlopen(PYPI.format(nombre=nombre, version=version),
                                timeout=60) as respuesta:
        archivos = [u["filename"] for u in json.load(respuesta)["urls"]]
    return not any(wheel_compatible(a, minor) for a in archivos)


def minor_de(python: str) -> int:
    salida = subprocess.run(
        [python, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
        check=True, text=True, capture_output=True).stdout.split()
    if salida[0] != "3":
        raise SystemExit(f"{python} no es CPython 3")
    return int(salida[1])


def tag_cpython(minor: int) -> str:
    return f"cp3{minor}"


def exigir_abi(wheel: Path, tag: str) -> None:
    """El wheel compilado tiene que ser exactamente del CPython que lo hizo."""
    coincidencia = _TAG_WHEEL.match(wheel.name)
    if (not coincidencia or coincidencia["py"] != tag
            or coincidencia["abi"] != tag):
        raise SystemExit(f"{wheel.name} no es un wheel {tag}-{tag}")


def compilar(python: str, tag: str, nombre: str, version: str,
             auditwheel: str, salida: Path, temporal: Path) -> Path:
    # Staging por CPython (cp312, cp313...): los intérpretes manylinux se
    # llaman todos .../bin/python, así que el basename no los distingue.
    venv = temporal / f"venv-{tag}"
    if not venv.exists():  # un mismo CPython reutiliza su venv entre paquetes
        subprocess.run([python, "-m", "venv", str(venv)], check=True)
    crudo = temporal / f"crudo-{nombre}-{tag}"
    crudo.mkdir()
    subprocess.run([
        str(venv / "bin" / "python"), "-m", "pip", "wheel",
        "--disable-pip-version-check", "--no-deps",
        "--no-binary", nombre, "--wheel-dir", str(crudo),
        f"{nombre}=={version}",
    ], check=True)
    construidos = list(crudo.glob("*.whl"))
    if len(construidos) != 1:
        raise SystemExit(f"se esperaba un wheel de {nombre}: {construidos}")
    exigir_abi(construidos[0], tag)
    reparado = temporal / f"reparado-{nombre}-{tag}"
    # auditwheel invoca patchelf: se busca junto a auditwheel (pip install
    # patchelf en el mismo entorno) antes que en el PATH del sistema.
    entorno = dict(os.environ)
    entorno["PATH"] = f"{Path(auditwheel).parent}{os.pathsep}{entorno.get('PATH', '')}"
    subprocess.run([auditwheel, "repair", "--wheel-dir", str(reparado),
                    str(construidos[0])], check=True, env=entorno)
    resultado = list(reparado.glob("*.whl"))
    if len(resultado) != 1:
        raise SystemExit(f"auditwheel no produjo un wheel único: {resultado}")
    exigir_abi(resultado[0], tag)
    destino = salida / resultado[0].name
    shutil.copy2(resultado[0], destino)
    return destino


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", action="append", required=True)
    parser.add_argument("--auditwheel", default=shutil.which("auditwheel"))
    parser.add_argument("--constraints", type=Path,
                        default=ROOT / "constraints.txt")
    args = parser.parse_args(argv)
    if not args.auditwheel:
        parser.error("auditwheel no encontrado; usá --auditwheel")

    pins = leer_constraints(args.constraints)
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SOURCE_DATE_EPOCH", "315532800")
    manifiesto = {}
    with tempfile.TemporaryDirectory(prefix="parlar-wheelhouse-") as tmp:
        temporal = Path(tmp)
        for python in args.python:
            minor = minor_de(python)
            tag = tag_cpython(minor)
            if f"3.{minor}" in manifiesto:
                raise SystemExit(f"{tag} repetido: {python}")
            nativos = [
                nombre for nombre, version in sorted(pins.items())
                if requiere_compilacion(nombre, version, minor)
            ]
            print(f"==> {tag}: compilar {', '.join(nativos) or 'nada'}")
            manifiesto[f"3.{minor}"] = []
            for nombre in nativos:
                wheel = compilar(python, tag, nombre, pins[nombre],
                                 args.auditwheel, args.out, temporal)
                manifiesto[f"3.{minor}"].append(wheel.name)
                print(f"    {wheel.name}")
    (args.out / "wheelhouse.json").write_text(
        json.dumps(manifiesto, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
