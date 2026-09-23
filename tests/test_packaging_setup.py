"""Regresiones del contrato de checkout, setup, servicio y gate central."""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import scripts.render_service as render_service
import scripts.render_desktop as render_desktop

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
        for nombre in (
            "setup.sh", "requirements.txt", "requirements-optional.txt",
            "pyproject.toml",
        ):
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
        for ruta in (
            ROOT / "setup.sh", ROOT / "install.sh", ROOT / "uninstall.sh",
            ROOT / "scripts" / "check.sh",
        ):
            with self.subTest(ruta=ruta):
                self.assertTrue(ruta.stat().st_mode & stat.S_IXUSR)
        resultado = ejecutar(
            "bash", "-n", "setup.sh", "install.sh", "uninstall.sh",
            "scripts/check.sh",
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
        self.assertIn("webrtcvad-wheels==2.0.14", opcionales.lower())
        self.assertNotIn("setuptools", opcionales.lower())

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
    @staticmethod
    def directivas(contenido):
        return {
            linea.split("=", 1)[0]: linea.split("=", 1)[1]
            for linea in contenido.splitlines()
            if "=" in linea
        }

    def test_template_sin_checkout_fijo_y_con_umask(self):
        plantilla = (ROOT / "scripts" / "parlar.service.in").read_text()
        self.assertNotIn("%h/parlar", plantilla)
        self.assertIn("@PARLAR_WORKDIR@", plantilla)
        self.assertIn("@PARLAR_EXECUTABLE@", plantilla)
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
            self.assertIn(str(repo.resolve() / ".venv/bin/parlar"), unit)
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
                    executable = repo / ".venv" / "bin" / "parlar"
                    executable.parent.mkdir(parents=True)
                    executable.symlink_to(sys.executable)
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
                    self.assertEqual(
                        working,
                        "WorkingDirectory="
                        + render_service._ruta_working_directory(repo.resolve()),
                    )
                    self.assertEqual(
                        exec_start,
                        "ExecStart="
                        + render_service._argumento_execstart(executable),
                    )
                    self.assertFalse(
                        working.removeprefix("WorkingDirectory=").startswith('"'))
                    self.assertTrue(
                        exec_start.removeprefix("ExecStart=").startswith(':"'))
                    verificacion = ejecutar(
                        "systemd-analyze", "verify", str(salida))
                    self.assertEqual(
                        verificacion.returncode, 0, verificacion.stderr)

    @unittest.skipUnless(shutil.which("systemd-analyze"),
                         "systemd-analyze no está disponible")
    def test_placeholders_en_paths_se_insertan_una_sola_vez(self):
        variantes = (
            ("executable", Path("@PARLAR_EXECUTABLE@")),
            ("workdir", Path("@PARLAR_WORKDIR@")),
            ("root-literal", Path("@PARLAR_ROOT@")),
            ("juntos", Path("@PARLAR_EXECUTABLE@@PARLAR_WORKDIR@")),
            ("embebido", Path("foo@PARLAR_EXECUTABLE@bar")),
            ("intermedio", Path("nivel") / "@PARLAR_EXECUTABLE@" / "repo"),
            ("escaping", Path("proyecto % $ @PARLAR_EXECUTABLE@ ñ")),
        )
        plantilla = (ROOT / "scripts" / "parlar.service.in").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for indice, (nombre, relativa) in enumerate(variantes):
                with self.subTest(nombre=nombre):
                    repo = base / relativa
                    executable = repo / ".venv" / "bin" / "parlar"
                    executable.parent.mkdir(parents=True)
                    executable.symlink_to(sys.executable)
                    unit = render_service.renderizar(repo)
                    directivas = self.directivas(unit)
                    self.assertEqual(
                        directivas["WorkingDirectory"],
                        render_service._ruta_working_directory(repo.resolve()),
                    )
                    self.assertEqual(
                        directivas["ExecStart"],
                        render_service._argumento_execstart(executable),
                    )
                    salida = base / f"parlar-placeholder-{indice}.service"
                    salida.write_text(unit, encoding="utf-8")
                    verificacion = ejecutar(
                        "systemd-analyze", "verify", str(salida))
                    self.assertEqual(
                        verificacion.returncode, 0, verificacion.stderr)

            repo = base / "checkout"
            repo.mkdir()
            executable = (
                base / "bin-@PARLAR_ROOT@@PARLAR_WORKDIR@" / "parlar")
            executable.parent.mkdir()
            executable.symlink_to(sys.executable)
            unit = render_service._renderizar_plantilla(
                plantilla, repo.resolve(), executable)
            directivas = self.directivas(unit)
            self.assertEqual(
                directivas["WorkingDirectory"],
                render_service._ruta_working_directory(repo.resolve()),
            )
            self.assertEqual(
                directivas["ExecStart"],
                render_service._argumento_execstart(executable),
            )
            salida = base / "parlar-python-placeholder.service"
            salida.write_text(unit, encoding="utf-8")
            verificacion = ejecutar(
                "systemd-analyze", "verify", str(salida))
            self.assertEqual(verificacion.returncode, 0, verificacion.stderr)

    def test_template_faltante_o_desconocido_falla_claro(self):
        plantilla = (ROOT / "scripts" / "parlar.service.in").read_text(
            encoding="utf-8"
        )
        repo = Path("/tmp/parlar-repo")
        executable = repo / ".venv" / "bin" / "parlar"
        for faltante in ("@PARLAR_WORKDIR@", "@PARLAR_EXECUTABLE@"):
            with self.subTest(faltante=faltante):
                with self.assertRaisesRegex(
                        ValueError, "falta placeholder requerido"):
                    render_service._renderizar_plantilla(
                        plantilla.replace(faltante, ""), repo, executable)

        with self.assertRaisesRegex(ValueError, "placeholder desconocido"):
            render_service._renderizar_plantilla(
                plantilla + "\nEnvironment=@PARLAR_DESCONOCIDO@\n",
                repo, executable,
            )

    def test_render_rechaza_paths_fuera_del_dominio_sin_publicar(self):
        variantes = (
            ("single-quote", Path("path'with'quote")),
            ("double-quote", Path('path"with"quote')),
            ("backslash", Path(r"path\with\backslash")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for nombre, relativa in variantes:
                with self.subTest(nombre=nombre):
                    repo = base / relativa
                    repo.mkdir()
                    salida = base / f"parlar-rejected-{nombre}.service"
                    comando = (
                        sys.executable,
                        str(ROOT / "scripts" / "render_service.py"),
                        "--repo", str(repo), "--output", str(salida),
                    )

                    sin_unit = ejecutar(*comando)
                    self.assertNotEqual(sin_unit.returncode, 0)
                    self.assertIn("carácter no soportado", sin_unit.stderr)
                    self.assertFalse(salida.exists())
                    self.assertEqual(list(base.glob(".*.tmp-*")), [])

                    testigo = "# Generated by ParlAR setup.sh.\nunit anterior\n"
                    salida.write_text(testigo, encoding="utf-8")
                    existente = ejecutar(*comando)
                    self.assertNotEqual(existente.returncode, 0)
                    self.assertIn("carácter no soportado", existente.stderr)
                    self.assertEqual(salida.read_text(encoding="utf-8"), testigo)
                    self.assertEqual(list(base.glob(".*.tmp-*")), [])

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


class ContratoCudaPackaging(unittest.TestCase):
    def test_pyproject_declara_runtime_cuda_reproducible(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn(
            'nvidia-cublas-cu12==12.9.2.10',
            pyproject,
        )
        self.assertIn(
            'nvidia-cuda-nvrtc-cu12==12.9.86',
            pyproject,
        )
        self.assertIn(
            'nvidia-cudnn-cu12==9.24.0.43',
            pyproject,
        )

    def test_instalador_ofrece_cpu_only_y_extra_cuda(self):
        instalador = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn("--cpu-only", instalador)
        self.assertIn('"$REPO_DIR[cuda]"', instalador)
        self.assertIn("ctranslate2.get_cuda_device_count()", instalador)
        self.assertNotIn("nvidia-smi", instalador)


class ContratoInstalacionUsuario(unittest.TestCase):
    def test_launcher_es_valido_idempotente_y_no_reemplaza_ajeno(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ejecutable = base / "ruta con $ y %" / "parlar"
            ejecutable.parent.mkdir()
            ejecutable.write_text("#!/bin/sh\n", encoding="utf-8")
            salida = base / "applications" / "parlar.desktop"
            contenido = render_desktop.renderizar(ejecutable)
            self.assertEqual(
                render_desktop.instalar(contenido, salida), "instalado")
            self.assertEqual(
                render_desktop.instalar(contenido, salida), "sin cambios")
            self.assertNotIn("@PARLAR_", salida.read_text(encoding="utf-8"))
            if shutil.which("desktop-file-validate"):
                validacion = ejecutar("desktop-file-validate", str(salida))
                self.assertEqual(
                    validacion.returncode, 0, validacion.stderr)

            salida.write_text("[Desktop Entry]\nName=Personal\n")
            with self.assertRaisesRegex(RuntimeError, "no fue generado"):
                render_desktop.instalar(contenido, salida)
            self.assertIn("Name=Personal", salida.read_text())

    def test_uninstall_help_y_opcion_invalida_no_borran_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            data = home / "share"
            config = home / "config"
            install_home = data / "parlar"
            bin_dir = home / "bin"

            install_home.mkdir(parents=True)
            bin_dir.mkdir(parents=True)

            testigo = install_home / "NO_BORRAR"
            testigo.write_text("presente\n", encoding="utf-8")

            entorno = os.environ.copy()
            entorno.update({
                "HOME": str(home),
                "XDG_DATA_HOME": str(data),
                "XDG_CONFIG_HOME": str(config),
                "PARLAR_INSTALL_HOME": str(install_home),
                "PARLAR_BIN_DIR": str(bin_dir),
            })

            ayuda = ejecutar(
                str(ROOT / "uninstall.sh"),
                "--help",
                cwd=ROOT,
                env=entorno,
            )

            self.assertEqual(
                ayuda.returncode,
                0,
                ayuda.stderr,
            )
            self.assertIn(
                "Uso: ./uninstall.sh",
                ayuda.stdout,
            )
            self.assertTrue(
                testigo.exists()
            )

            invalida = ejecutar(
                str(ROOT / "uninstall.sh"),
                "--opcion-inexistente",
                cwd=ROOT,
                env=entorno,
            )

            self.assertEqual(
                invalida.returncode,
                2,
            )
            self.assertIn(
                "opción desconocida",
                invalida.stderr,
            )
            self.assertTrue(
                testigo.exists()
            )

    def test_instalacion_y_desinstalacion_xdg_simuladas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            for nombre in ("install.sh", "uninstall.sh"):
                shutil.copy2(ROOT / nombre, repo / nombre)
            scripts = repo / "scripts"
            scripts.mkdir()
            for nombre in (
                "render_service.py", "render_desktop.py",
                "parlar.service.in", "parlar.desktop.in",
            ):
                shutil.copy2(ROOT / "scripts" / nombre, scripts / nombre)

            home = base / "home"
            data = home / "share"
            config = home / "config"
            install_home = data / "parlar"
            bin_dir = home / "bin"
            venv_bin = install_home / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            python = venv_bin / "python"
            python.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *render_service.py|*render_desktop.py) "
                f"exec {shlex.quote(sys.executable)} \"$@\" ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            for comando in ("parlar", "parlarctl"):
                ruta = venv_bin / comando
                ruta.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                ruta.chmod(0o755)

            tools = base / "tools"
            tools.mkdir()
            systemctl = tools / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            systemctl.chmod(0o755)

            testigo_config = config / "parlar" / "config.json"
            testigo_config.parent.mkdir(parents=True)
            testigo_config.write_text("{}\n", encoding="utf-8")
            entorno = os.environ.copy()
            entorno.update({
                "HOME": str(home),
                "XDG_DATA_HOME": str(data),
                "XDG_CONFIG_HOME": str(config),
                "PARLAR_INSTALL_HOME": str(install_home),
                "PARLAR_BIN_DIR": str(bin_dir),
                "PARLAR_BOOTSTRAP_PYTHON": str(python),
                "PATH": f"{tools}:/usr/bin:/bin",
            })

            instalado = ejecutar(
                str(repo / "install.sh"), "--install-service",
                cwd=repo, env=entorno)
            self.assertEqual(instalado.returncode, 0, instalado.stderr)
            for comando in ("parlar", "parlarctl"):
                enlace = bin_dir / comando
                self.assertTrue(enlace.is_symlink())
                self.assertEqual(
                    enlace.readlink(), venv_bin / comando)
            unit = config / "systemd" / "user" / "parlar.service"
            desktop = data / "applications" / "parlar.desktop"
            self.assertIn(str(venv_bin / "parlar"), unit.read_text())
            self.assertNotIn(str(repo), unit.read_text())
            self.assertIn(str(venv_bin / "parlar"), desktop.read_text())

            desinstalado = ejecutar(
                str(repo / "uninstall.sh"), cwd=repo, env=entorno)
            self.assertEqual(
                desinstalado.returncode, 0, desinstalado.stderr)
            self.assertFalse(install_home.exists())
            self.assertFalse(unit.exists())
            self.assertFalse(desktop.exists())
            self.assertFalse((bin_dir / "parlar").exists())
            self.assertFalse((bin_dir / "parlarctl").exists())
            self.assertTrue(testigo_config.exists())


class SmokesCheckout(unittest.TestCase):
    def test_pyproject_instala_ambos_entrypoints(self):
        proyecto = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(proyecto["project"]["requires-python"], ">=3.12")
        self.assertEqual(proyecto["project"]["scripts"], {
            "parlar": "parlar.__main__:main",
            "parlarctl": "parlar.control:parlarctl_main",
        })

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

    def test_utf8_invalido_no_rompe_help_y_falla_controlado_al_iniciar(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config"
            ruta = config_home / "parlar" / "config.json"
            ruta.parent.mkdir(parents=True)
            ruta.write_bytes(b"\xff")
            entorno = os.environ.copy()
            entorno["XDG_CONFIG_HOME"] = str(config_home)

            ayuda = ejecutar(sys.executable, "-B", "-m", "parlar", "--help",
                             env=entorno)
            self.assertEqual(ayuda.returncode, 0, ayuda.stderr)
            self.assertIn("usage: parlar", ayuda.stdout)
            self.assertNotIn("Traceback", ayuda.stderr)

            inicio = ejecutar(sys.executable, "-B", "-m", "parlar", env=entorno)
            self.assertEqual(inicio.returncode, 2)
            self.assertIn("[config] configuración inválida", inicio.stderr)
            self.assertIn(str(ruta), inicio.stderr)
            self.assertIn("UTF-8", inicio.stderr)
            self.assertNotIn("Traceback", inicio.stderr)
            self.assertNotIn("ParlAR: dictado", inicio.stdout)

    def test_parlarctl_sin_argumentos_muestra_uso_y_sale_2(self):
        resultado = ejecutar("./parlarctl")
        self.assertEqual(resultado.returncode, 2)
        self.assertIn("uso: parlarctl", resultado.stdout)

    def test_parlarctl_help_no_contacta_daemon(self):
        resultado = ejecutar("./parlarctl", "--help")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("usage: parlarctl", resultado.stdout)
        self.assertIn("cancelar", resultado.stdout)

    def test_inspeccion_config_no_carga_runtime_y_expone_hotkey(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config"
            ruta = config_home / "parlar" / "config.json"
            ruta.parent.mkdir(parents=True)
            ruta.write_text(json.dumps({
                "hotkey_toggle": "<ctrl>+<alt>+d",
                "context_terms": ["Amara"],
            }), encoding="utf-8")
            entorno = os.environ.copy()
            entorno["XDG_CONFIG_HOME"] = str(config_home)

            atajo = ejecutar(
                sys.executable, "-B", "-m", "parlar", "--mostrar-atajo",
                env=entorno)
            self.assertEqual(atajo.returncode, 0, atajo.stderr)
            self.assertEqual(atajo.stdout.strip(), "<ctrl>+<alt>+d")
            self.assertNotIn("[stt]", atajo.stdout)

            ruta_resultado = ejecutar(
                sys.executable, "-B", "-m", "parlar", "--config-path",
                env=entorno)
            self.assertEqual(ruta_resultado.returncode, 0, ruta_resultado.stderr)
            self.assertEqual(ruta_resultado.stdout.strip(), str(ruta))

            mostrada = ejecutar(
                sys.executable, "-B", "-m", "parlar", "--show-config",
                env=entorno)
            self.assertEqual(mostrada.returncode, 0, mostrada.stderr)
            datos = json.loads(mostrada.stdout)
            self.assertEqual(datos["hotkey_toggle"], "<ctrl>+<alt>+d")
            self.assertEqual(datos["context_terms"], ["Amara"])
            self.assertEqual(datos["schema_version"], 1)

    def test_atajo_cli_se_puede_persistir_sin_iniciar_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config"
            entorno = os.environ.copy()
            entorno["XDG_CONFIG_HOME"] = str(config_home)
            resultado = ejecutar(
                sys.executable, "-B", "-m", "parlar",
                "--atajo", "<ctrl>+<alt>+p",
                "--guardar-config", "--mostrar-atajo",
                env=entorno,
            )
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            self.assertIn("[config] guardada", resultado.stdout)
            self.assertTrue(resultado.stdout.rstrip().endswith("<ctrl>+<alt>+p"))
            guardada = json.loads(
                (config_home / "parlar" / "config.json").read_text())
            self.assertEqual(guardada["hotkey_toggle"], "<ctrl>+<alt>+p")

    def test_setup_instala_paquete_y_documenta_comandos(self):
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('pip install --no-deps "$REPO_DIR"', setup)
        self.assertIn("    parlar\n", setup)
        self.assertIn("`parlarctl alternar`", setup)

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
