"""Contrato de release: versión única, artefacto, instalador y desinstalador.

El instalador corre en proceso con herramientas falsas (sin pip ni red): se
prueban staging, swap atómico, rollback ante fallos, migración legacy y
ownership. ``scripts/test_release_install.sh`` cubre la instalación real.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import unittest.mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_release  # noqa: E402
import parlar_installer as pi  # noqa: E402

VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
RENDERERS = pi.cargar_renderers(ROOT / "scripts")
PYTHONS = ("3.12", "3.13", "3.14")


def ejecutar(*args, cwd=ROOT, env=None, timeout=60):
    return subprocess.run(args, cwd=cwd, env=env, text=True,
                          capture_output=True, check=False, timeout=timeout)


def digest(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def arbol(raiz: Path) -> dict[str, str]:
    """Snapshot de nombres, targets de symlinks y contenidos."""
    resultado = {}
    for ruta in sorted(raiz.rglob("*")):
        clave = str(ruta.relative_to(raiz))
        if ruta.is_symlink():
            resultado[clave] = "->" + os.readlink(ruta)
        elif ruta.is_file():
            resultado[clave] = digest(ruta)
        else:
            resultado[clave] = "dir"
    return resultado


class VersionUnica(unittest.TestCase):
    def test_pyproject_cli_build_y_changelog_coinciden(self):
        self.assertEqual(build_release.leer_version(), VERSION)
        resultado = ejecutar(sys.executable, "-B", "-m", "parlar", "--version")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual(resultado.stdout, f"ParlAR {VERSION}\n")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"\n## [{VERSION}] - ", changelog)
        self.assertLess(changelog.index("## [Unreleased]"),
                        changelog.index(f"## [{VERSION}]"))

    def test_version_no_esta_duplicada_en_codigo(self):
        for ruta in [*ROOT.glob("parlar/*.py"), *ROOT.glob("scripts/*.py"),
                     ROOT / "install.sh", ROOT / "uninstall.sh"]:
            with self.subTest(ruta=ruta.name):
                self.assertNotIn(f'"{VERSION}"', ruta.read_text(encoding="utf-8"))

    def test_version_es_instantanea_sin_runtime_ni_config(self):
        codigo = r'''
import sys
import parlar.__main__ as entrada
sys.argv = ["parlar", "--version"]
entrada.main()
pesados = {"faster_whisper", "ctranslate2", "numpy", "sounddevice",
           "pynput", "evdev", "gi", "tkinter", "parlar.app",
           "parlar.motor_transcripcion", "parlar.capturador_audio",
           "parlar.tray", "parlar.settings_window", "parlar.indicador"}
cargados = sorted(pesados & set(sys.modules))
assert not cargados, cargados
'''
        with tempfile.TemporaryDirectory() as tmp:
            entorno = os.environ.copy()
            entorno["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            entorno["XDG_DATA_HOME"] = str(Path(tmp) / "data")
            resultado = ejecutar(sys.executable, "-B", "-c", codigo, env=entorno)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertEqual(resultado.stdout, f"ParlAR {VERSION}\n")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_version_aparece_en_help(self):
        resultado = ejecutar(sys.executable, "-B", "-m", "parlar", "--help")
        self.assertIn("--version", resultado.stdout)


def crear_wheelhouse(destino: Path) -> Path:
    destino.mkdir(parents=True)
    manifiesto = {}
    for version in PYTHONS:
        tag = "cp" + version.replace(".", "")
        nombre = f"evdev-1.9.3-{tag}-{tag}-manylinux_2_28_x86_64.whl"
        with zipfile.ZipFile(destino / nombre, "w") as archivo:
            archivo.writestr("evdev/__init__.py", f"# {tag}\n")
        manifiesto[version] = [nombre]
    (destino / "wheelhouse.json").write_text(json.dumps(manifiesto))
    return destino


@unittest.skipUnless(importlib.util.find_spec("setuptools"),
                     "el build del wheel requiere setuptools")
class ArtefactoRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.wheelhouse = crear_wheelhouse(base / "wheelhouse")
        cls._epoch = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "1790000000"
        cls.antes = cls._estado_git()
        cls.salida = base / "out1"
        cls.artefactos = build_release.construir(cls.wheelhouse, cls.salida)
        cls.tarball = cls.artefactos["tarball"]

    @classmethod
    def tearDownClass(cls):
        if cls._epoch is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = cls._epoch
        cls._tmp.cleanup()

    @staticmethod
    def _estado_git():
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            return None
        return subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain=v1",
             "--untracked-files=all", "--ignored=no"],
            text=True, capture_output=True, check=False).stdout

    def miembros(self):
        with tarfile.open(self.tarball, "r:gz") as tar:
            return {m.name: m for m in tar.getmembers()}

    def test_nombres_de_artefactos(self):
        self.assertEqual(
            sorted(p.name for p in self.salida.iterdir()),
            sorted([f"parlar-{VERSION}-linux-x86_64.tar.gz",
                    f"parlar-{VERSION}-py3-none-any.whl", "SHA256SUMS"]))

    def test_allowlist_exacta(self):
        prefijo = f"parlar-{VERSION}"
        esperados = {
            prefijo, f"{prefijo}/share", f"{prefijo}/wheels",
            *(f"{prefijo}/{n}" for n in (
                "README.md", "README.en.md", "LICENSE", "SECURITY.md",
                "VERSION", "install.sh", "uninstall.sh", "constraints.txt")),
            *(f"{prefijo}/share/{n}" for n in (
                "parlar_installer.py", "render_desktop.py",
                "render_service.py", "parlar.desktop.in",
                "parlar-systemd.desktop.in", "parlar.service.in",
                "parlar.svg", "python-versions.txt")),
            f"{prefijo}/wheels/parlar-{VERSION}-py3-none-any.whl",
            *(f"{prefijo}/wheels/evdev-1.9.3-cp{v.replace('.', '')}-"
              f"cp{v.replace('.', '')}-manylinux_2_28_x86_64.whl"
              for v in PYTHONS),
        }
        self.assertEqual(set(self.miembros()), esperados)

    def test_exclusiones(self):
        for nombre in self.miembros():
            with self.subTest(nombre=nombre):
                for prohibido in (
                        ".git", ".github", "tests/", "benchmarks", "setup.sh",
                        "/parlarctl", "build/", "egg-info", ".claude",
                        ".agents", "snapshots", "__pycache__", ".wav",
                        "results"):
                    self.assertNotIn(prohibido, nombre)

    def test_version_y_metadata_normalizada(self):
        with tarfile.open(self.tarball, "r:gz") as tar:
            version = tar.extractfile(f"parlar-{VERSION}/VERSION").read()
            pythons = tar.extractfile(
                f"parlar-{VERSION}/share/python-versions.txt").read()
            for miembro in tar.getmembers():
                self.assertEqual((miembro.uid, miembro.gid), (0, 0))
                self.assertEqual((miembro.uname, miembro.gname), ("", ""))
                self.assertEqual(miembro.mtime, 1790000000)
                ejecutable = miembro.name.endswith((".sh",)) or miembro.isdir()
                self.assertEqual(miembro.mode, 0o755 if ejecutable else 0o644,
                                 miembro.name)
        self.assertEqual(version, f"{VERSION}\n".encode())
        self.assertEqual(pythons.decode().split(), list(PYTHONS))
        self.assertEqual(self.tarball.read_bytes()[4:8], b"\0\0\0\0")

    def test_sha256sums_correctos(self):
        lineas = (self.salida / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lineas), 2)
        for linea in lineas:
            suma, nombre = linea.split("  ")
            self.assertEqual(digest(self.salida / nombre), suma)
        if shutil.which("sha256sum"):
            resultado = ejecutar("sha256sum", "-c", "SHA256SUMS", cwd=self.salida)
            self.assertEqual(resultado.returncode, 0, resultado.stdout)

    def test_constraints_son_pins_exactos(self):
        with tarfile.open(self.tarball, "r:gz") as tar:
            texto = tar.extractfile(f"parlar-{VERSION}/constraints.txt").read()
        pins = [l for l in texto.decode().splitlines() if not l.startswith("#")]
        self.assertTrue(pins)
        self.assertEqual(pins, sorted(pins))
        self.assertTrue(all("==" in pin for pin in pins))
        self.assertIn("evdev==1.9.3", pins)
        self.assertIn("webrtcvad-wheels==2.0.14", pins)

    def test_sin_rutas_del_repo(self):
        with tarfile.open(self.tarball, "r:gz") as tar:
            for miembro in tar.getmembers():
                if miembro.isfile():
                    datos = tar.extractfile(miembro).read()
                    self.assertNotIn(str(ROOT).encode(), datos, miembro.name)

    def test_build_determinista(self):
        segunda = Path(self._tmp.name) / "out2"
        build_release.construir(self.wheelhouse, segunda)
        for nombre in ("SHA256SUMS", self.tarball.name):
            self.assertEqual(digest(self.salida / nombre),
                             digest(segunda / nombre), nombre)

    def test_build_no_modifica_el_worktree(self):
        if self.antes is None:
            self.skipTest("sin git")
        self.assertEqual(self._estado_git(), self.antes)

    def test_release_extraido_no_depende_de_git(self):
        destino = Path(self._tmp.name) / "extraido"
        with tarfile.open(self.tarball, "r:gz") as tar:
            tar.extractall(destino, filter="data")
        raiz = destino / f"parlar-{VERSION}"
        self.assertFalse(any(p.name == ".git" for p in raiz.rglob("*")))
        origen = pi.detectar_origen(raiz)
        self.assertEqual((origen.modo, origen.version), ("release", VERSION))
        self.assertEqual(pi.versiones_python(origen), list(PYTHONS))
        for archivo in ("install.sh", "uninstall.sh"):
            texto = (raiz / archivo).read_text(encoding="utf-8")
            self.assertNotIn("git ", texto)
            self.assertTrue(os.access(raiz / archivo, os.X_OK))

    def test_wheelhouse_incompleto_falla(self):
        roto = Path(self._tmp.name) / "roto"
        shutil.copytree(self.wheelhouse, roto)
        next(roto.glob("evdev-*cp313*.whl")).unlink()
        with self.assertRaisesRegex(build_release.ErrorBuild, "cp313"):
            build_release.construir(roto, Path(self._tmp.name) / "out-roto")


# ---------------------------------------------------------------------------
# Instalador en proceso
# ---------------------------------------------------------------------------


class HerramientasFalsas(pi.Herramientas):
    """Simula venv/pip con scripts mínimos; registra cada llamada."""

    def __init__(self, version=VERSION):
        self.version = version
        self.fallar_pip = False
        self.version_reportada = version
        self.llamadas = []

    def crear_venv(self, destino):
        self.llamadas.append(("venv", str(destino)))
        (destino / "bin").mkdir(parents=True)
        python = destino / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)

    def pip_install(self, python, argumentos):
        self.llamadas.append(("pip", argumentos))
        if self.fallar_pip:
            return False
        for comando in pi.COMANDOS:
            ruta = python.parent / comando
            ruta.write_text("#!/bin/sh\nexit 0\n")
            ruta.chmod(0o755)
        return True

    def ejecutar(self, comando):
        self.llamadas.append(("run", comando))
        salida, codigo = "", 0
        if comando[1:] == ["--version"]:
            salida = f"ParlAR {self.version_reportada}\n"
        elif "get_cuda_device_count" in comando[-1]:
            codigo = 1
        elif "importlib" in comando[-1]:
            self.assertAislado(comando)
            salida = f"{self.version}\n"
        return subprocess.CompletedProcess(comando, codigo, salida, "")

    @staticmethod
    def assertAislado(comando):
        # -I: ni el cwd ni PYTHONPATH pueden aportar otra ParlAR.
        if comando[1:3] != ["-I", "-c"]:
            raise AssertionError(f"validación sin modo aislado: {comando}")


class InstaladorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.home = self.base / "home con ñ"
        self.data = self.home / ".local" / "share"
        self.config = self.home / ".config"
        self.entorno = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
        }
        self.destino = pi.Destino.desde_entorno(self.entorno)
        self.release = self.base / "release extraído"
        (self.release / "wheels").mkdir(parents=True)
        self.origen = pi.Origen(
            modo="release", raiz=self.release, version=VERSION,
            share=ROOT / "scripts", icono=ROOT / "parlar/assets/parlar.svg",
            constraints=ROOT / "constraints.txt",
            uninstall=ROOT / "uninstall.sh", wheels=self.release / "wheels")
        self.herramientas = HerramientasFalsas()
        self.tools = self.base / "tools"
        self.tools.mkdir()
        for comando in ("systemctl", "update-desktop-database",
                        "gtk-update-icon-cache"):
            (self.tools / comando).symlink_to(shutil.which("true"))
        self._path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.tools}:/usr/bin:/bin"

    def tearDown(self):
        os.environ["PATH"] = self._path
        self._tmp.cleanup()

    def instalar(self, servicio=False, herramientas=None):
        instalador = pi.Instalador(
            self.origen, self.destino, pi.Opciones(servicio=servicio),
            herramientas or self.herramientas, renderers=RENDERERS)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return instalador.instalar()

    def uninstall(self, *args, script=None, entorno_extra=None):
        entorno = dict(os.environ)
        entorno.update(self.entorno)
        entorno.pop("PARLAR_INSTALL_HOME", None)
        entorno.update(entorno_extra or {})
        return ejecutar(str(script or self.destino.uninstall), *args,
                        cwd=self.base, env=entorno)

    def sembrar_datos(self):
        config = self.config / "parlar" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "schema_version": 1, "hotkey_toggle": "<ctrl>+<alt>+d"}) + "\n")
        sesion = self.destino.install_home / "sesiones" / "parlar-1.txt"
        sesion.parent.mkdir(parents=True)
        sesion.write_bytes("transcript ñ\x00\xff".encode("utf-8", "surrogatepass"))
        return {ruta: digest(ruta) for ruta in (config, sesion)}

    def assertPreservados(self, preservados):
        for ruta, valor in preservados.items():
            self.assertTrue(ruta.exists(), ruta)
            self.assertEqual(digest(ruta), valor, ruta)

    def versiones(self):
        return sorted(p.name for p in self.destino.versions.iterdir())


class InstalacionNueva(InstaladorBase):
    def test_layout_enlaces_y_launcher_apuntan_a_current(self):
        resultado = self.instalar()
        d = self.destino
        self.assertTrue(d.current.is_symlink())
        self.assertEqual(os.readlink(d.current), resultado.version_dir)
        self.assertRegex(resultado.version_dir,
                         rf"^versions/{VERSION}-[0-9a-f]{{8}}$")
        self.assertIsNone(resultado.previous)
        for comando in pi.COMANDOS:
            self.assertEqual(os.readlink(d.bin_dir / comando),
                             str(d.install_home / "current/venv/bin" / comando))
        self.assertEqual(os.readlink(d.bin_dir / "parlar-uninstall"),
                         str(d.uninstall))
        desktop = d.desktop.read_text(encoding="utf-8")
        self.assertIn(
            f'Exec="{d.install_home}/current/venv/bin/parlar" '
            "--abrir-configuracion", desktop)
        for texto in (desktop, *(os.readlink(d.bin_dir / c) for c in
                                 (*pi.COMANDOS, "parlar-uninstall"))):
            self.assertNotIn(str(self.release), texto)
            self.assertNotIn(str(ROOT), texto)
            self.assertNotIn("/versions/", texto)
        self.assertTrue(d.icono.read_text().startswith(RENDERERS[0].MARCA_ICONO))
        self.assertEqual(d.uninstall.read_bytes(), (ROOT / "uninstall.sh").read_bytes())
        self.assertTrue(os.access(d.uninstall, os.X_OK))
        version_dir = d.install_home / resultado.version_dir
        self.assertFalse((version_dir / pi.MARCA_INCOMPLETA).exists())
        self.assertEqual((version_dir / pi.MARCA_VERSION).read_text(),
                         f"ParlAR {VERSION}\n")
        self.assertFalse(d.unit.exists())
        self.assertFalse(d.autostart.exists())

    def test_installed_json_minimo_y_sin_datos_sensibles(self):
        resultado = self.instalar()
        datos = json.loads(self.destino.metadata.read_text())
        self.assertEqual(set(datos),
                         {"app", "version", "current", "previous", "installed_at"})
        self.assertEqual(datos["app"], "ParlAR")
        self.assertEqual(datos["version"], VERSION)
        self.assertEqual(datos["current"], resultado.version_dir)
        self.assertIsNone(datos["previous"])
        self.assertRegex(datos["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn(str(self.home), self.destino.metadata.read_text())
        self.assertEqual(list(self.destino.install_home.glob(".*.tmp-*")), [])

    def test_release_instala_solo_wheels_con_constraints(self):
        self.instalar()
        argumentos = [a for tipo, a in self.herramientas.llamadas if tipo == "pip"]
        self.assertEqual(argumentos[0], [
            "-c", str(ROOT / "constraints.txt"), "--only-binary=:all:",
            "--find-links", str(self.release / "wheels"),
            f"parlar=={VERSION}"])
        self.assertTrue(all("--only-binary=:all:" in a for a in argumentos))

    def test_servicio_opcional_apunta_a_current(self):
        self.instalar(servicio=True)
        unit = self.destino.unit.read_text()
        self.assertIn(f'ExecStart=:"{self.destino.install_home}/current/venv/bin/parlar"',
                      unit)
        self.assertNotIn("/versions/", unit)
        self.assertIn("Exec=systemctl --user start parlar.service",
                      self.destino.autostart.read_text())

    def test_install_repetido_es_idempotente_y_poda(self):
        preservados = self.sembrar_datos()
        primero = self.instalar()
        desktop = self.destino.desktop.read_bytes()
        enlaces = {c: os.readlink(self.destino.bin_dir / c)
                   for c in (*pi.COMANDOS, "parlar-uninstall")}
        segundo = self.instalar()
        tercero = self.instalar()
        self.assertEqual(segundo.previous, primero.version_dir)
        self.assertEqual(tercero.previous, segundo.version_dir)
        self.assertEqual(
            self.versiones(),
            sorted(Path(v).name for v in (segundo.version_dir, tercero.version_dir)))
        self.assertEqual(self.destino.desktop.read_bytes(), desktop)
        self.assertEqual({c: os.readlink(self.destino.bin_dir / c)
                          for c in enlaces}, enlaces)
        self.assertPreservados(preservados)

    def test_update_preserva_config_y_sesiones(self):
        self.instalar()
        preservados = self.sembrar_datos()
        self.instalar()
        self.assertPreservados(preservados)


class FalloAntesDelSwap(InstaladorBase):
    def _comprobar_fallo(self, preparar_fallo):
        self.instalar(servicio=True)
        antes = arbol(self.home)
        preparar_fallo(self.herramientas)
        with self.assertRaises(pi.ErrorInstalacion):
            self.instalar(servicio=True)
        self.assertEqual(arbol(self.home), antes)

    def test_fallo_de_pip_preserva_current(self):
        self._comprobar_fallo(lambda h: setattr(h, "fallar_pip", True))

    def test_version_inesperada_preserva_current(self):
        self._comprobar_fallo(
            lambda h: setattr(h, "version_reportada", "0.0.0"))

    def test_fallo_en_instalacion_nueva_no_deja_rastros(self):
        self.herramientas.fallar_pip = True
        with self.assertRaises(pi.ErrorInstalacion):
            self.instalar()
        self.assertFalse(os.path.lexists(self.destino.install_home))
        self.assertEqual([p for p in self.base.rglob("*")
                          if p.is_relative_to(self.home) and not p.is_dir()], [])

    def test_interrupcion_preserva_current(self):
        def interrumpir(herramientas):
            def crear_venv(_destino):
                raise KeyboardInterrupt
            herramientas.crear_venv = crear_venv
        self.instalar()
        antes = arbol(self.home)
        interrumpir(self.herramientas)
        with self.assertRaises(KeyboardInterrupt):
            self.instalar()
        self.assertEqual(arbol(self.home), antes)

    def test_launcher_ajeno_aborta_sin_efectos(self):
        self.destino.desktop.parent.mkdir(parents=True)
        self.destino.desktop.write_text("[Desktop Entry]\nName=Otro\n")
        antes = arbol(self.home)
        with self.assertRaisesRegex(pi.ErrorInstalacion, "no fue generado"):
            self.instalar()
        self.assertEqual(arbol(self.home), antes)
        self.assertEqual(self.herramientas.llamadas, [])

    def test_enlace_ajeno_aborta_sin_efectos(self):
        self.destino.bin_dir.mkdir(parents=True)
        (self.destino.bin_dir / "parlar").symlink_to("/opt/otro/bin/parlar")
        antes = arbol(self.home)
        with self.assertRaisesRegex(pi.ErrorInstalacion, "no pertenece"):
            self.instalar()
        self.assertEqual(arbol(self.home), antes)

    def test_rutas_inseguras_se_rechazan(self):
        for valor in (f"{self.data}/./parlar", f"{self.data}/x/../parlar",
                      "/parlar", "relativa/parlar", f"{self.data}/otro"):
            with self.subTest(valor=valor):
                with self.assertRaisesRegex(pi.ErrorInstalacion, "insegura"):
                    pi.Destino.desde_entorno(
                        {**self.entorno, "PARLAR_INSTALL_HOME": valor})


class MigracionLegacy(InstaladorBase):
    def crear_legacy(self, *, servicio=True, sufijo=""):
        """Reproduce el layout de install.sh 1.0.1."""
        d = self.destino
        venv_bin = d.install_home / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        for nombre in ("python", *pi.COMANDOS):
            (venv_bin / nombre).write_text("#!/bin/sh\nexit 0\n")
            (venv_bin / nombre).chmod(0o755)
        d.bin_dir.mkdir(parents=True)
        for comando in pi.COMANDOS:
            (d.bin_dir / comando).symlink_to(
                f"{d.install_home}{sufijo}/venv/bin/{comando}")
        rd, rs = RENDERERS
        rd.instalar(rd.renderizar(venv_bin / "parlar"), d.desktop)
        rd.instalar_icono(rd.cargar_icono(ROOT / "parlar/assets/parlar.svg"), d.icono)
        if servicio:
            rs.instalar(rs.renderizar_instalacion(d.install_home, venv_bin / "parlar"),
                        d.unit)
            rd.instalar(rd.renderizar_autostart(), d.autostart)
        return venv_bin

    def test_migra_tras_validar_y_preserva_datos(self):
        venv_bin = self.crear_legacy()
        preservados = self.sembrar_datos()
        preservados[venv_bin / "parlar"] = digest(venv_bin / "parlar")
        autostart = self.destino.autostart.read_bytes()

        resultado = self.instalar()

        d = self.destino
        self.assertEqual(resultado.previous, "venv")
        self.assertEqual(json.loads(d.metadata.read_text())["previous"], "venv")
        self.assertPreservados(preservados)  # config, hotkey, sesiones, venv
        for comando in pi.COMANDOS:
            self.assertEqual(os.readlink(d.bin_dir / comando),
                             str(d.install_home / "current/venv/bin" / comando))
        self.assertIn(f"{d.install_home}/current/venv/bin/parlar",
                      d.desktop.read_text())
        self.assertIn(f"{d.install_home}/current/venv/bin/parlar",
                      d.unit.read_text())
        self.assertNotIn(str(venv_bin), d.unit.read_text())
        self.assertEqual(d.autostart.read_bytes(), autostart)
        config = json.loads((self.config / "parlar/config.json").read_text())
        self.assertEqual(config["hotkey_toggle"], "<ctrl>+<alt>+d")

        # La actualización siguiente ya no necesita el venv 1.0.x.
        self.instalar()
        self.assertFalse(os.path.lexists(d.install_home / "venv"))

    def test_reconoce_enlaces_legacy_con_separadores_redundantes(self):
        self.crear_legacy(servicio=False, sufijo="//")
        self.instalar()
        self.assertEqual(os.readlink(self.destino.bin_dir / "parlar"),
                         str(self.destino.install_home / "current/venv/bin/parlar"))

    def test_fallo_no_toca_la_instalacion_legacy(self):
        self.crear_legacy()
        self.sembrar_datos()
        antes = arbol(self.home)
        self.herramientas.fallar_pip = True
        with self.assertRaises(pi.ErrorInstalacion):
            self.instalar()
        self.assertEqual(arbol(self.home), antes)

    def test_venv_symlink_no_es_legacy_ni_se_sigue(self):
        ajeno = self.base / "venv ajeno"
        (ajeno / "bin").mkdir(parents=True)
        testigo = ajeno / "bin" / "python"
        testigo.write_text("ajeno")
        self.destino.install_home.mkdir(parents=True)
        (self.destino.install_home / "venv").symlink_to(ajeno)
        resultado = self.instalar()
        self.assertIsNone(resultado.previous)
        self.assertFalse(os.path.lexists(self.destino.install_home / "venv"))
        self.assertEqual(testigo.read_text(), "ajeno")

    def test_unit_ajena_no_se_toca_sin_install_service(self):
        self.destino.unit.parent.mkdir(parents=True)
        self.destino.unit.write_text("[Service]\nExecStart=/opt/otro\n")
        self.instalar()
        self.assertEqual(self.destino.unit.read_text(),
                         "[Service]\nExecStart=/opt/otro\n")


class Desinstalacion(InstaladorBase):
    def test_default_conserva_config_y_sesiones(self):
        self.instalar(servicio=True)
        preservados = self.sembrar_datos()
        ajeno = self.destino.install_home / "archivo-no-administrado.bin"
        ajeno.write_bytes(b"ajeno\x00")
        preservados[ajeno] = digest(ajeno)

        resultado = self.uninstall()

        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        d = self.destino
        for ruta in (d.current, d.versions, d.metadata, d.uninstall,
                     d.desktop, d.icono, d.unit, d.autostart,
                     *(d.bin_dir / c for c in (*pi.COMANDOS, "parlar-uninstall"))):
            self.assertFalse(os.path.lexists(ruta), ruta)
        self.assertPreservados(preservados)
        self.assertIn("--purge-data", resultado.stdout)
        self.assertIn("~/.cache/huggingface", resultado.stdout)

    def test_parlar_uninstall_funciona_sin_release_y_con_otra_xdg(self):
        self.instalar()
        shutil.rmtree(self.release)
        resultado = self.uninstall(
            script=self.destino.bin_dir / "parlar-uninstall",
            entorno_extra={"XDG_DATA_HOME": str(self.base / "otra")})
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertFalse(os.path.lexists(self.destino.current))
        self.assertFalse(os.path.lexists(self.destino.versions))

    def test_purge_data_borra_config_y_sesiones_pero_no_huggingface(self):
        self.instalar()
        self.sembrar_datos()
        modelo = self.home / ".cache/huggingface/hub/models--Systran--x/blob"
        modelo.parent.mkdir(parents=True)
        modelo.write_bytes(b"modelo")
        otra_app = self.config / "otra-app" / "config.json"
        otra_app.parent.mkdir(parents=True)
        otra_app.write_text("{}")

        resultado = self.uninstall("--purge-data")

        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertFalse(os.path.lexists(self.config / "parlar"))
        self.assertFalse(os.path.lexists(self.destino.install_home))
        self.assertEqual(modelo.read_bytes(), b"modelo")
        self.assertEqual(otra_app.read_text(), "{}")
        self.assertIn("rm -rf ~/.cache/huggingface/hub/models--Systran--faster-whisper-*",
                      resultado.stdout)

    def test_purge_data_no_sigue_symlinks_de_datos(self):
        self.instalar()
        ajeno = self.base / "config ajena"
        ajeno.mkdir()
        (ajeno / "KEEP").write_text("ajeno")
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "parlar").symlink_to(ajeno)
        resultado = self.uninstall("--purge-data")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual((ajeno / "KEEP").read_text(), "ajeno")

    def test_uninstall_tambien_limpia_legacy(self):
        MigracionLegacy.crear_legacy(self)
        self.instalar()
        resultado = self.uninstall()
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertFalse(os.path.lexists(self.destino.install_home / "venv"))

    def test_opcion_invalida_no_borra(self):
        self.instalar()
        antes = arbol(self.home)
        resultado = self.uninstall("--purgar")
        self.assertEqual(resultado.returncode, 2)
        self.assertEqual(arbol(self.home), antes)


class WrapperInstallSh(unittest.TestCase):
    def preparar(self, base, version_python):
        release = base / "release"
        (release / "wheels").mkdir(parents=True)
        (release / "share").mkdir()
        shutil.copy2(ROOT / "install.sh", release / "install.sh")
        (release / "VERSION").write_text(f"{VERSION}\n")
        (release / "share" / "python-versions.txt").write_text(
            "\n".join(PYTHONS) + "\n")
        marca = base / "instalador-ejecutado"
        python = base / "python-falso"
        python.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then\n'
            '  case "$2" in\n'
            f'    *version_info*) echo {version_python} ;;\n'
            "  esac\n"
            "  exit 0\n"
            "fi\n"
            f'printf "%s\\n" "$@" > "{marca}"\n')
        python.chmod(0o755)
        entorno = dict(os.environ, PARLAR_BOOTSTRAP_PYTHON=str(python),
                       HOME=str(base / "home"))
        return release, marca, entorno

    def test_python_no_soportado_falla_antes_con_mensaje_claro(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, marca, entorno = self.preparar(Path(tmp), "3.11")
            resultado = ejecutar(str(release / "install.sh"), env=entorno)
            self.assertEqual(resultado.returncode, 1)
            self.assertIn("Esta versión de ParlAR soporta Python 3.12–3.14.",
                          resultado.stderr)
            self.assertIn("Instalá una versión compatible o descargá una "
                          "release más nueva.", resultado.stderr)
            self.assertFalse(marca.exists())
            self.assertFalse((Path(tmp) / "home").exists())

    def test_python_soportado_delega_con_origen_del_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, marca, entorno = self.preparar(Path(tmp), "3.13")
            resultado = ejecutar(str(release / "install.sh"), "--cpu-only",
                                 env=entorno)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertEqual(marca.read_text().splitlines(), [
                str(release.resolve() / "share/parlar_installer.py"),
                "--origen", str(release.resolve()), "--cpu-only"])

    def test_help_y_opcion_invalida_no_buscan_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, marca, entorno = self.preparar(Path(tmp), "3.13")
            ayuda = ejecutar(str(release / "install.sh"), "--help", env=entorno)
            self.assertEqual(ayuda.returncode, 0)
            self.assertIn("parlar-uninstall", ayuda.stdout)
            invalida = ejecutar(str(release / "install.sh"), "--x", env=entorno)
            self.assertEqual(invalida.returncode, 2)
            self.assertFalse(marca.exists())

    def test_directorio_desconocido_falla(self):
        with tempfile.TemporaryDirectory() as tmp:
            copia = Path(tmp) / "install.sh"
            shutil.copy2(ROOT / "install.sh", copia)
            resultado = ejecutar(str(copia), cwd=Path(tmp))
            self.assertEqual(resultado.returncode, 1)
            self.assertIn("no es un release de ParlAR", resultado.stderr)

    # -- Python con Tk ------------------------------------------------------

    def preparar_candidatos(self, base, pythons):
        """Release + PATH aislado con Pythons falsos {nombre: (versión, tk)}."""
        release, _marca, _ = self.preparar(base, "3.14")
        binarios = base / "bin"
        binarios.mkdir()
        (binarios / "dirname").symlink_to(shutil.which("dirname"))
        marca = base / "elegido"
        for nombre, (version, tk) in pythons.items():
            ruta = binarios / nombre
            ruta.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then\n'
                '  case "$2" in\n'
                f'    *%d.%d*) echo {version} ;;\n'
                f'    *tkinter*) exit {0 if tk else 1} ;;\n'
                "  esac\n"
                "  exit 0\n"
                "fi\n"
                f'echo {nombre} > "{marca}"\n')
            ruta.chmod(0o755)
        entorno = {"PATH": str(binarios), "HOME": str(base / "home"),
                   "LANG": "C.UTF-8"}
        return release, marca, entorno

    def ejecutar_install(self, release, entorno):
        return ejecutar(shutil.which("bash"), str(release / "install.sh"),
                        "--cpu-only", env=entorno)

    def test_python_sin_tkinter_se_salta_y_se_usa_el_siguiente(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, marca, entorno = self.preparar_candidatos(Path(tmp), {
                "python3": ("3.14", False),
                "python3.14": ("3.14", False),
                "python3.13": ("3.13", True),
            })
            resultado = self.ejecutar_install(release, entorno)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertEqual(marca.read_text().strip(), "python3.13")

    def test_ningun_python_con_tkinter_falla_sin_tocar_la_instalacion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            release, marca, entorno = self.preparar_candidatos(base, {
                "python3": ("3.14", False),
                "python3.13": ("3.13", False),
                "python3.12": ("3.12", False),
            })
            # Instalación vigente simulada: current, versión, enlaces y datos.
            raiz = base / "home/.local/share/parlar"
            version = raiz / "versions/1.0.9-0123abcd/venv/bin"
            version.mkdir(parents=True)
            (version / "parlar").write_text("#!/bin/sh\n")
            (raiz / "current").symlink_to("versions/1.0.9-0123abcd")
            (raiz / "sesiones").mkdir()
            (raiz / "sesiones/s.txt").write_text("dato")
            bin_dir = base / "home/.local/bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "parlar").symlink_to(raiz / "current/venv/bin/parlar")
            antes = arbol(base / "home")

            resultado = self.ejecutar_install(release, entorno)

            self.assertEqual(resultado.returncode, 1)
            self.assertIn("con Tk (tkinter)", resultado.stderr)
            self.assertIn("3.12–3.14", resultado.stderr)
            self.assertIn("python3-tk", resultado.stderr)
            self.assertFalse(marca.exists())
            self.assertEqual(arbol(base / "home"), antes)

    def test_instalador_exige_tkinter_antes_del_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            (share / "python-versions.txt").write_text(
                "%d.%d\n" % sys.version_info[:2])
            origen = pi.Origen("release", share, VERSION, share, share,
                               share, share, share)
            with unittest.mock.patch.dict(sys.modules, {"tkinter": None}):
                with self.assertRaisesRegex(pi.ErrorInstalacion, "tkinter"):
                    pi.verificar_python(origen)

    def test_instalador_rechaza_python_fuera_del_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp)
            (share / "python-versions.txt").write_text("3.10\n3.11\n")
            origen = pi.Origen("release", share, VERSION, share, share,
                               share, share, share)
            with self.assertRaisesRegex(pi.ErrorInstalacion,
                                        "soporta Python 3.10–3.11"):
                pi.verificar_python(origen)


class ReinicioTrasActualizacion(unittest.TestCase):
    """Un proceso de una versión vieja relanza la versión de current."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name) / "datos con ñ" / "parlar"
        sitio = "venv/lib/python3.14/site-packages/parlar"
        self.paquete_viejo = self.raiz / "versions/1.1.0-aaaaaaaa" / sitio
        self.paquete_viejo.mkdir(parents=True)
        nuevo = self.raiz / "versions/1.1.1-bbbbbbbb"
        (nuevo / "venv/bin").mkdir(parents=True)
        (nuevo / "venv/bin/python").write_text("#!/bin/sh\n")
        (self.raiz / "current").symlink_to("versions/1.1.1-bbbbbbbb")
        self.estable = str(self.raiz / "current/venv/bin/python")

    def tearDown(self):
        self._tmp.cleanup()

    def viejo(self):
        from parlar import instalacion
        return unittest.mock.patch.object(
            instalacion, "PAQUETE", self.paquete_viejo)

    def test_python_estable_apunta_a_current(self):
        from parlar import instalacion
        self.assertEqual(instalacion.raiz_instalacion(self.paquete_viejo),
                         self.raiz)
        self.assertEqual(instalacion.python_estable(self.paquete_viejo),
                         self.estable)

    def test_reiniciar_lanza_el_python_de_current(self):
        from parlar.restart import comando_reinicio
        with self.viejo():
            self.assertEqual(comando_reinicio(),
                             [self.estable, "-m", "parlar", "--reiniciar"])
            self.assertEqual(
                comando_reinicio(reintentar_gestor="manual")[0], self.estable)

    def test_reinicio_manual_arranca_runtime_de_current(self):
        from parlar import launcher
        popen = unittest.mock.Mock()
        with self.viejo():
            launcher.iniciar_runtime(popen=popen)
        self.assertEqual(popen.call_args.args[0],
                         [self.estable, "-m", "parlar"])

    def test_bandeja_relanza_con_el_python_de_current(self):
        from parlar.tray import TrayLinux
        with self.viejo():
            self.assertEqual(TrayLinux().python_launcher, self.estable)

    def test_checkout_y_layouts_ajenos_conservan_sys_executable(self):
        from parlar import instalacion
        self.assertEqual(instalacion.python_estable(ROOT / "parlar"),
                         sys.executable)
        (self.raiz / "current").unlink()
        self.assertEqual(instalacion.python_estable(self.paquete_viejo),
                         sys.executable)
        (self.raiz / "current").symlink_to("versions/no-existe")
        self.assertEqual(instalacion.python_estable(self.paquete_viejo),
                         sys.executable)
        legacy = self.raiz / "venv/lib/python3.14/site-packages/parlar"
        legacy.mkdir(parents=True)
        self.assertEqual(instalacion.python_estable(legacy), sys.executable)

    def test_explicito_sigue_mandando(self):
        from parlar.restart import comando_reinicio
        with self.viejo():
            self.assertEqual(comando_reinicio(executable="/opt/p")[0], "/opt/p")


if __name__ == "__main__":
    unittest.main()
