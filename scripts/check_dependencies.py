#!/usr/bin/env python3
"""Inventario y auditoría de dependencias de ParlAR para CI.

Subcomandos:

  entorno   Compara los paquetes del intérprete actual con constraints.txt.
            Falla si un paquete fijado quedó instalado con otra versión,
            salvo con ``--informativo``.
  auditar   Ejecuta pip-audit, sin instalar nada, sobre inventarios separados
            y publica un resumen clasificado.

Inventarios de ``auditar``:

  constraints  versiones enumeradas en constraints.txt;
  runtime      paquetes instalados en un venv de runtime CPU (dependencias de
               ParlAR, VAD opcional y pip del venv, con constraints.txt);
  extras       extras opcionales de pyproject.toml (vad, cuda) con la versión
               de constraints.txt;
  build        herramientas que construyen el release (setuptools, wheel).

Que una versión afectada aparezca en un inventario no implica que el aviso
sea explotable en el runtime de ParlAR: el reporte sólo dice dónde está.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PROYECTO = "parlar"
INVENTARIOS = ("constraints", "runtime", "extras", "build")
DESCRIPCION = {
    "constraints": "versiones enumeradas en constraints.txt",
    "runtime": "instaladas en el venv de runtime CPU",
    "extras": "extras opcionales (vad, cuda) según constraints.txt",
    "build": "herramientas de construcción del release",
}
_NOMBRE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_LISTAR = (
    "import importlib.metadata as m, json\n"
    "print(json.dumps({d.metadata['Name']: d.version"
    " for d in m.distributions() if d.metadata['Name']}))\n"
)


class ErrorAuditoria(RuntimeError):
    pass


def normalizar(nombre: str) -> str:
    return re.sub(r"[-_.]+", "-", nombre).lower()


def leer_constraints(ruta: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip()
        if not linea:
            continue
        nombre, separador, version = linea.partition("==")
        if not separador or not version or any(c in version for c in "<>=!~*,; "):
            raise ValueError(f"constraint sin versión exacta: {linea!r}")
        pins[normalizar(nombre)] = version.strip()
    return pins


def leer_pin(especificacion: str) -> tuple[str, str]:
    nombre, separador, version = especificacion.partition("==")
    if not separador or not nombre.strip() or not version.strip():
        raise ValueError(f"se esperaba nombre==versión: {especificacion!r}")
    return normalizar(nombre.strip()), version.strip()


def nombres_extras(pyproject: Path) -> set[str]:
    datos = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    extras = datos.get("project", {}).get("optional-dependencies", {})
    nombres = set()
    for requisitos in extras.values():
        for requisito in requisitos:
            coincidencia = _NOMBRE.match(requisito)
            if coincidencia:
                nombres.add(normalizar(coincidencia.group(1)))
    return nombres


def instalados(python: str | None = None) -> dict[str, str]:
    """Distribuciones instaladas en ``python`` (o en este intérprete)."""
    if python is None:
        import importlib.metadata as metadata
        crudo = {d.metadata["Name"]: d.version
                 for d in metadata.distributions() if d.metadata["Name"]}
    else:
        resultado = subprocess.run(
            [python, "-I", "-c", _LISTAR],
            capture_output=True, text=True, check=False)
        if resultado.returncode != 0:
            raise ErrorAuditoria(
                f"no se pudo listar {python}: {resultado.stderr.strip()[-500:]}")
        crudo = json.loads(resultado.stdout)
    return {normalizar(n): v for n, v in crudo.items()}


def escribir_resumen(texto: str) -> None:
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as archivo:
            archivo.write(texto)


def _celda(texto: str) -> str:
    return " ".join(str(texto).split()).replace("|", "\\|")[:200]


def _escapar_anotacion(texto: str) -> str:
    return (texto.replace("%", "%25").replace("\r", "%0D")
            .replace("\n", "%0A"))


def anotar(nivel: str, titulo: str, mensaje: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{nivel} title={_escapar_anotacion(titulo)}::"
              f"{_escapar_anotacion(mensaje)}")


# -- entorno -----------------------------------------------------------------

@dataclass
class Comparacion:
    coinciden: dict[str, str] = field(default_factory=dict)
    distintos: dict[str, tuple[str, str]] = field(default_factory=dict)
    fuera: dict[str, str] = field(default_factory=dict)


def comparar(entorno: dict[str, str], pins: dict[str, str]) -> Comparacion:
    comparacion = Comparacion()
    for nombre, version in sorted(entorno.items()):
        if nombre == PROYECTO:
            continue
        if nombre not in pins:
            comparacion.fuera[nombre] = version
        elif pins[nombre] == version:
            comparacion.coinciden[nombre] = version
        else:
            comparacion.distintos[nombre] = (version, pins[nombre])
    return comparacion


def resumen_entorno(comparacion: Comparacion, python: str) -> str:
    lineas = [
        "## Dependencias probadas frente a constraints.txt\n",
        f"Intérprete: `{python}`\n",
        f"- Coinciden con constraints.txt: {len(comparacion.coinciden)}",
        f"- Distintas de constraints.txt: {len(comparacion.distintos)}",
        f"- Fuera de constraints.txt: {len(comparacion.fuera)}\n",
    ]
    if comparacion.distintos:
        lineas += ["| Paquete | Instalada | constraints.txt |",
                   "| --- | --- | --- |"]
        lineas += [f"| {n} | {i} | {c} |"
                   for n, (i, c) in comparacion.distintos.items()]
        lineas.append("")
    if comparacion.fuera:
        lineas.append("Fuera de constraints.txt (herramientas del entorno o "
                      "dependencias sin fijar): "
                      + ", ".join(f"`{n}=={v}`"
                                  for n, v in comparacion.fuera.items()))
        lineas.append("")
    return "\n".join(lineas) + "\n"


def comando_entorno(args) -> int:
    pins = leer_constraints(args.constraints)
    comparacion = comparar(instalados(args.python), pins)
    texto = resumen_entorno(comparacion, args.python or sys.executable)
    print(texto)
    escribir_resumen(texto)
    if comparacion.distintos:
        nivel = "warning" if args.informativo else "error"
        for nombre, (instalada, fijada) in comparacion.distintos.items():
            anotar(nivel, "constraints.txt",
                   f"{nombre} instalado {instalada}; constraints.txt fija {fijada}")
        return 0 if args.informativo else 1
    return 0


# -- auditar -----------------------------------------------------------------

@dataclass
class Hallazgo:
    paquete: str
    version: str
    aviso: str
    alias: list[str]
    corregida: list[str]
    inventarios: set[str] = field(default_factory=set)


@dataclass
class EstadoInventario:
    paquetes: int
    estado: str = "sin ejecutar"
    vulnerables: int = 0
    omitidos: list[str] = field(default_factory=list)


def inventarios(pins: dict[str, str], runtime: dict[str, str] | None,
                extras: set[str], build: dict[str, str]
                ) -> dict[str, dict[str, str]]:
    resultado = {"constraints": dict(pins)}
    if runtime is not None:
        resultado["runtime"] = {n: v for n, v in runtime.items()
                                if n != PROYECTO}
    resultado["extras"] = {n: pins[n] for n in sorted(extras) if n in pins}
    resultado["build"] = dict(build)
    return resultado


def ejecutar_pip_audit(pip_audit: str, requisitos: Path, salida: Path) -> dict:
    comando = [pip_audit, "--progress-spinner", "off", "--no-deps",
               "--disable-pip", "-r", str(requisitos), "-f", "json",
               "-o", str(salida)]
    resultado = subprocess.run(comando, capture_output=True, text=True,
                               check=False)
    # pip-audit devuelve 1 cuando encuentra avisos: el éxito se decide por el
    # JSON escrito, no por el código de salida.
    try:
        datos = json.loads(salida.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        datos = None
    if resultado.returncode not in (0, 1) or not isinstance(datos, dict) \
            or not isinstance(datos.get("dependencies"), list):
        detalle = (resultado.stderr or resultado.stdout).strip()[-1500:]
        raise ErrorAuditoria(
            f"pip-audit terminó con {resultado.returncode}: {detalle}")
    return datos


def auditar(inventario: dict[str, dict[str, str]], pip_audit: str,
            salida: Path) -> tuple[dict[str, EstadoInventario], list[Hallazgo]]:
    salida.mkdir(parents=True, exist_ok=True)
    estados: dict[str, EstadoInventario] = {}
    hallazgos: dict[tuple[str, str, str], Hallazgo] = {}
    for nombre in INVENTARIOS:
        if nombre not in inventario:
            continue
        paquetes = inventario[nombre]
        estado = EstadoInventario(paquetes=len(paquetes))
        estados[nombre] = estado
        if not paquetes:
            estado.estado = "vacío"
            continue
        requisitos = salida / f"{nombre}.txt"
        requisitos.write_text(
            "".join(f"{n}=={v}\n" for n, v in sorted(paquetes.items())),
            encoding="utf-8")
        try:
            datos = ejecutar_pip_audit(pip_audit, requisitos,
                                       salida / f"{nombre}.json")
        except ErrorAuditoria as exc:
            estado.estado = f"error: {exc}"
            continue
        estado.estado = "ok"
        for dependencia in datos["dependencies"]:
            paquete = normalizar(str(dependencia.get("name", "")))
            version = str(dependencia.get("version", ""))
            if dependencia.get("skip_reason"):
                estado.omitidos.append(
                    f"{paquete}: {dependencia['skip_reason']}")
                continue
            avisos = dependencia.get("vulns") or []
            if avisos:
                estado.vulnerables += 1
            for aviso in avisos:
                clave = (paquete, version, str(aviso.get("id")))
                hallazgo = hallazgos.get(clave)
                if hallazgo is None:
                    hallazgo = hallazgos[clave] = Hallazgo(
                        paquete, version, clave[2],
                        sorted(set(aviso.get("aliases") or [])),
                        list(aviso.get("fix_versions") or []))
                hallazgo.inventarios.add(nombre)
    # La misma versión puede figurar en otro inventario sin haberse auditado
    # allí (por ejemplo extras ⊂ constraints): se marca igual su presencia.
    for hallazgo in hallazgos.values():
        for nombre, paquetes in inventario.items():
            if paquetes.get(hallazgo.paquete) == hallazgo.version:
                hallazgo.inventarios.add(nombre)
    ordenados = sorted(hallazgos.values(),
                       key=lambda h: (h.paquete, h.version, h.aviso))
    return estados, ordenados


def resumen_auditoria(estados: dict[str, EstadoInventario],
                      hallazgos: list[Hallazgo], estricto: bool) -> str:
    modo = "bloqueante" if estricto else "informativo"
    lineas = [
        f"## Auditoría de dependencias con pip-audit ({modo})\n",
        "| Inventario | Alcance | Paquetes | Vulnerables | Omitidos | Estado |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for nombre, estado in estados.items():
        lineas.append(
            f"| {nombre} | {DESCRIPCION[nombre]} | {estado.paquetes} | "
            f"{estado.vulnerables} | {len(estado.omitidos)} | "
            f"{_celda(estado.estado)} |")
    lineas.append("")
    if hallazgos:
        lineas += [
            f"**{len(hallazgos)} avisos** en "
            f"{len({(h.paquete, h.version) for h in hallazgos})} versiones "
            "de paquetes.\n",
            "| Paquete | Versión | Aviso | Alias | Corregida en | Presente en |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for h in hallazgos:
            lineas.append(
                f"| {h.paquete} | {h.version} | {h.aviso} | "
                f"{', '.join(h.alias) or '-'} | "
                f"{', '.join(h.corregida) or 'sin corrección'} | "
                f"{', '.join(n for n in INVENTARIOS if n in h.inventarios)} |")
        lineas += [
            "",
            "Presencia en un inventario no implica que el aviso sea "
            "explotable en el runtime de ParlAR. El análisis de impacto está "
            "en `docs/security/supply-chain.md`.",
        ]
    else:
        lineas.append("Sin avisos conocidos en los inventarios auditados.")
    omitidos = [(n, o) for n, e in estados.items() for o in e.omitidos]
    if omitidos:
        lineas += ["", "Paquetes que pip-audit no pudo auditar:"]
        lineas += [f"- {n}: {_celda(o)}" for n, o in omitidos]
    return "\n".join(lineas) + "\n"


def comando_auditar(args) -> int:
    pins = leer_constraints(args.constraints)
    runtime = instalados(args.runtime_python) if args.runtime_python else None
    build = dict(leer_pin(p) for p in args.build)
    inventario = inventarios(pins, runtime, nombres_extras(args.pyproject),
                             build)
    estados, hallazgos = auditar(inventario, args.pip_audit, args.salida)

    reporte = {
        "inventarios": {n: {"paquetes": inventario[n],
                            "estado": e.estado,
                            "omitidos": e.omitidos}
                        for n, e in estados.items()},
        "hallazgos": [{"paquete": h.paquete, "version": h.version,
                       "aviso": h.aviso, "alias": h.alias,
                       "corregida": h.corregida,
                       "inventarios": sorted(h.inventarios)}
                      for h in hallazgos],
    }
    (args.salida / "reporte.json").write_text(
        json.dumps(reporte, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    texto = resumen_auditoria(estados, hallazgos, args.estricto)
    print(texto)
    escribir_resumen(texto)

    por_version: dict[tuple[str, str], list[Hallazgo]] = {}
    for h in hallazgos:
        por_version.setdefault((h.paquete, h.version), []).append(h)
    nivel = "error" if args.estricto else "warning"
    for (paquete, version), grupo in por_version.items():
        corregidas = sorted({c for h in grupo for c in h.corregida})
        presentes = sorted({n for h in grupo for n in h.inventarios})
        anotar(nivel, f"pip-audit: {paquete} {version}",
               f"{len(grupo)} aviso{'' if len(grupo) == 1 else 's'} "
               f"({', '.join(h.aviso for h in grupo)}); "
               f"corregida en {', '.join(corregidas) or 'sin corrección'}; "
               f"presente en {', '.join(presentes)}")

    errores = [n for n, e in estados.items() if e.estado.startswith("error")]
    for nombre in errores:
        anotar("error", "pip-audit",
               f"no se pudo auditar el inventario {nombre}")
    if errores:
        return 2
    if hallazgos and args.estricto:
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="comando", required=True)

    entorno = sub.add_parser("entorno", help="comparar con constraints.txt")
    entorno.add_argument("--constraints", type=Path,
                         default=ROOT / "constraints.txt")
    entorno.add_argument("--python", help="intérprete a inspeccionar "
                         "(por defecto el actual)")
    entorno.add_argument("--informativo", action="store_true",
                         help="avisar las diferencias sin fallar")
    entorno.set_defaults(funcion=comando_entorno)

    auditar_ = sub.add_parser("auditar", help="pip-audit por inventario")
    auditar_.add_argument("--constraints", type=Path,
                          default=ROOT / "constraints.txt")
    auditar_.add_argument("--pyproject", type=Path,
                          default=ROOT / "pyproject.toml")
    auditar_.add_argument("--runtime-python",
                          help="python del venv de runtime a inventariar")
    auditar_.add_argument("--build", action="append", default=[],
                          metavar="NOMBRE==VERSION",
                          help="herramienta de build del release")
    auditar_.add_argument("--pip-audit", default="pip-audit")
    auditar_.add_argument("--salida", type=Path, required=True)
    auditar_.add_argument("--estricto", action="store_true",
                          help="fallar si hay avisos (por defecto informa)")
    auditar_.set_defaults(funcion=comando_auditar)

    args = parser.parse_args(argv)
    try:
        return args.funcion(args)
    except (ErrorAuditoria, ValueError) as exc:
        print(f"!! {exc}", file=sys.stderr)
        anotar("error", "dependencias", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
