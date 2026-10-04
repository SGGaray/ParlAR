#!/usr/bin/env python3
"""Construye el release Linux x86_64 de ParlAR de forma reproducible.

Produce en ``--out`` (por defecto ``dist/release``, ignorado por git):

    parlar-<versión>-linux-x86_64.tar.gz
    parlar-<versión>-py3-none-any.whl
    SHA256SUMS

El tarball se arma desde una allowlist explícita: nunca se empaqueta el
checkout entero. Con el mismo commit, ``SOURCE_DATE_EPOCH`` y wheelhouse, dos
builds producen bytes idénticos (orden estable, uid/gid 0, mtime fijo y gzip
sin timestamp). El working tree no se modifica: el wheel se construye desde
una copia temporal.

    scripts/build_release.py --wheelhouse dist/wheelhouse
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARQUITECTURA = "linux-x86_64"

# Archivos de la raíz del release: (origen en el repo, destino en el release).
RAIZ = (
    ("README.md", "README.md"),
    ("README.en.md", "README.en.md"),
    ("LICENSE", "LICENSE"),
    ("SECURITY.md", "SECURITY.md"),
    ("install.sh", "install.sh"),
    ("uninstall.sh", "uninstall.sh"),
)
# share/: launcher, systemd opcional, autostart, ícono, renderers e instalador.
SHARE = (
    ("scripts/parlar_installer.py", "parlar_installer.py"),
    ("scripts/render_desktop.py", "render_desktop.py"),
    ("scripts/render_service.py", "render_service.py"),
    ("scripts/parlar.desktop.in", "parlar.desktop.in"),
    ("scripts/parlar-systemd.desktop.in", "parlar-systemd.desktop.in"),
    ("scripts/parlar.service.in", "parlar.service.in"),
    ("parlar/assets/parlar.svg", "parlar.svg"),
)
EJECUTABLES = {"install.sh", "uninstall.sh"}
# Fuentes del wheel: copia temporal, sin build/, egg-info ni caches del árbol.
FUENTES_WHEEL = ("pyproject.toml", "README.md", "LICENSE")
PATRONES_WHEEL = ("parlar/*.py", "parlar/assets/*.svg",
                  "parlar/assets/brand/*.svg", "parlar/assets/tray/*.svg")
PROHIBIDOS = re.compile(
    r"(^|/)(\.git|\.github|tests|benchmarks|build|\.claude|\.agents|"
    r"__pycache__|snapshots|results|recordings|[^/]*\.egg-info)(/|$)"
    r"|(^|/)(setup\.sh|parlarctl|\.gitignore)$"
    r"|\.(wav|mp3|flac|ogg|opus|pyc|log)$")


class ErrorBuild(RuntimeError):
    pass


def exigir(condicion: bool, mensaje: str) -> None:
    if not condicion:
        raise ErrorBuild(mensaje)


def leer_version(raiz: Path = ROOT) -> str:
    proyecto = tomllib.loads(
        (raiz / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return proyecto["version"]


def source_date_epoch(raiz: Path = ROOT) -> int:
    valor = os.environ.get("SOURCE_DATE_EPOCH")
    if valor:
        return int(valor)
    try:
        salida = subprocess.run(
            ["git", "-C", str(raiz), "log", "-1", "--format=%ct"],
            check=True, text=True, capture_output=True).stdout.strip()
        return int(salida)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return int((raiz / "pyproject.toml").stat().st_mtime)


def sha256(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1 << 20), b""):
            digest.update(bloque)
    return digest.hexdigest()


def generar_constraints(raiz: Path = ROOT) -> str:
    """Normaliza constraints.txt: sólo pins exactos, ordenados, sin duplicados."""
    pins = {}
    for linea in (raiz / "constraints.txt").read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip()
        if not linea:
            continue
        nombre, separador, version = linea.partition("==")
        exigir(bool(separador) and bool(version)
               and not re.search(r"[<>=!~*,; ]", version),
               f"constraint sin versión exacta: {linea!r}")
        clave = re.sub(r"[-_.]+", "-", nombre).lower()
        exigir(clave not in pins, f"constraint duplicada: {clave}")
        pins[clave] = version
    exigir(bool(pins), "constraints.txt vacío")
    lineas = [f"{nombre}=={pins[nombre]}" for nombre in sorted(pins)]
    return ("# Versiones probadas de ParlAR; pip las aplica con -c.\n"
            + "\n".join(lineas) + "\n")


def construir_wheel(version: str, destino: Path, temporal: Path) -> Path:
    fuente = temporal / "fuente"
    for nombre in FUENTES_WHEEL:
        (fuente / nombre).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / nombre, fuente / nombre)
    for patron in PATRONES_WHEEL:
        for ruta in sorted(ROOT.glob(patron)):
            copia = fuente / ruta.relative_to(ROOT)
            copia.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ruta, copia)
    salida = temporal / "wheel"
    subprocess.run([
        sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check",
        "--no-deps", "--no-build-isolation", "--wheel-dir", str(salida),
        str(fuente),
    ], check=True, stdout=subprocess.DEVNULL)
    nombre = f"parlar-{version}-py3-none-any.whl"
    wheel = salida / nombre
    exigir(wheel.is_file(), f"pip no produjo {nombre}")
    shutil.copy2(wheel, destino / nombre)
    return destino / nombre


def leer_wheelhouse(wheelhouse: Path) -> tuple[list[Path], list[str]]:
    """Valida el wheelhouse nativo: cada paquete cubre todos los CPython."""
    manifiesto = json.loads(
        (wheelhouse / "wheelhouse.json").read_text(encoding="utf-8"))
    versiones = sorted(manifiesto, key=lambda v: tuple(map(int, v.split("."))))
    exigir(bool(versiones), "wheelhouse sin versiones de Python")
    ruedas = []
    for version, nombres in manifiesto.items():
        tag = "cp" + version.replace(".", "")
        for nombre in nombres:
            ruta = wheelhouse / nombre
            exigir(ruta.is_file(), f"falta {nombre} en el wheelhouse")
            exigir(f"-{tag}-{tag}-" in nombre and "x86_64" in nombre
                   and "linux" in nombre,
                   f"{nombre} no es un wheel Linux x86_64 de {tag}")
            with zipfile.ZipFile(ruta) as archivo:
                exigir(archivo.testzip() is None, f"{nombre} está corrupto")
            ruedas.append(ruta)
    return sorted(ruedas, key=lambda r: r.name), versiones


def _info_tar(nombre: str, tipo: bytes, tamano: int, modo: int,
              epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(nombre)
    info.type = tipo
    info.size = tamano
    info.mode = modo
    info.mtime = epoch
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def escribir_tarball(staging: Path, prefijo: str, destino: Path,
                     epoch: int) -> None:
    """tar.gz determinista: orden, dueños, permisos, mtime y gzip fijos."""
    crudo = io.BytesIO()
    with tarfile.open(fileobj=crudo, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(_info_tar(prefijo, tarfile.DIRTYPE, 0, 0o755, epoch))
        for ruta in sorted(staging.rglob("*"), key=lambda r: r.relative_to(staging).as_posix()):
            relativo = ruta.relative_to(staging).as_posix()
            nombre = f"{prefijo}/{relativo}"
            if ruta.is_dir():
                tar.addfile(_info_tar(nombre, tarfile.DIRTYPE, 0, 0o755, epoch))
                continue
            datos = ruta.read_bytes()
            modo = 0o755 if relativo in EJECUTABLES else 0o644
            tar.addfile(_info_tar(nombre, tarfile.REGTYPE, len(datos), modo, epoch),
                        io.BytesIO(datos))
    with destino.open("wb") as salida:
        with gzip.GzipFile(filename="", mode="wb", fileobj=salida,
                           compresslevel=9, mtime=0) as comprimido:
            comprimido.write(crudo.getvalue())


def contenido_esperado(version: str, ruedas: list[str]) -> set[str]:
    prefijo = f"parlar-{version}"
    archivos = {destino for _origen, destino in RAIZ}
    archivos |= {"VERSION", "constraints.txt"}
    archivos |= {f"share/{destino}" for _origen, destino in SHARE}
    archivos.add("share/python-versions.txt")
    archivos |= {f"wheels/{nombre}" for nombre in ruedas}
    rutas = {f"{prefijo}/{a}" for a in archivos}
    return rutas | {prefijo, f"{prefijo}/share", f"{prefijo}/wheels"}


def verificar(salida: Path, version: str, ruedas: list[str],
              versiones_python: list[str]) -> None:
    """Revisa el artefacto ya escrito, sin confiar en el staging."""
    tarball = salida / f"parlar-{version}-{ARQUITECTURA}.tar.gz"
    wheel = salida / f"parlar-{version}-py3-none-any.whl"
    sumas = (salida / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    esperadas = {f"{sha256(r)}  {r.name}" for r in (tarball, wheel)}
    exigir(set(sumas) == esperadas, "SHA256SUMS no coincide con los artefactos")

    prefijo = f"parlar-{version}"
    with tarfile.open(tarball, "r:gz") as tar:
        miembros = {m.name: m for m in tar.getmembers()}
        exigir(set(miembros) == contenido_esperado(version, ruedas),
               "contenido del tarball fuera de la allowlist: "
               f"{sorted(set(miembros) ^ contenido_esperado(version, ruedas))}")
        repo = str(ROOT).encode()
        for nombre, miembro in miembros.items():
            exigir(not PROHIBIDOS.search(nombre), f"ruta prohibida: {nombre}")
            exigir(miembro.uid == 0 and miembro.gid == 0
                   and not miembro.uname and not miembro.gname,
                   f"dueño no normalizado: {nombre}")
            exigir(miembro.isdir() or miembro.isfile(),
                   f"tipo no permitido: {nombre}")
            if miembro.isfile():
                datos = tar.extractfile(miembro).read()
                exigir(repo not in datos, f"{nombre} contiene la ruta del repo")
        leido = tar.extractfile(f"{prefijo}/VERSION").read().decode()
        exigir(leido == f"{version}\n", f"VERSION inesperado: {leido!r}")
        python = tar.extractfile(f"{prefijo}/share/python-versions.txt").read()
        exigir(python.decode().split() == versiones_python,
               "python-versions.txt no coincide con el wheelhouse")
        incluido = tar.extractfile(
            f"{prefijo}/wheels/parlar-{version}-py3-none-any.whl").read()
        exigir(hashlib.sha256(incluido).hexdigest() == sha256(wheel),
               "el wheel del tarball difiere del publicado")
    with zipfile.ZipFile(wheel) as archivo:
        metadata = archivo.read(f"parlar-{version}.dist-info/METADATA").decode()
        exigir(f"\nVersion: {version}\n" in metadata,
               "versión del wheel inesperada")


def construir(wheelhouse: Path, salida: Path) -> dict[str, Path]:
    version = leer_version()
    epoch = source_date_epoch()
    os.environ["SOURCE_DATE_EPOCH"] = str(epoch)  # wheel reproducible
    ruedas_nativas, versiones_python = leer_wheelhouse(wheelhouse)
    salida.mkdir(parents=True, exist_ok=True)
    prefijo = f"parlar-{version}"
    tarball = salida / f"{prefijo}-{ARQUITECTURA}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="parlar-release-") as tmp:
        temporal = Path(tmp)
        staging = temporal / prefijo
        (staging / "share").mkdir(parents=True)
        (staging / "wheels").mkdir()
        for origen, destino in RAIZ:
            shutil.copyfile(ROOT / origen, staging / destino)
        for origen, destino in SHARE:
            shutil.copyfile(ROOT / origen, staging / "share" / destino)
        (staging / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        (staging / "constraints.txt").write_text(
            generar_constraints(), encoding="utf-8")
        (staging / "share" / "python-versions.txt").write_text(
            "\n".join(versiones_python) + "\n", encoding="utf-8")
        wheel = construir_wheel(version, staging / "wheels", temporal)
        for rueda in ruedas_nativas:
            shutil.copyfile(rueda, staging / "wheels" / rueda.name)

        escribir_tarball(staging, prefijo, tarball, epoch)
        wheel_publicado = salida / wheel.name
        shutil.copyfile(wheel, wheel_publicado)

    lineas = sorted(f"{sha256(r)}  {r.name}" for r in (tarball, wheel_publicado))
    (salida / "SHA256SUMS").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    nombres = sorted([wheel_publicado.name, *(r.name for r in ruedas_nativas)])
    verificar(salida, version, nombres, versiones_python)
    return {"tarball": tarball, "wheel": wheel_publicado,
            "sha256sums": salida / "SHA256SUMS"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wheelhouse", type=Path, required=True,
                        help="salida de scripts/build_wheelhouse.py")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "release")
    args = parser.parse_args(argv)
    try:
        artefactos = construir(args.wheelhouse.resolve(), args.out.resolve())
    except (ErrorBuild, OSError, subprocess.CalledProcessError, KeyError,
            ValueError) as exc:
        print(f"!! build de release falló: {exc}", file=sys.stderr)
        return 1
    for ruta in artefactos.values():
        print(ruta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
