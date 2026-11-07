"""Contrato de seguridad de la cadena de CI (GitHub Actions y Dependabot).

Sin red: lee los workflows, la configuración de Dependabot y la
documentación del repositorio. No fija SHAs concretos de acciones para que
las actualizaciones de Dependabot no rompan el gate; sí exige su forma y su
coherencia entre workflows.
"""

import re
import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
PYTHONS = ["3.12", "3.13", "3.14"]
DISPARADORES_PERMITIDOS = {"push", "pull_request", "workflow_dispatch",
                           "schedule"}
DISPARADORES_PELIGROSOS = {"pull_request_target", "workflow_run",
                           "issue_comment", "issues", "discussion",
                           "discussion_comment", "pull_request_review",
                           "pull_request_review_comment", "repository_dispatch",
                           "workflow_call", "fork", "watch", "release"}
DUENOS_PERMITIDOS = {"actions"}
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<resto>.*)$")
_USES_FIJADO = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s+(?P<version>v\d+\.\d+\.\d+)\s*$")
_IMAGEN = re.compile(
    r"^quay\.io/pypa/manylinux_2_28_x86_64@(?P<digest>sha256:[0-9a-f]{64})$")


def cargar(ruta: Path) -> dict:
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    # YAML 1.1 lee la clave ``on`` como booleano.
    if True in datos:
        datos["on"] = datos.pop(True)
    return datos


WORKFLOWS = {ruta.name: cargar(ruta)
             for ruta in sorted(WORKFLOWS_DIR.glob("*.yml"))}
TEXTOS = {ruta.name: ruta.read_text(encoding="utf-8")
          for ruta in sorted(WORKFLOWS_DIR.glob("*.yml"))}


def jobs():
    for archivo, workflow in WORKFLOWS.items():
        for nombre, job in workflow["jobs"].items():
            yield archivo, nombre, job


def pasos():
    for archivo, nombre, job in jobs():
        for paso in job.get("steps", []):
            yield archivo, nombre, paso


def paso_con(job: dict, fragmento: str) -> dict:
    for paso in job["steps"]:
        if fragmento in str(paso.get("run", "")):
            return paso
    raise AssertionError(f"no hay paso con {fragmento!r}")


def indice_con(job: dict, fragmento: str) -> int:
    return job["steps"].index(paso_con(job, fragmento))


class Inventario(unittest.TestCase):
    def test_workflows_esperados(self):
        self.assertEqual(set(WORKFLOWS),
                         {"tests.yml", "release.yml", "informational.yml"})


