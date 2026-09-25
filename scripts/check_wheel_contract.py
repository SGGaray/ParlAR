#!/usr/bin/env python3
"""Valida el wheel de ParlAR y su instalación aislada sin iniciar el daemon."""

import argparse
import configparser
from email import policy
from email.parser import BytesParser
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import sysconfig
import tomllib
import zipfile


class ContractError(RuntimeError):
    """El artefacto no cumple el contrato instalable esperado."""


def _normalizar_distribucion(valor):
    return re.sub(r"[-_.]+", "_", valor).lower()


def _normalizar_version(valor):
    return re.sub(r"[^A-Za-z0-9.]+", "_", valor)


def _cargar_proyecto(ruta):
    return tomllib.loads(Path(ruta).read_text(encoding="utf-8"))["project"]


def _exigir(condicion, mensaje):
    if not condicion:
        raise ContractError(mensaje)


def _leer_miembro(archivo, nombre):
    try:
        return archivo.read(nombre)
    except KeyError as exc:
        raise ContractError(f"falta {nombre}") from exc


def _entrypoints_desde_bytes(contenido):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(contenido.decode("utf-8"))
    _exigir(
        parser.has_section("console_scripts"),
        "falta la sección console_scripts",
    )
    return dict(parser["console_scripts"])


def _entrypoints_instalados(distribucion):
    return {
        punto.name: punto.value
        for punto in distribucion.entry_points
        if punto.group == "console_scripts"
    }


def validar_wheel(directorio, pyproject):
    """Inspecciona estructura, metadata y payload del único wheel construido."""
    directorio = Path(directorio)
    pyproject = Path(pyproject).resolve()
    proyecto = _cargar_proyecto(pyproject)
    ruedas = sorted(directorio.glob("*.whl"))
    _exigir(
        len(ruedas) == 1,
        f"se esperaba exactamente un wheel; encontrados: {len(ruedas)}",
    )

    nombre = _normalizar_distribucion(proyecto["name"])
    version = _normalizar_version(proyecto["version"])
    esperado = f"{nombre}-{version}-py3-none-any.whl"
    wheel = ruedas[0]
    _exigir(wheel.name == esperado, f"filename inesperado: {wheel.name}")

    dist_info = f"{nombre}-{version}.dist-info"
    raiz = pyproject.parent
    with zipfile.ZipFile(wheel) as archivo:
        corrupto = archivo.testzip()
        _exigir(corrupto is None, f"miembro corrupto: {corrupto}")
        miembros = set(archivo.namelist())

        modulos = {
            str(ruta.relative_to(raiz))
            for ruta in (raiz / "parlar").glob("*.py")
        }
        faltantes = sorted(modulos - miembros)
        _exigir(not faltantes, f"módulos ausentes: {', '.join(faltantes)}")

        metadata = BytesParser(policy=policy.default).parsebytes(
            _leer_miembro(archivo, f"{dist_info}/METADATA")
        )
        campos = {
            "Name": proyecto["name"],
            "Version": proyecto["version"],
            "Requires-Python": proyecto["requires-python"],
            "License-Expression": proyecto["license"],
        }
        for campo, valor in campos.items():
            _exigir(
                metadata.get(campo) == valor,
                f"{campo} inesperado: {metadata.get(campo)!r}",
            )

        licencias = proyecto["license-files"]
        _exigir(licencias == ["LICENSE"], "license-files inesperado")
        _exigir(
            metadata.get_all("License-File", []) == licencias,
            "License-File no coincide con pyproject.toml",
        )
        licencia = _leer_miembro(
            archivo,
            f"{dist_info}/licenses/LICENSE",
        )
        _exigir(
            licencia == (raiz / "LICENSE").read_bytes(),
            "LICENSE empaquetado no coincide con el repositorio",
        )

        entrypoints = _entrypoints_desde_bytes(
            _leer_miembro(archivo, f"{dist_info}/entry_points.txt")
        )
        _exigir(
            entrypoints == proyecto["scripts"],
            f"entrypoints inesperados: {entrypoints!r}",
        )

    print(f"wheel={wheel}")
    print(f"metadata={proyecto['name']} {proyecto['version']}")
    print(f"modules={len(modulos)} license=LICENSE entrypoints=parlar,parlarctl")
    return wheel


def validar_instalacion(pyproject, source_root):
    """Prueba el paquete instalado sin importar desde el checkout."""
    pyproject = Path(pyproject).resolve()
    source_root = Path(source_root).resolve()
    proyecto = _cargar_proyecto(pyproject)
    cwd = Path.cwd().resolve()
    _exigir(
        not cwd.is_relative_to(source_root),
        f"el smoke corre dentro del checkout: {cwd}",
    )
    rutas_checkout = [
        entrada for entrada in sys.path
        if entrada and Path(entrada).resolve().is_relative_to(source_root)
    ]
    _exigir(
        not rutas_checkout,
        f"sys.path contiene el checkout: {rutas_checkout!r}",
    )

    import parlar

    modulo = Path(parlar.__file__).resolve()
    _exigir(
        not modulo.is_relative_to(source_root),
        f"ParlAR se importó desde el checkout: {modulo}",
    )
    _exigir(
        modulo.is_relative_to(Path(sys.prefix).resolve()),
        f"ParlAR no pertenece al venv del smoke: {modulo}",
    )

    distribucion = importlib.metadata.distribution(proyecto["name"])
    _exigir(
        distribucion.version == proyecto["version"],
        f"versión instalada inesperada: {distribucion.version}",
    )
    entrypoints = _entrypoints_instalados(distribucion)
    _exigir(
        entrypoints == proyecto["scripts"],
        f"entrypoints instalados inesperados: {entrypoints!r}",
    )
    scripts = Path(sysconfig.get_path("scripts"))
    faltantes = sorted(
        nombre for nombre in entrypoints if not (scripts / nombre).is_file()
    )
    _exigir(not faltantes, f"console scripts ausentes: {', '.join(faltantes)}")

    print(f"cwd={cwd}")
    print(f"sys.path={json.dumps(sys.path, ensure_ascii=False)}")
    print(f"parlar.__file__={modulo}")
    print(f"version={distribucion.version}")
    print(f"entrypoints={','.join(sorted(entrypoints))}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="comando", required=True)

    wheel = subparsers.add_parser("wheel", help="valida una wheel construida")
    wheel.add_argument("directorio")
    wheel.add_argument("pyproject")

    installed = subparsers.add_parser(
        "installed",
        help="valida el paquete instalado en el intérprete actual",
    )
    installed.add_argument("pyproject")
    installed.add_argument("source_root")

    args = parser.parse_args(argv)
    try:
        if args.comando == "wheel":
            validar_wheel(args.directorio, args.pyproject)
        else:
            validar_instalacion(args.pyproject, args.source_root)
    except (ContractError, OSError, tomllib.TOMLDecodeError, zipfile.BadZipFile) as exc:
        print(f"wheel contract: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
