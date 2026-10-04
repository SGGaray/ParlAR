#!/usr/bin/env python3
"""Instalador de usuario de ParlAR con versiones lado a lado y swap atómico.

Lo invoca ``install.sh`` con un Python ya validado. Sirve a dos orígenes:

* release extraído: ``VERSION`` + ``wheels/`` + ``share/``; instala sólo
  wheels (nunca compila) con ``constraints.txt``;
* checkout de mantenedor: construye ParlAR desde el árbol de fuentes.

Layout administrado bajo ``$XDG_DATA_HOME/parlar``::

    versions/<versión>-<id>/venv/   una instalación completa e inmutable
    current -> versions/<...>       symlink relativo, reemplazo atómico
    installed.json                  metadata mínima, escritura atómica
    uninstall.sh                    desinstalador independiente del release
    sesiones/                       datos del usuario: nunca se tocan

Todo lo previo al swap ocurre en un directorio nuevo. Si algo falla, ese
directorio se elimina y ``current``, enlaces y launcher quedan como estaban.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, field
import datetime as _dt
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tomllib

APP = "ParlAR"
VERSIONES_CONSERVADAS = 2  # current + previous, para no romper una instancia abierta
MARCA_INCOMPLETA = ".parlar-incomplete"
MARCA_VERSION = ".parlar-version"
NOMBRE_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+]*-[0-9a-f]{8}$")
LEGACY_VENV = "venv"
MIB = 1024 * 1024
# Mínimos libres en la raíz de instalación, medidos sobre el pico real: el
# venv CPU ronda 450 MiB; con CUDA, pip descarga ~1.5 GiB de wheels NVIDIA y
# los desempaqueta en ~2.3 GiB mientras todavía existen los temporales.
ESPACIO_CPU = 2048 * MIB
ESPACIO_CUDA = 6144 * MIB
# Antes del pip CUDA ya está instalada la base: lo que tiene que quedar libre.
ESPACIO_CUDA_RESTANTE = ESPACIO_CUDA - ESPACIO_CPU
# Una instalación no conserva descargas en ~/.cache/pip (cientos de MB o GB).
PIP_SIN_CACHE = "--no-cache-dir"
DRIVER_NVIDIA = (Path("/proc/driver/nvidia/version"), Path("/dev/nvidiactl"))
_SIN_ESPACIO = re.compile(
    r"Errno 28|Errno 122|No space left on device|Disk quota exceeded",
    re.IGNORECASE)
COMANDOS = ("parlar", "parlarctl")
COMANDO_UNINSTALL = "parlar-uninstall"
MODULOS_VALIDADOS = (
    "parlar", "parlar.__main__", "parlar.config", "parlar.control",
    "faster_whisper", "ctranslate2", "numpy", "evdev",
)


class ErrorInstalacion(RuntimeError):
    """Fallo controlado: el mensaje es para la persona usuaria."""


class ErrorSinEspacio(ErrorInstalacion):
    """El disco o la cuota se llenaron mientras se instalaban dependencias."""

    def __init__(self, detalle: str):
        super().__init__(detalle)
        self.detalle = detalle


def es_sin_espacio(exc: BaseException) -> bool:
    if isinstance(exc, ErrorSinEspacio):
        return True
    return isinstance(exc, OSError) and exc.errno in {
        errno.ENOSPC, errno.EDQUOT}


def mensaje_sin_espacio(previa: bool, detalle: str) -> str:
    estado = ("La versión anterior sigue intacta." if previa
              else "No quedó nada instalado a medias.")
    return ("La instalación se quedó sin espacio durante la instalación de "
            f"dependencias.\n{estado}\nDetalle técnico: {detalle}")


def info(mensaje: str) -> None:
    print(f"==> {mensaje}", flush=True)


def aviso(mensaje: str) -> None:
    print(f"!! {mensaje}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Origen: release extraído o checkout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Origen:
    modo: str
    raiz: Path
    version: str
    share: Path
    icono: Path
    constraints: Path
    uninstall: Path
    wheels: Path | None = None

    def objetivo(self, extras: str = "") -> str:
        if self.modo == "release":
            return f"parlar{extras}=={self.version}"
        return f"{self.raiz}{extras}"


def detectar_origen(raiz: Path) -> Origen:
    raiz = raiz.resolve()
    if ((raiz / "VERSION").is_file() and (raiz / "wheels").is_dir()
            and (raiz / "share").is_dir()):
        version = (raiz / "VERSION").read_text(encoding="utf-8").strip()
        share = raiz / "share"
        wheel = raiz / "wheels" / f"parlar-{version}-py3-none-any.whl"
        if not wheel.is_file():
            raise ErrorInstalacion(
                f"el release no contiene {wheel.name}; descargalo de nuevo")
        return Origen(
            modo="release", raiz=raiz, version=version, share=share,
            icono=share / "parlar.svg",
            constraints=raiz / "constraints.txt",
            uninstall=raiz / "uninstall.sh",
            wheels=raiz / "wheels",
        )
    pyproject = raiz / "pyproject.toml"
    if pyproject.is_file() and (raiz / "parlar" / "__init__.py").is_file():
        proyecto = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return Origen(
            modo="checkout", raiz=raiz,
            version=proyecto["project"]["version"],
            share=raiz / "scripts",
            icono=raiz / "parlar" / "assets" / "parlar.svg",
            constraints=raiz / "constraints.txt",
            uninstall=raiz / "uninstall.sh",
        )
    raise ErrorInstalacion(
        f"{raiz} no es un release de ParlAR ni un checkout del proyecto")


# ---------------------------------------------------------------------------
# Destino: rutas XDG del usuario
# ---------------------------------------------------------------------------


def _sin_separadores_finales(valor: str) -> str:
    while valor != "/" and valor.endswith("/"):
        valor = valor[:-1]
    return valor


def validar_install_home(original: str, home: str) -> Path:
    """Misma regla léxica que ``uninstall.sh``: nunca una raíz amplia."""
    valor = _sin_separadores_finales(original)
    partes = valor.split("/")
    if "." in partes or ".." in partes:
        raise ErrorInstalacion(f"ruta de instalación insegura: {original}")
    ruta = Path(valor)
    if (not ruta.is_absolute() or ruta.name != "parlar"
            or str(ruta.parent) == "/"):
        raise ErrorInstalacion(f"ruta de instalación insegura: {original}")
    if valor in {"/", _sin_separadores_finales(home)}:
        raise ErrorInstalacion(f"ruta de instalación insegura: {valor}")
    return ruta


@dataclass(frozen=True)
class Destino:
    home: Path
    install_home: Path
    bin_dir: Path
    config_base: Path
    desktop: Path
    icono: Path
    unit: Path
    autostart: Path
    legacy_enable: Path

    @classmethod
    def desde_entorno(cls, entorno=os.environ) -> "Destino":
        home = entorno.get("HOME", "")
        if not home or not os.path.isabs(home):
            raise ErrorInstalacion("HOME no está definido como ruta absoluta")
        data = Path(entorno.get("XDG_DATA_HOME") or f"{home}/.local/share")
        config = Path(entorno.get("XDG_CONFIG_HOME") or f"{home}/.config")
        install_home = validar_install_home(
            entorno.get("PARLAR_INSTALL_HOME") or str(data / "parlar"), home)
        return cls(
            home=Path(home),
            install_home=install_home,
            bin_dir=Path(entorno.get("PARLAR_BIN_DIR") or f"{home}/.local/bin"),
            config_base=config,
            desktop=data / "applications" / "parlar.desktop",
            icono=data / "icons" / "hicolor" / "scalable" / "apps" / "parlar.svg",
            unit=config / "systemd" / "user" / "parlar.service",
            autostart=config / "autostart" / "parlar-systemd.desktop",
            legacy_enable=(config / "systemd" / "user"
                           / "default.target.wants" / "parlar.service"),
        )

    @property
    def versions(self) -> Path:
        return self.install_home / "versions"

    @property
    def current(self) -> Path:
        return self.install_home / "current"

    @property
    def legacy_venv(self) -> Path:
        return self.install_home / LEGACY_VENV

    @property
    def metadata(self) -> Path:
        return self.install_home / "installed.json"

    @property
    def uninstall(self) -> Path:
        return self.install_home / "uninstall.sh"

    def ejecutable_estable(self, comando: str) -> Path:
        """Ruta que nunca cambia entre versiones: atraviesa ``current``."""
        return self.current / "venv" / "bin" / comando

    def destinos_enlaces(self) -> dict[Path, Path]:
        enlaces = {
            self.bin_dir / comando: self.ejecutable_estable(comando)
            for comando in COMANDOS
        }
        enlaces[self.bin_dir / COMANDO_UNINSTALL] = self.uninstall
        return enlaces

    def es_target_propio(self, target: str, nombre: str) -> bool:
        """Reconoce targets de 1.1+ y del layout legacy 1.0.x."""
        normalizado = re.sub(r"/+", "/", target)
        raiz = str(self.install_home)
        if nombre == COMANDO_UNINSTALL:
            return normalizado == f"{raiz}/uninstall.sh"
        return normalizado in {
            f"{raiz}/current/venv/bin/{nombre}",
            f"{raiz}/{LEGACY_VENV}/bin/{nombre}",
        }


# ---------------------------------------------------------------------------
# Herramientas externas (sustituibles en tests)
# ---------------------------------------------------------------------------


class Herramientas:
    """Única frontera con venv/pip/subprocesos del entorno nuevo.

    Todo corre con cwd ``/``: un checkout o release en el directorio actual
    no debe colarse en ``sys.path`` ni en la metadata que se valida.
    """

    def crear_venv(self, destino: Path) -> None:
        subprocess.run([sys.executable, "-m", "venv", str(destino)],
                       check=True, cwd="/")

    def nvidia_presente(self) -> bool:
        """Sonda previa al venv: hay driver NVIDIA cargado en el sistema."""
        return any(ruta.exists() for ruta in DRIVER_NVIDIA)

    def pip_install(self, python: Path, argumentos: list[str]) -> bool:
        """Corre pip mostrando su salida; detecta disco o cuota llenos."""
        comando = [
            str(python), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", *argumentos,
        ]
        proceso = subprocess.Popen(
            comando, cwd="/", stderr=subprocess.PIPE, text=True,
            errors="replace")
        sin_espacio = None
        for linea in proceso.stderr:
            sys.stderr.write(linea)
            if sin_espacio is None and _SIN_ESPACIO.search(linea):
                sin_espacio = linea.strip()
        if proceso.wait() == 0:
            return True
        if sin_espacio is not None:
            raise ErrorSinEspacio(sin_espacio)
        return False

    def ejecutar(self, comando: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            comando, check=False, text=True, capture_output=True, timeout=300,
            cwd="/")


# ---------------------------------------------------------------------------
# Operaciones atómicas
# ---------------------------------------------------------------------------


def escribir_atomico(destino: Path, contenido: bytes, modo: int) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(f".{destino.name}.tmp-{secrets.token_hex(4)}")
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, modo)
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.chmod(temporal, modo)
        os.replace(temporal, destino)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporal.unlink()
        raise


def symlink_atomico(destino: Path, target: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(f".{destino.name}.tmp-{secrets.token_hex(4)}")
    os.symlink(target, temporal)
    try:
        os.replace(temporal, destino)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporal.unlink()
        raise


def borrar_arbol(ruta: Path) -> None:
    """Borra un directorio propio sin seguir un symlink en su lugar."""
    if ruta.is_symlink():
        ruta.unlink()
    elif ruta.is_dir():
        shutil.rmtree(ruta)


# ---------------------------------------------------------------------------
# Estado existente
# ---------------------------------------------------------------------------


@dataclass
class EstadoPrevio:
    current: str | None = None  # "versions/<...>" vigente
    legacy: bool = False  # venv 1.0.x sin current


def leer_estado(destino: Destino) -> EstadoPrevio:
    estado = EstadoPrevio()
    if destino.current.is_symlink():
        target = os.readlink(destino.current)
        partes = target.split("/")
        if (len(partes) == 2 and partes[0] == "versions"
                and NOMBRE_VERSION.match(partes[1])):
            if (destino.install_home / target).is_dir():
                estado.current = target
            else:
                aviso(f"{destino.current} apunta a una versión ausente; "
                      "se reemplaza")
        else:
            raise ErrorInstalacion(
                f"{destino.current} apunta a un destino no administrado: {target}")
    elif os.path.lexists(destino.current):
        raise ErrorInstalacion(
            f"{destino.current} existe y no es un enlace de ParlAR")
    venv = destino.legacy_venv
    if venv.is_dir() and not venv.is_symlink() and estado.current is None:
        estado.legacy = (venv / "bin" / "python").exists()
    return estado


def verificar_propiedad(destino: Destino, servicio: bool,
                        marcas: dict[str, str]) -> None:
    """Falla antes de tocar nada si algún destino pertenece a otro programa."""
    for enlace in destino.destinos_enlaces():
        if not os.path.lexists(enlace):
            continue
        if not enlace.is_symlink() or not destino.es_target_propio(
                os.readlink(enlace), enlace.name):
            raise ErrorInstalacion(
                f"{enlace} existe y no pertenece a esta instalación")
    revisar = [(destino.desktop, marcas["desktop"]),
               (destino.icono, marcas["icono"])]
    if servicio:
        revisar.append((destino.unit, marcas["unit"]))
        revisar.append((destino.autostart, marcas["desktop"]))
    for ruta, marca in revisar:
        if not os.path.lexists(ruta):
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            contenido = ""
        if ruta.is_symlink() or not contenido.startswith(marca):
            raise ErrorInstalacion(
                f"{ruta} ya existe y no fue generado por ParlAR; no se reemplaza")
    if os.path.lexists(destino.uninstall) and not destino.uninstall.is_file():
        raise ErrorInstalacion(f"{destino.uninstall} no es un archivo regular")


def es_generado(ruta: Path, marca: str) -> bool:
    if ruta.is_symlink() or not ruta.is_file():
        return False
    try:
        return ruta.read_text(encoding="utf-8").startswith(marca)
    except (OSError, UnicodeDecodeError):
        return False


def verificar_espacio(ruta: Path, minimo: int, *, cuda: bool = False) -> None:
    existente = ruta
    while not existente.exists():
        existente = existente.parent
    libre = shutil.disk_usage(existente).free
    if libre >= minimo:
        return
    mensaje = (f"Espacio insuficiente en {existente}: hay {libre // MIB} MiB "
               f"libres y se necesitan al menos {minimo // MIB} MiB.")
    if cuda:
        mensaje += (
            "\nCon GPU NVIDIA, ParlAR instala además los runtimes CUDA, que "
            "ocupan varios GB. Liberá espacio y volvé a ejecutar el "
            "instalador, o instalá sólo para CPU con ./install.sh --cpu-only "
            f"(necesita {ESPACIO_CPU // MIB} MiB libres).")
    raise ErrorInstalacion(mensaje)


# ---------------------------------------------------------------------------
# Instalación
# ---------------------------------------------------------------------------


@dataclass
class Opciones:
    servicio: bool = False
    precargar_modelo: bool = False
    solo_cpu: bool = False


@dataclass
class Resultado:
    version_dir: str
    previous: str | None
    cuda: bool
    avisos: list[str] = field(default_factory=list)


class Instalador:
    def __init__(self, origen: Origen, destino: Destino, opciones: Opciones,
                 herramientas: Herramientas | None = None,
                 renderers=None):
        self.origen = origen
        self.destino = destino
        self.opciones = opciones
        self.h = herramientas or Herramientas()
        self.render_desktop, self.render_service = (
            renderers or cargar_renderers(origen.share))

    # -- pip -------------------------------------------------------------

    def _argumentos_pip(self) -> list[str]:
        argumentos = [PIP_SIN_CACHE, "-c", str(self.origen.constraints)]
        if self.origen.modo == "release":
            # Sin compilación: todo lo nativo viene en wheels/ o como wheel
            # de PyPI. Un Python sin wheel compatible falla aquí, antes del swap.
            argumentos += ["--only-binary=:all:",
                           "--find-links", str(self.origen.wheels)]
        return argumentos

    def _pip(self, python: Path, *paquetes: str) -> bool:
        return self.h.pip_install(python, [*self._argumentos_pip(), *paquetes])

    # -- staging ---------------------------------------------------------

    def _preparar(self, version_dir: Path) -> bool:
        """Construye y valida una versión completa. Devuelve si usa CUDA."""
        venv = version_dir / "venv"
        python = venv / "bin" / "python"
        info(f"Creando entorno aislado {version_dir.name}")
        self.h.crear_venv(venv)

        info(f"Instalando ParlAR {self.origen.version}")
        if not self._pip(python, self.origen.objetivo()):
            raise ErrorInstalacion("pip no pudo instalar ParlAR")

        cuda = False
        if self.opciones.solo_cpu:
            info("Instalación CPU solicitada; se omiten runtimes NVIDIA")
        elif self.h.ejecutar([
                str(python), "-I", "-c",
                "import ctranslate2, sys; "
                "sys.exit(0 if ctranslate2.get_cuda_device_count() > 0 else 1)",
        ]).returncode == 0:
            info("GPU CUDA detectada por CTranslate2; instalando runtime NVIDIA")
            # El preflight ya exigió el mínimo CUDA si la sonda vio el driver;
            # esto cubre una GPU que sólo CTranslate2 detectó.
            verificar_espacio(version_dir, ESPACIO_CUDA_RESTANTE, cuda=True)
            if not self._pip(python, self.origen.objetivo("[cuda]")):
                raise ErrorInstalacion("pip no pudo instalar el runtime NVIDIA")
            cuda = True
        else:
            info("CTranslate2 no detectó GPU CUDA; ParlAR usará CPU")

        info("Instalando WebRTC VAD")
        if not self._pip(python, "webrtcvad-wheels"):
            aviso("WebRTC VAD no disponible; se usará el VAD de energía probado")

        info("Validando la instalación nueva")
        codigo = (
            "import importlib, importlib.metadata as m\n"
            f"for nombre in {MODULOS_VALIDADOS!r}:\n"
            "    importlib.import_module(nombre)\n"
            f"print(m.version({APP!r}))\n"
        )
        resultado = self.h.ejecutar([str(python), "-I", "-c", codigo])
        if resultado.returncode != 0:
            raise ErrorInstalacion(
                "la instalación nueva no importa correctamente:\n"
                + resultado.stderr.strip()[-2000:])
        if resultado.stdout.strip() != self.origen.version:
            raise ErrorInstalacion(
                f"metadata instalada inesperada: {resultado.stdout.strip()!r}")
        resultado = self.h.ejecutar([str(venv / "bin" / "parlar"), "--version"])
        esperado = f"{APP} {self.origen.version}"
        if resultado.returncode != 0 or resultado.stdout.strip() != esperado:
            raise ErrorInstalacion(
                f"parlar --version devolvió {resultado.stdout.strip()!r}; "
                f"se esperaba {esperado!r}")
        for comando in COMANDOS:
            if not os.access(venv / "bin" / comando, os.X_OK):
                raise ErrorInstalacion(f"falta el comando {comando} en el venv")
        return cuda

    def _avisos_sistema(self, python: Path) -> list[str]:
        resultado = self.h.ejecutar(
            [str(python), "-I", "-c", "import sounddevice"])
        if resultado.returncode != 0:
            return ["PortAudio no está disponible: instalá el paquete "
                    "libportaudio2 (Debian/Ubuntu) o portaudio (Fedora) "
                    "para usar el micrófono."]
        return []

    # -- publicación -----------------------------------------------------

    def _publicar_enlaces(self) -> None:
        for enlace, target in self.destino.destinos_enlaces().items():
            if enlace.is_symlink() and os.readlink(enlace) == str(target):
                continue
            symlink_atomico(enlace, str(target))

    def _publicar_escritorio(self, servicio_existente: bool) -> bool:
        rd, rs = self.render_desktop, self.render_service
        rd.instalar_icono(rd.cargar_icono(self.origen.icono), self.destino.icono)
        estable = self.destino.ejecutable_estable("parlar")
        rd.instalar(rd.renderizar(estable), self.destino.desktop)
        recargar = False
        if self.opciones.servicio or servicio_existente:
            rs.instalar(
                rs.renderizar_instalacion(self.destino.install_home, estable),
                self.destino.unit)
            recargar = True
        if self.opciones.servicio:
            rd.instalar(rd.renderizar_autostart(), self.destino.autostart)
        info(f"Launcher en el menú: {self.destino.desktop}")
        self._refrescar_menu()
        return recargar

    def _refrescar_menu(self) -> None:
        """Cachés de menú e íconos: opcionales según el escritorio."""
        for comando in (
                ["update-desktop-database", str(self.destino.desktop.parent)],
                ["gtk-update-icon-cache", "-q", "-t",
                 str(self.destino.icono.parents[2])]):
            if shutil.which(comando[0]):
                self.h.ejecutar(comando)

    def _systemd(self, recargar: bool) -> None:
        if not shutil.which("systemctl"):
            return
        if self.opciones.servicio:
            # Upgrade desde la unit antigua habilitada en default.target. La
            # unit nueva es static y el login gráfico es dueño del autostart.
            self.h.ejecutar(["systemctl", "--user", "disable", "parlar.service"])
        if self.opciones.servicio and self.destino.legacy_enable.is_symlink():
            self.destino.legacy_enable.unlink()
        if recargar and self.h.ejecutar(
                ["systemctl", "--user", "daemon-reload"]).returncode != 0:
            aviso("daemon-reload queda pendiente en la sesión gráfica")

    def _instalar_uninstall(self) -> None:
        escribir_atomico(
            self.destino.uninstall, self.origen.uninstall.read_bytes(), 0o755)

    def _escribir_metadata(self, version_dir: str, previous: str | None) -> None:
        datos = {
            "app": APP,
            "version": self.origen.version,
            "current": version_dir,
            "previous": previous,
            "installed_at": _dt.datetime.now(_dt.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        contenido = (json.dumps(datos, indent=2) + "\n").encode("utf-8")
        escribir_atomico(self.destino.metadata, contenido, 0o644)

    def _podar(self, conservar: set[str]) -> None:
        versions = self.destino.versions
        for entrada in sorted(versions.iterdir()):
            relativo = f"versions/{entrada.name}"
            if relativo in conservar:
                continue
            if entrada.is_symlink() or not entrada.is_dir():
                continue
            if NOMBRE_VERSION.match(entrada.name) and (
                    (entrada / MARCA_VERSION).is_file()
                    or (entrada / MARCA_INCOMPLETA).is_file()):
                info(f"Eliminando versión antigua {entrada.name}")
                shutil.rmtree(entrada)
        if LEGACY_VENV not in conservar and os.path.lexists(
                self.destino.legacy_venv):
            info("Eliminando el entorno de la instalación 1.0.x")
            borrar_arbol(self.destino.legacy_venv)

    # -- flujo -----------------------------------------------------------

    def instalar(self) -> Resultado:
        d = self.destino
        marcas = {
            "desktop": self.render_desktop.MARCA,
            "icono": self.render_desktop.MARCA_ICONO,
            "unit": self.render_service.MARCA,
        }
        # 1-2. Validaciones sin efectos: Python, rutas, ownership y espacio.
        verificar_propiedad(d, self.opciones.servicio, marcas)
        # Una unit propia de 1.0.x apunta al venv legacy: se re-renderiza
        # hacia current aunque esta ejecución no pida --install-service.
        servicio_existente = es_generado(d.unit, marcas["unit"])
        if d.install_home.is_symlink():
            raise ErrorInstalacion(
                f"la raíz de instalación es un enlace simbólico: {d.install_home}")
        # El pico de CUDA se decide antes de crear nada: la sonda del driver
        # no necesita el venv. La elección CPU/CUDA sigue siendo del usuario.
        cuda_probable = (not self.opciones.solo_cpu
                         and self.h.nvidia_presente())
        verificar_espacio(
            d.install_home, ESPACIO_CUDA if cuda_probable else ESPACIO_CPU,
            cuda=cuda_probable)

        raiz_nueva = not d.install_home.exists()
        d.install_home.mkdir(parents=True, exist_ok=True)
        with bloqueo(d.install_home):
            estado = leer_estado(d)
            previous = estado.current or (LEGACY_VENV if estado.legacy else None)
            if estado.legacy:
                info("Instalación 1.0.x detectada; se migra tras validar "
                     f"{self.origen.version}")

            # 3. Directorio de versión nuevo; nada visible apunta a él todavía.
            versions_nuevo = not d.versions.exists()
            d.versions.mkdir(exist_ok=True)
            nombre = f"{self.origen.version}-{secrets.token_hex(4)}"
            version_dir = d.versions / nombre
            version_dir.mkdir()
            (version_dir / MARCA_INCOMPLETA).write_text("", encoding="utf-8")
            try:
                # 4-8. venv, wheels, dependencias, import y --version.
                cuda = self._preparar(version_dir)
                # 9. Assets de la versión: marca de completitud.
                (version_dir / MARCA_VERSION).write_text(
                    f"{APP} {self.origen.version}\n", encoding="utf-8")
                (version_dir / MARCA_INCOMPLETA).unlink()
            except BaseException as exc:
                aviso("La instalación nueva falló; se descarta sin tocar la "
                      "instalación vigente")
                shutil.rmtree(version_dir, ignore_errors=True)
                if versions_nuevo:
                    with contextlib.suppress(OSError):
                        d.versions.rmdir()
                if raiz_nueva:
                    with contextlib.suppress(OSError):
                        d.install_home.rmdir()
                if es_sin_espacio(exc):
                    detalle = getattr(exc, "detalle", None) or str(exc)
                    raise ErrorSinEspacio(mensaje_sin_espacio(
                        previous is not None, detalle)) from exc
                raise

            relativo = f"versions/{nombre}"
            # 10. Swap atómico: desde acá, la versión nueva es la vigente.
            symlink_atomico(d.current, relativo)
            # 11-12. Enlaces, launcher, servicio y desinstalador.
            self._publicar_enlaces()
            recargar = self._publicar_escritorio(servicio_existente)
            self._instalar_uninstall()
            self._escribir_metadata(relativo, previous)
            # 13. Conserva current y previous: una instancia abierta sigue
            # ejecutando desde su versión hasta que se reinicie.
            self._podar({relativo} | ({previous} if previous else set()))
        self._systemd(recargar)
        resultado = Resultado(relativo, previous, cuda)
        resultado.avisos = self._avisos_sistema(
            d.ejecutable_estable("python"))
        return resultado

    def precargar_modelo(self) -> None:
        python = self.destino.ejecutable_estable("python")
        codigo = (
            "from faster_whisper import WhisperModel\n"
            "WhisperModel('small', device='cpu', compute_type='int8')\n"
        )
        if subprocess.run([str(python), "-I", "-c", codigo], check=False,
                          cwd="/").returncode:
            raise ErrorInstalacion("no se pudo precargar el modelo small")
        info("Modelo small disponible en caché")


@contextlib.contextmanager
def bloqueo(ruta: Path):
    """flock sobre el directorio raíz: no deja archivos de lock."""
    descriptor = os.open(ruta, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ErrorInstalacion(
                "hay otra instalación de ParlAR en curso") from None
        yield
    finally:
        os.close(descriptor)


def versiones_python(origen: Origen) -> list[str]:
    archivo = origen.share / "python-versions.txt"
    return [linea.strip() for linea in archivo.read_text(
        encoding="utf-8").splitlines() if linea.strip()]


def mensaje_python_no_soportado(versiones: list[str]) -> str:
    return (f"Esta versión de ParlAR soporta Python {versiones[0]}–"
            f"{versiones[-1]}.\nInstalá una versión compatible o descargá "
            "una release más nueva.")


def verificar_python(origen: Origen) -> None:
    actual = "%d.%d" % sys.version_info[:2]
    if origen.modo == "release":
        versiones = versiones_python(origen)
        if actual not in versiones:
            raise ErrorInstalacion(mensaje_python_no_soportado(versiones))
    elif sys.version_info < (3, 12):
        raise ErrorInstalacion(
            f"ParlAR requiere Python 3.12 o posterior; encontrado {actual}")
    try:
        import tkinter  # noqa: F401  (la Configuración usa Tk)
    except ImportError:
        raise ErrorInstalacion(
            f"Python {actual} no tiene Tk (tkinter), que usa la ventana de "
            "Configuración. Debian/Ubuntu: sudo apt install python3-tk; "
            "Fedora: sudo dnf install python3-tkinter.") from None


def cargar_renderers(share: Path):
    sys.path.insert(0, str(share))
    try:
        import render_desktop
        import render_service
    finally:
        sys.path.remove(str(share))
    return render_desktop, render_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install.sh")
    parser.add_argument("--origen", type=Path, required=True)
    parser.add_argument("--install-service", action="store_true")
    parser.add_argument("--preload-model", action="store_true")
    parser.add_argument("--cpu-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        origen = detectar_origen(args.origen)
        verificar_python(origen)
        destino = Destino.desde_entorno()
        opciones = Opciones(
            servicio=args.install_service,
            precargar_modelo=args.preload_model,
            solo_cpu=args.cpu_only,
        )
        if origen.modo == "checkout":
            info("Instalando desde un checkout de mantenimiento")
        instalador = Instalador(origen, destino, opciones)
        resultado = instalador.instalar()
    except ErrorInstalacion as exc:
        aviso(str(exc))
        return 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        aviso(f"la instalación falló: {exc}")
        return 1
    except KeyboardInterrupt:
        aviso("instalación interrumpida; la instalación vigente no cambió")
        return 130

    bin_dir = destino.bin_dir
    info(f"ParlAR {origen.version} instalado. Comandos: {bin_dir}/parlar, "
         f"{bin_dir}/parlarctl y {bin_dir}/{COMANDO_UNINSTALL}")
    for texto in resultado.avisos:
        aviso(texto)
    if resultado.previous:
        print("    Si ParlAR está abierto, cerralo y volvé a abrirlo desde el "
              f"menú para usar {origen.version}.")
        if opciones.servicio or destino.unit.exists():
            print("    Con el servicio: systemctl --user restart parlar.service")
    if f":{bin_dir}:" not in f":{os.environ.get('PATH', '')}:":
        aviso(f"{bin_dir} no está en PATH; agregalo para invocar los "
              "comandos por nombre")
    if opciones.servicio:
        print(f"    Autostart gráfico instalado: {destino.autostart}")
        print("    Inicio opcional ahora: systemctl --user start parlar.service")
    if opciones.precargar_modelo:
        try:
            instalador.precargar_modelo()
        except ErrorInstalacion as exc:
            aviso(str(exc))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