class AccionesFijadas(unittest.TestCase):
    def test_cada_uses_es_sha_completo_con_version(self):
        for archivo, texto in TEXTOS.items():
            for numero, linea in enumerate(texto.splitlines(), 1):
                coincidencia = _USES.match(linea)
                if not coincidencia:
                    continue
                with self.subTest(archivo=archivo, linea=numero):
                    self.assertRegex(coincidencia.group("resto"), _USES_FIJADO)

    def test_todos_los_uses_del_yaml_estan_fijados(self):
        # Evita que un ``uses`` con otro formato escape al chequeo por línea.
        lineas = sum(1 for t in TEXTOS.values() for l in t.splitlines()
                     if _USES.match(l))
        usos = [p["uses"] for _a, _j, p in pasos() if "uses" in p]
        self.assertEqual(len(usos), lineas)
        for uso in usos:
            with self.subTest(uso=uso):
                self.assertRegex(uso, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_solo_acciones_oficiales(self):
        for _archivo, _job, paso in pasos():
            if "uses" in paso:
                with self.subTest(uso=paso["uses"]):
                    self.assertIn(paso["uses"].split("/", 1)[0],
                                  DUENOS_PERMITIDOS)
                    self.assertFalse(paso["uses"].startswith("docker://"))

    def test_misma_accion_mismo_sha_y_version(self):
        vistos: dict[str, tuple[str, str]] = {}
        for texto in TEXTOS.values():
            for linea in texto.splitlines():
                coincidencia = _USES.match(linea)
                if not coincidencia:
                    continue
                fijado = _USES_FIJADO.match(coincidencia.group("resto"))
                self.assertIsNotNone(fijado, linea)
                valor = (fijado.group("sha"), fijado.group("version"))
                anterior = vistos.setdefault(fijado.group("repo"), valor)
                with self.subTest(repo=fijado.group("repo")):
                    self.assertEqual(anterior, valor)


class ImagenDeConstruccion(unittest.TestCase):
    def test_contenedor_fijado_por_digest(self):
        contenedores = [(a, n, j["container"]) for a, n, j in jobs()
                        if "container" in j]
        self.assertEqual([(a, n) for a, n, _c in contenedores],
                         [("release.yml", "wheelhouse")])
        imagen = contenedores[0][2]
        if isinstance(imagen, dict):
            imagen = imagen["image"]
        self.assertRegex(imagen, _IMAGEN)
        self.assertNotIn(":latest", TEXTOS["release.yml"])

    def test_digest_documentado(self):
        imagen = WORKFLOWS["release.yml"]["jobs"]["wheelhouse"]["container"]
        digest = _IMAGEN.match(imagen).group("digest")
        documento = (ROOT / "docs/security/supply-chain.md").read_text(
            encoding="utf-8")
        self.assertIn(digest, documento)

    def test_sin_servicios_ni_imagenes_sueltas(self):
        for archivo, nombre, job in jobs():
            with self.subTest(job=f"{archivo}:{nombre}"):
                self.assertNotIn("services", job)


class Permisos(unittest.TestCase):
    def test_solo_lectura_a_nivel_workflow(self):
        for archivo, workflow in WORKFLOWS.items():
            with self.subTest(archivo=archivo):
                self.assertEqual(workflow.get("permissions"),
                                 {"contents": "read"})

    def test_ningun_job_amplia_permisos(self):
        for archivo, nombre, job in jobs():
            with self.subTest(job=f"{archivo}:{nombre}"):
                self.assertNotIn("permissions", job)
        for archivo, texto in TEXTOS.items():
            with self.subTest(archivo=archivo):
                self.assertNotRegex(texto, r":\s*write\b")
                self.assertNotIn("write-all", texto)
                self.assertNotIn("secrets.", texto)


class Disparadores(unittest.TestCase):
    def test_sin_disparadores_peligrosos(self):
        for archivo, workflow in WORKFLOWS.items():
            disparadores = workflow["on"]
            nombres = ({disparadores} if isinstance(disparadores, str)
                       else set(disparadores))
            with self.subTest(archivo=archivo):
                self.assertLessEqual(nombres, DISPARADORES_PERMITIDOS)
                self.assertFalse(nombres & DISPARADORES_PELIGROSOS)

    def test_disparadores_por_workflow(self):
        self.assertEqual(set(WORKFLOWS["tests.yml"]["on"]),
                         {"push", "pull_request"})
        self.assertEqual(set(WORKFLOWS["release.yml"]["on"]),
                         {"workflow_dispatch"})
        for archivo in ("tests.yml", "informational.yml"):
            for evento in ("push", "pull_request"):
                with self.subTest(archivo=archivo, evento=evento):
                    self.assertEqual(
                        WORKFLOWS[archivo]["on"][evento]["branches"], ["main"])


class Credenciales(unittest.TestCase):
    def test_checkout_sin_credenciales_persistentes(self):
        checkouts = 0
        for archivo, nombre, paso in pasos():
            if paso.get("uses", "").startswith("actions/checkout@"):
                checkouts += 1
                with self.subTest(job=f"{archivo}:{nombre}"):
                    self.assertIs(
                        paso.get("with", {}).get("persist-credentials"), False)
        self.assertGreater(checkouts, 0)


class Timeouts(unittest.TestCase):
    def test_cada_job_tiene_timeout_razonable(self):
        for archivo, nombre, job in jobs():
            with self.subTest(job=f"{archivo}:{nombre}"):
                timeout = job.get("timeout-minutes")
                self.assertIsInstance(timeout, int)
                self.assertGreaterEqual(timeout, 1)
                self.assertLessEqual(timeout, 30)


class Interpolacion(unittest.TestCase):
    def test_run_no_interpola_expresiones(self):
        for archivo, nombre, paso in pasos():
            if "run" in paso:
                with self.subTest(job=f"{archivo}:{nombre}",
                                  paso=paso.get("name", paso["run"][:40])):
                    self.assertNotIn("${{", paso["run"])

    def test_sin_contextos_no_confiables(self):
        for archivo, texto in TEXTOS.items():
            with self.subTest(archivo=archivo):
                self.assertNotRegex(
                    texto, r"github\.(event|head_ref)\b")


class MatrizPython(unittest.TestCase):
    def matriz(self, archivo, job):
        return (WORKFLOWS[archivo]["jobs"][job]["strategy"]["matrix"]
                ["python-version"])

    def test_gates_cubren_312_313_314(self):
        for archivo in ("tests.yml", "release.yml"):
            with self.subTest(archivo=archivo):
                self.assertEqual(self.matriz(archivo, "gate"), PYTHONS)

    def test_release_conserva_smokes_en_todas_las_versiones(self):
        self.assertEqual(self.matriz("release.yml", "fresh-install-smoke"),
                         PYTHONS)
        wheelhouse = paso_con(WORKFLOWS["release.yml"]["jobs"]["wheelhouse"],
                              "build_wheelhouse.py")["run"]
        for version in PYTHONS:
            tag = "cp" + version.replace(".", "")
            with self.subTest(tag=tag):
                self.assertIn(f"/opt/python/{tag}-{tag}/bin/python", wheelhouse)

    def test_matriz_coherente_con_pyproject(self):
        proyecto = tomllib.loads((ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))
        self.assertEqual(proyecto["project"]["requires-python"],
                         f">={PYTHONS[0]}")


class DependenciasProbadas(unittest.TestCase):
    def setUp(self):
        self.constraints = {}
        for linea in (ROOT / "constraints.txt").read_text(
                encoding="utf-8").splitlines():
            linea = linea.split("#", 1)[0].strip()
            if linea:
                nombre, _sep, version = linea.partition("==")
                self.constraints[nombre.lower()] = version

    def test_gates_instalan_con_constraints_y_verifican(self):
        for archivo in ("tests.yml", "release.yml"):
            job = WORKFLOWS[archivo]["jobs"]["gate"]
            instalacion = paso_con(job, "pip install")["run"]
            with self.subTest(archivo=archivo):
                self.assertIn("-c constraints.txt", instalacion)
                self.assertIn("-r requirements.txt", instalacion)
                self.assertLess(
                    indice_con(job, "pip install"),
                    indice_con(job, "check_dependencies.py entorno"))
                self.assertLess(
                    indice_con(job, "check_dependencies.py entorno"),
                    indice_con(job, "./scripts/check.sh"))
                verificacion = paso_con(job, "check_dependencies.py entorno")
                self.assertNotIn("--informativo", verificacion["run"])
                self.assertNotIn("continue-on-error", verificacion)

    def test_gate_no_contradice_constraints(self):
        for archivo in ("tests.yml", "release.yml"):
            instalacion = paso_con(WORKFLOWS[archivo]["jobs"]["gate"],
                                   "pip install")["run"]
            for nombre, version in re.findall(
                    r"([A-Za-z0-9_.-]+)==([^\s]+)", instalacion):
                with self.subTest(archivo=archivo, paquete=nombre):
                    if nombre.lower() in self.constraints:
                        self.assertEqual(version,
                                         self.constraints[nombre.lower()])

    def test_toolchain_de_build_fijado_y_auditado(self):
        toolchain = "setuptools==84.1.0 wheel==0.48.0"
        self.assertIn(toolchain, paso_con(
            WORKFLOWS["release.yml"]["jobs"]["build"], "pip install")["run"])
        self.assertIn(toolchain, paso_con(
            WORKFLOWS["tests.yml"]["jobs"]["wheel-contract"],
            "pip install")["run"])
        auditoria = paso_con(WORKFLOWS["informational.yml"]["jobs"]["pip-audit"],
                             "check_dependencies.py auditar")["run"]
        for pin in toolchain.split():
            with self.subTest(pin=pin):
                self.assertIn(f"--build {pin}", auditoria)
        # Runtime y build usan el mismo setuptools: un aviso corregido en uno
        # no puede quedar abierto en el otro.
        self.assertEqual(toolchain.split()[0],
                         f"setuptools=={self.constraints['setuptools']}")

    def test_smoke_del_wheel_usa_constraints(self):
        instalacion = paso_con(WORKFLOWS["tests.yml"]["jobs"]["wheel-contract"],
                               "parlar-*.whl")["run"]
        self.assertIn("-c constraints.txt", instalacion)

    def test_job_reciente_aislado_e_informativo(self):
        job = WORKFLOWS["informational.yml"]["jobs"]["latest-dependencies"]
        instalacion = paso_con(job, "pip install")
        self.assertNotIn("constraints", instalacion["run"])
        self.assertIn("--upgrade", instalacion["run"])
        gate = paso_con(job, "./scripts/check.sh")
        self.assertTrue(instalacion.get("continue-on-error"))
        self.assertTrue(gate.get("continue-on-error"))
        self.assertNotIn("continue-on-error", job)
        reporte = paso_con(job, "GITHUB_STEP_SUMMARY")
        self.assertEqual(reporte.get("if"), "always()")
        self.assertEqual(reporte["env"]["GATE_OUTCOME"],
                         f"${{{{ steps.{gate['id']}.outcome }}}}")
        self.assertEqual(reporte["env"]["INSTALL_OUTCOME"],
                         f"${{{{ steps.{instalacion['id']}.outcome }}}}")
        self.assertIn("::warning", reporte["run"])
        self.assertIn("check_dependencies.py entorno --informativo",
                      reporte["run"])

    def test_sin_caches_compartidas(self):
        for archivo, nombre, paso in pasos():
            with self.subTest(job=f"{archivo}:{nombre}"):
                self.assertFalse(paso.get("uses", "").startswith("actions/cache"))
                self.assertNotIn("cache", paso.get("with", {}))

    def test_instalador_respeta_wheels_nativos_y_cuda(self):
        instalador = (ROOT / "scripts/parlar_installer.py").read_text(
            encoding="utf-8")
        self.assertIn('"--only-binary=:all:"', instalador)
        self.assertIn('self.origen.objetivo("[cuda]")', instalador)
        self.assertIn('"-c", str(self.origen.constraints)', instalador)
        proyecto = tomllib.loads((ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))
        for requisito in proyecto["project"]["optional-dependencies"]["cuda"]:
            nombre, _sep, version = requisito.partition("==")
            with self.subTest(extra=nombre):
                self.assertEqual(self.constraints[nombre.lower()], version)


class AuditoriaVisible(unittest.TestCase):
    def setUp(self):
        self.job = WORKFLOWS["informational.yml"]["jobs"]["pip-audit"]

    def test_pip_audit_instalado_con_hashes_en_venv_propio(self):
        instalacion = paso_con(self.job, "pip-audit.txt")["run"]
        self.assertIn("--require-hashes", instalacion)
        self.assertIn('python -m venv "$RUNNER_TEMP/pip-audit"', instalacion)

    def test_lock_de_pip_audit_tiene_hash_por_paquete(self):
        texto = (ROOT / ".github/requirements/pip-audit.txt").read_text(
            encoding="utf-8")
        bloques = re.split(r"\n(?=[A-Za-z0-9])",
                           "\n".join(l for l in texto.splitlines()
                                     if not l.startswith("#")).strip())
        nombres = []
        for bloque in bloques:
            with self.subTest(bloque=bloque.split()[0]):
                self.assertRegex(bloque, r"^[a-z0-9-]+==[^\s\\]+ \\")
                self.assertRegex(bloque, r"--hash=sha256:[0-9a-f]{64}")
                nombres.append(bloque.split("==")[0])
        self.assertIn("pip-audit", nombres)

    def test_resultado_visible_y_no_oculto(self):
        auditoria = paso_con(self.job, "check_dependencies.py auditar")
        self.assertNotIn("continue-on-error", auditoria)
        self.assertNotIn("continue-on-error", self.job)
        self.assertNotIn("--estricto", auditoria["run"])
        self.assertIn("--runtime-python", auditoria["run"])
        subida = [p for p in self.job["steps"]
                  if p.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(subida), 1)
        self.assertEqual(subida[0].get("if"), "always()")
        self.assertEqual(subida[0]["with"]["name"], "pip-audit-report")
        script = (ROOT / "scripts/check_dependencies.py").read_text(
            encoding="utf-8")
        self.assertIn("GITHUB_STEP_SUMMARY", script)

    def test_runtime_auditado_con_constraints(self):
        runtime = paso_con(self.job, '"$RUNNER_TEMP/runtime/bin/python"')["run"]
        self.assertIn("-c constraints.txt", runtime)
        self.assertIn("-r requirements.txt", runtime)
        self.assertIn("-r requirements-optional.txt", runtime)

    def test_actionlint_verificado_e_informativo(self):
        job = WORKFLOWS["informational.yml"]["jobs"]["actionlint"]
        instalacion = paso_con(job, "sha256sum -c")
        self.assertRegex(instalacion["env"]["ACTIONLINT_VERSION"],
                         r"^\d+\.\d+\.\d+$")
        self.assertRegex(instalacion["env"]["ACTIONLINT_SHA256"],
                         r"^[0-9a-f]{64}$")
        self.assertIn("https://github.com/rhysd/actionlint/releases/download/",
                      instalacion["run"])
        self.assertIn("--proto '=https'", instalacion["run"])
        # La verificación ocurre antes de extraer y ejecutar.
        self.assertLess(instalacion["run"].index("sha256sum -c"),
                        instalacion["run"].index("tar -xzf"))
        ejecucion = paso_con(job, "-oneline")
        self.assertIn("GITHUB_STEP_SUMMARY", ejecucion["run"])
        self.assertIn("::warning", ejecucion["run"])
        self.assertNotIn("continue-on-error", ejecucion)

    def test_informativos_no_estan_en_el_gate(self):
        texto = TEXTOS["tests.yml"]
        for herramienta in ("pip-audit", "actionlint", "--upgrade"):
            with self.subTest(herramienta=herramienta):
                self.assertNotIn(herramienta, texto)


class Dependabot(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(
            (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))

    def test_estructura_valida(self):
        self.assertEqual(self.config["version"], 2)
        ecosistemas = [u["package-ecosystem"] for u in self.config["updates"]]
        self.assertEqual(sorted(ecosistemas), ["github-actions", "pip"])
        for update in self.config["updates"]:
            with self.subTest(ecosistema=update["package-ecosystem"]):
                self.assertEqual(update["schedule"]["interval"], "weekly")
                self.assertIn(update["schedule"]["day"],
                              {"monday", "tuesday", "wednesday", "thursday",
                               "friday", "saturday", "sunday"})
                limite = update["open-pull-requests-limit"]
                self.assertTrue(1 <= limite <= 5)
                self.assertGreaterEqual(update["cooldown"]["default-days"], 1)
                self.assertTrue(update["groups"])
                for grupo in update["groups"].values():
                    self.assertTrue(grupo["patterns"])
                    self.assertLessEqual(set(grupo["update-types"]),
                                         {"minor", "patch"})
                self.assertTrue(("directory" in update)
                                ^ ("directories" in update))
                self.assertNotIn("labels", update)

    def test_directorios_existen(self):
        for update in self.config["updates"]:
            directorios = update.get("directories") or [update["directory"]]
            for directorio in directorios:
                with self.subTest(directorio=directorio):
                    self.assertTrue(directorio.startswith("/"))
                    self.assertTrue((ROOT / directorio.lstrip("/")).is_dir())
        pip = next(u for u in self.config["updates"]
                   if u["package-ecosystem"] == "pip")
        self.assertEqual(set(pip["directories"]),
                         {"/", "/.github/requirements"})


class CompatibilidadWorkflows(unittest.TestCase):
    def test_grafo_del_release_intacto(self):
        jobs_release = WORKFLOWS["release.yml"]["jobs"]
        self.assertEqual(set(jobs_release), {"gate", "wheelhouse", "build",
                                             "checksum", "fresh-install-smoke"})
        self.assertEqual(jobs_release["build"]["needs"], ["gate", "wheelhouse"])
        self.assertEqual(jobs_release["checksum"]["needs"], "build")
        self.assertEqual(jobs_release["fresh-install-smoke"]["needs"], "build")

    def test_smokes_de_instalacion_update_y_rollback(self):
        job = WORKFLOWS["release.yml"]["jobs"]["fresh-install-smoke"]
        checkout = next(p for p in job["steps"]
                        if p.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        smoke = paso_con(job, "test_release_install.sh")
        self.assertIn("./scripts/test_release_install.sh release", smoke["run"])
        self.assertIn("--skip-legacy", smoke["run"])
        self.assertEqual(smoke["env"]["PYTHON_VERSION"],
                         "${{ matrix.python-version }}")

    def test_build_reproducible_y_artifacts(self):
        build = WORKFLOWS["release.yml"]["jobs"]["build"]
        reproducible = paso_con(build, "build_release.py")["run"]
        self.assertIn("cmp ", reproducible)
        self.assertIn("git diff --exit-code", reproducible)
        subidos = {p["with"]["name"] for _a, _n, p in pasos()
                   if p.get("uses", "").startswith("actions/upload-artifact@")}
        bajados = {p["with"]["name"] for _a, _n, p in pasos()
                   if p.get("uses", "").startswith("actions/download-artifact@")}
        self.assertLessEqual(bajados, subidos)
        self.assertLessEqual({"parlar-wheelhouse", "parlar-release"}, subidos)

    def test_tests_conserva_gate_y_wheel_contract(self):
        self.assertEqual(set(WORKFLOWS["tests.yml"]["jobs"]),
                         {"gate", "wheel-contract"})


if __name__ == "__main__":
    unittest.main()
