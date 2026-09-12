"""Regresiones del contrato de checkout, setup, servicio y gate central."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import scripts.render_service as render_service

ROOT = Path(__file__).resolve().parents[1]


def ejecutar(*args, cwd=ROOT, env=None):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


class ContratoSetup(unittest.TestCase):
    def preparar_setup_simulado(self, base):
        for nombre in ("setup.sh", "requirements.txt", "requirements-optional.txt"):
            shutil.copy2(ROOT / nombre, base / nombre)
        (base / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "render_service.py",
                     base / "scripts" / "render_service.py")
        shutil.copy2(ROOT / "scripts" / "parlar.service.in",
                     base / "scripts" / "parlar.service.in")
        python = base / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        return python

    def test_setup_y_runner_son_ejecutables_y_shell_valido(self):
        for ruta in (ROOT / "setup.sh", ROOT / "scripts" / "check.sh"):
            with self.subTest(ruta=ruta):
                self.assertTrue(ruta.stat().st_mode & stat.S_IXUSR)
        resultado = ejecutar(
            "bash", "-n", "setup.sh", "scripts/check.sh"
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)

    def test_setup_help_no_modifica_checkout(self):
        antes = ejecutar(
            "git", "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout
        resultado = ejecutar("./setup.sh", "--help")
        despues = ejecutar(
            "git", "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("--install-service", resultado.stdout)
        self.assertIn("--preload-model", resultado.stdout)
        self.assertEqual(antes, despues)

    def test_webrtcvad_es_opcional_y_fallback_real(self):
        requeridas = (ROOT / "requirements.txt").read_text()
        opcionales = (ROOT / "requirements-optional.txt").read_text()
        self.assertNotIn("webrtcvad", requeridas.lower())
        self.assertIn("webrtcvad", opcionales.lower())

        codigo = r'''
import builtins
real_import = builtins.__import__
def sin_webrtcvad(name, *args, **kwargs):
    if name == "webrtcvad":
        raise ImportError("ausente a propósito")
    return real_import(name, *args, **kwargs)
builtins.__import__ = sin_webrtcvad
from parlar import capturador_audio
vad = capturador_audio.crear_vad(2, 16000)
assert type(vad).__name__ == "_VADEnergia"
'''
        resultado = ejecutar(sys.executable, "-B", "-c", codigo)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("VAD de energía", resultado.stderr)

    def test_modelo_no_se_precarga_sin_flag(self):
        setup = (ROOT / "setup.sh").read_text()
        self.assertIn("if ((PRELOAD_MODEL))", setup)
        self.assertIn("--preload-model", setup)

    def test_setup_repetido_reutiliza_venv_sin_borrar_datos(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.preparar_setup_simulado(base)
            testigo = base / ".venv" / "dato-existente"
            testigo.write_text("conservar")
            primera = ejecutar(
                str(base / "setup.sh"), "--skip-system-packages", cwd=base
            )
            segunda = ejecutar(
                str(base / "setup.sh"), "--skip-system-packages", cwd=base
            )
            self.assertEqual(primera.returncode, 0, primera.stderr)
            self.assertEqual(segunda.returncode, 0, segunda.stderr)
            self.assertIn("Reutilizando entorno virtual", segunda.stdout)
            self.assertEqual(testigo.read_text(), "conservar")

    def test_fallo_de_dependencia_requerida_es_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.preparar_setup_simulado(base)
            binario = base / "bin"
            binario.mkdir()
            sudo = binario / "sudo"
            sudo.write_text("#!/bin/sh\nexit 7\n")
            sudo.chmod(0o755)
            entorno = os.environ.copy()
            entorno["PATH"] = f"{binario}:/usr/bin:/bin"
            resultado = ejecutar(str(base / "setup.sh"), cwd=base, env=entorno)
            self.assertEqual(resultado.returncode, 7)
            self.assertNotIn("Instalación estructural completa", resultado.stdout)

    def test_fallo_de_paquete_opcional_advierte_y_continua(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.preparar_setup_simulado(base)
            binario = base / "bin"
            binario.mkdir()
            sudo = binario / "sudo"
            sudo.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in *wtype) exit 9 ;; *) exit 0 ;; esac\n"
            )
            sudo.chmod(0o755)
            entorno = os.environ.copy()
            entorno["PATH"] = f"{binario}:/usr/bin:/bin"
            resultado = ejecutar(str(base / "setup.sh"), cwd=base, env=entorno)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertIn("opcional no instalado: wtype", resultado.stderr)

    def test_fallo_de_webrtcvad_opcional_advierte_y_continua(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            python = self.preparar_setup_simulado(base)
            python.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in *requirements-optional.txt*) exit 9 ;; "
                "*) exit 0 ;; esac\n"
            )
            python.chmod(0o755)
            resultado = ejecutar(
                str(base / "setup.sh"), "--skip-system-packages", cwd=base
            )
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertIn("se usará el VAD de energía probado", resultado.stderr)


class ContratoServicio(unittest.TestCase):
    def test_template_sin_checkout_fijo_y_con_umask(self):
        plantilla = (ROOT / "scripts" / "parlar.service.in").read_text()
        self.assertNotIn("%h/parlar", plantilla)
        self.assertIn("@PARLAR_WORKDIR@", plantilla)
        self.assertIn("@PARLAR_PYTHON@", plantilla)
        self.assertIn("UMask=0077", plantilla)

    def test_render_usa_path_real_con_espacios_y_es_idempotente(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "checkout con espacios"
            repo.mkdir()
            salida = base / "config" / "parlar.service"
            comando = (
                sys.executable,
                str(ROOT / "scripts" / "render_service.py"),
                "--repo", str(repo),
                "--output", str(salida),
            )
            primera = ejecutar(*comando)
            segunda = ejecutar(*comando)
            self.assertEqual(primera.returncode, 0, primera.stderr)
            self.assertEqual(segunda.returncode, 0, segunda.stderr)
            unit = salida.read_text()
            self.assertIn(str(repo.resolve()), unit)
            self.assertIn(str(repo.resolve() / ".venv/bin/python"), unit)
            self.assertNotIn("@PARLAR_", unit)
            self.assertIn("sin cambios", segunda.stdout)
            self.assertEqual(stat.S_IMODE(salida.stat().st_mode), 0o600)
            self.assertEqual(list(salida.parent.glob(".*.tmp-*")), [])

    @unittest.skipUnless(shutil.which("systemd-analyze"),
                         "systemd-analyze no está disponible")
    def test_systemd_analyze_acepta_matriz_de_paths(self):
        variantes = (
            ("simple", Path("simple/path")),
            ("spaces", Path("path with spaces")),
            ("unicode", Path("path-con-unicode-ñ")),
            ("long", Path("path-" + "x" * 180)),
            ("dollar", Path("path$with$dollar")),
            ("percent", Path("path%with%percent")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for indice, (nombre, relativa) in enumerate(variantes):
                with self.subTest(nombre=nombre):
                    repo = base / relativa
                    python = repo / ".venv" / "bin" / "python"
                    python.parent.mkdir(parents=True)
                    python.symlink_to(sys.executable)
                    salida = base / f"parlar-probe-{indice}.service"
                    resultado = ejecutar(
                        sys.executable,
                        str(ROOT / "scripts" / "render_service.py"),
                        "--repo", str(repo), "--output", str(salida),
                    )
                    self.assertEqual(resultado.returncode, 0, resultado.stderr)
                    unit = salida.read_text(encoding="utf-8")
                    working = next(linea for linea in unit.splitlines()
                                   if linea.startswith("WorkingDirectory="))
                    exec_start = next(linea for linea in unit.splitlines()
                                      if linea.startswith("ExecStart="))
                    self.assertFalse(
                        working.removeprefix("WorkingDirectory=").startswith('"'))
                    self.assertTrue(
                        exec_start.removeprefix("ExecStart=").startswith(':"'))
                    verificacion = ejecutar(
                        "systemd-analyze", "verify", str(salida))
                    self.assertEqual(
                        verificacion.returncode, 0, verificacion.stderr)

    def test_temporal_eexist_no_se_borra_y_se_elige_otro(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            salida = base / "parlar.service"
            testigo = base / ".parlar.service.tmp-424242-0"
            testigo.write_text("ajeno", encoding="utf-8")
            with mock.patch.object(
                    render_service.os, "getpid", return_value=424242):
                estado = render_service.instalar("unit válida\n", salida)
            self.assertEqual(estado, "instalada")
            self.assertEqual(testigo.read_text(encoding="utf-8"), "ajeno")
            self.assertEqual(salida.read_text(encoding="utf-8"), "unit válida\n")
            self.assertEqual(
                list(base.glob(".parlar.service.tmp-424242-*")), [testigo])

    def test_dos_renders_concurrentes_no_comparten_temporal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            salida = base / "parlar.service"
            barrera = threading.Barrier(2)
            replace_real = os.replace
            resultados = []
            errores = []

            def replace_sincronizado(origen, destino):
                barrera.wait(timeout=2)
                replace_real(origen, destino)

            def render():
                try:
                    resultados.append(render_service.instalar(
                        "unit concurrente\n", salida))
                except BaseException as exc:
                    errores.append(exc)

            with mock.patch.object(
                    render_service.os, "replace",
                    side_effect=replace_sincronizado):
                hilos = [threading.Thread(target=render) for _ in range(2)]
                for hilo in hilos:
                    hilo.start()
                for hilo in hilos:
                    hilo.join(3)
            self.assertFalse(any(hilo.is_alive() for hilo in hilos))
            self.assertEqual(errores, [])
            self.assertEqual(resultados, ["instalada", "instalada"])
            self.assertEqual(
                salida.read_text(encoding="utf-8"), "unit concurrente\n")
            self.assertEqual(list(base.glob(".*.tmp-*")), [])

    def test_render_no_reemplaza_unit_ajena(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            salida = base / "parlar.service"
            salida.write_text("[Unit]\nDescription=personal\n")
            resultado = ejecutar(
                sys.executable,
                str(ROOT / "scripts" / "render_service.py"),
                "--repo", str(base / "repo"),
                "--output", str(salida),
            )
            self.assertNotEqual(resultado.returncode, 0)
            self.assertEqual(salida.read_text(), "[Unit]\nDescription=personal\n")


class SmokesCheckout(unittest.TestCase):
    @staticmethod
    def _python_controlado(base):
        python = base / "python"
        python.write_text(
            "#!/bin/sh\n"
            "echo RESOLVED_PYTHON=$0 >&2\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
        return python

    def test_gate_resuelve_override_path_y_nombre_sin_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            python = self._python_controlado(base)
            entorno_base = os.environ.copy()
            entorno_base["PATH"] = f"{base}:/usr/bin:/bin"
            casos = (
                ("nombre", "python", str(python)),
                ("absoluta", str(python), str(python)),
                ("relativa", os.path.relpath(python, ROOT),
                 os.path.relpath(python, ROOT)),
            )
            for nombre, override, esperado in casos:
                with self.subTest(nombre=nombre):
                    entorno = entorno_base.copy()
                    entorno["PARLAR_PYTHON"] = override
                    resultado = ejecutar(
                        str(ROOT / "scripts" / "check.sh"), env=entorno)
                    self.assertEqual(resultado.returncode, 0, resultado.stderr)
                    self.assertIn(f"==> Python: {esperado}", resultado.stdout)
                    self.assertIn("RESOLVED_PYTHON=", resultado.stderr)

    def test_gate_rechaza_nombre_python_inexistente_antes_del_gate(self):
        entorno = os.environ.copy()
        entorno["PATH"] = "/usr/bin:/bin"
        entorno["PARLAR_PYTHON"] = "python-que-no-existe-parlar"
        resultado = ejecutar(
            str(ROOT / "scripts" / "check.sh"), env=entorno)
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("PARLAR_PYTHON no se pudo resolver", resultado.stderr)
        self.assertNotIn("Shell y bytecode", resultado.stdout)

    def test_help_no_importa_app_ni_crea_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            codigo = r'''
import builtins
import sys
import parlar.__main__ as entrada
real_import = builtins.__import__
def sin_app(name, *args, **kwargs):
    if name in {"app", "parlar.app"}:
        raise AssertionError("--help intentó importar App")
    return real_import(name, *args, **kwargs)
builtins.__import__ = sin_app
sys.argv = ["parlar", "--help"]
try:
    entrada.main()
except SystemExit as exc:
    assert exc.code == 0
'''
            entorno = os.environ.copy()
            entorno["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            resultado = ejecutar(sys.executable, "-B", "-c", codigo, env=entorno)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertIn("usage: parlar", resultado.stdout)
            self.assertFalse((Path(tmp) / "config").exists())

    def test_parlarctl_sin_argumentos_muestra_uso_y_sale_2(self):
        resultado = ejecutar("./parlarctl")
        self.assertEqual(resultado.returncode, 2)
        self.assertIn("uso: parlarctl", resultado.stdout)

    def test_ci_invoca_el_gate_central_completo(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text()
        runner = (ROOT / "scripts/check.sh").read_text()
        self.assertIn("./scripts/check.sh", workflow)
        for garantia in (
            "tests/run_tests.py",
            "unittest discover",
            "py_compile",
            "git diff --check",
        ):
            with self.subTest(garantia=garantia):
                self.assertIn(garantia, runner)


if __name__ == "__main__":
    unittest.main()
