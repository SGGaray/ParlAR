# Cadena de suministro de CI y dependencias

Este documento describe los controles de GitHub Actions y de dependencias de
ParlAR, cómo actualizarlos y el estado de los avisos de seguridad conocidos
sobre las dependencias. Complementa a [SECURITY.md](../../SECURITY.md).

## Workflows

| Workflow | Disparadores | Rol |
| --- | --- | --- |
| `tests.yml` | `push` y `pull_request` a `main` | Gate obligatorio: calidad (3.12, 3.13, 3.14) y contrato del wheel |
| `release.yml` | `workflow_dispatch` | Construye y valida el release; no publica |
| `informational.yml` | `push`, `pull_request`, semanal, manual | Controles informativos: dependencias recientes, pip-audit y actionlint |

Controles comunes, verificados por `tests/test_ci_supply_chain.py`:

- **Permisos mínimos.** Todos los workflows declaran sólo `contents: read` a
  nivel de workflow y ningún job los amplía.
- **Disparadores.** Sólo `push`, `pull_request`, `workflow_dispatch` y
  `schedule`. No se usan `pull_request_target`, `workflow_run`,
  `issue_comment` ni otros que ejecuten código de un fork con permisos o
  secretos del repositorio.
- **Credenciales.** Todo `actions/checkout` usa `persist-credentials: false`:
  ningún job necesita volver a autenticarse contra el repositorio después
  del checkout.
- **Timeouts.** Cada job tiene `timeout-minutes` (entre 5 y 25 minutos; las
  ejecuciones validadas duran menos de 3 minutos por job).
- **Expresiones en shell.** Ningún bloque `run:` interpola `${{ ... }}`. Los
  valores del contexto llegan como variables de entorno.

### Acciones fijadas por SHA

Cada `uses:` apunta al SHA completo (40 caracteres) del commit de una release
del repositorio oficial y lleva la versión como comentario (`# vX.Y.Z`), el
formato que Dependabot actualiza.

| Acción | Versión | Commit | Verificación (2026-11-07) |
| --- | --- | --- | --- |
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | `v7` y `v7.0.1` apuntan a este commit; commit firmado y verificado por GitHub |
| `actions/setup-python` | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` | `v7` y `v7.1.0` apuntan a este commit; firmado y verificado |
| `actions/upload-artifact` | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` | `v7` y `v7.0.2` apuntan a este commit; firmado y verificado |
| `actions/download-artifact` | v8.0.2 | `9000827ccba6bdab643e8b6fd33ac0654aef8333` | `v8` y `v8.0.2` apuntan a este commit; firmado y verificado |

Las cuatro son acciones JavaScript del repositorio oficial `actions/*` (no
forks) y no invocan otras acciones, así que el pinning individual cubre toda
la cadena de acciones que usa ParlAR hoy.

Para verificar un SHA antes de aceptar una actualización:

```sh
gh api repos/actions/checkout/git/ref/tags/v7.0.1 --jq .object
gh api repos/actions/checkout/commits/<sha> --jq .commit.verification
```

Si el tag es anotado (`type: tag`), el commit es el `object.sha` de
`gh api repos/<repo>/git/tags/<sha>`.

### Imagen de construcción manylinux

El job `wheelhouse` de `release.yml` corre en:

```
quay.io/pypa/manylinux_2_28_x86_64@sha256:7e41b1c7fd64dfc69703a1124b43c404382973ee06ec5d764b85c34d2221fded
```

- Corresponde al tag `2026.11.01-2` de `quay.io/pypa/manylinux_2_28_x86_64`.
- Es la imagen que descargó el último release validado: la run
  42269746390 de `release.yml` (2026-11-06) registró ese mismo digest al
  hacer `docker pull` de `:latest`.
- El manifest (`application/vnd.docker.distribution.manifest.v2+json`,
  imagen de una sola plataforma) se descargó por digest y por tag, y su
  SHA-256 coincide con el digest en los dos casos.
- La ejecución de `release.yml` sobre `main` del 2026-10-05 (run
  37250481656) usó una imagen anterior, `sha256:39df0042d5cc900b085aa25a0659368b42a0006c54c474299b785b44c1b4ff82`.
- En el momento de fijarla, `:latest` ya apuntaba a otra imagen
  (`2026.11.06-1`, `sha256:ae830e2e…`) que no se usó en ningún release
  validado.

Dependabot no actualiza imágenes declaradas en `container:` de un workflow.
Para cambiarla:

1. Elegir un tag fechado y obtener su digest:
   `curl -fsSI -H "Accept: application/vnd.docker.distribution.manifest.v2+json" https://quay.io/v2/pypa/manylinux_2_28_x86_64/manifests/<tag>`
   (cabecera `docker-content-digest`).
2. Actualizar el digest en `release.yml` y en este documento (la prueba
   exige que coincidan).
3. Ejecutar `release.yml` y confirmar que el release se construye, que los
   bytes son reproducibles y que los smokes de instalación pasan.

### actionlint

Se instala desde la release oficial `rhysd/actionlint` v1.7.12
(`actionlint_1.7.12_linux_amd64.tar.gz`). El job verifica el SHA-256
`8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8` antes de
extraerlo. Ese valor coincide con el archivo de checksums publicado en la
release y con el digest que informa la API de GitHub para el asset. Para
actualizarlo hay que cambiar la versión y el hash juntos, después de
comprobar el archivo `actionlint_<versión>_checksums.txt` de la release.

### Herramientas no incorporadas

- **zizmor:** no se agrega por ahora. Las pruebas de
  `tests/test_ci_supply_chain.py` cubren pinning, permisos, disparadores,
  credenciales e interpolación, y actionlint detecta expresiones no
  confiables en scripts.
- **CodeQL:** GitHub ya lo ejecuta con la configuración por defecto para
  Python y Actions. No se duplica en un workflow.

### Lockfiles de dependencias de workflows (adopción futura)

Todavía no se usan. Un lockfile de workflows registraría la resolución
completa de cada acción y workflow reutilizable, incluidas las acciones que
una acción compuesta invoca por tag. Frente al pinning individual:

- **Dependencias transitivas.** El pinning de un `uses:` no fija lo que esa
  acción referencia por dentro. Un lockfile sí.
- **Fuente única.** Las versiones resueltas quedan en un archivo
  revisable, en lugar de repartidas entre los `uses:` de cada workflow.
- **Integridad.** Una herramienta puede verificar el lockfile contra el
  contenido de las acciones y detectar un tag movido o un SHA inesperado.

Hoy el beneficio es acotado porque las cuatro acciones son JavaScript sin
acciones transitivas. Conviene reevaluarlo si se agregan acciones compuestas,
workflows reutilizables o acciones de terceros, o cuando GitHub Actions
ofrezca ese mecanismo de forma estable.

## Dependencias de Python

### Qué versiones se prueban

- **Gate obligatorio** (`tests.yml` y el gate de `release.yml`): instala
  `requirements.txt` con `-c constraints.txt`, la misma resolución que aplica
  el instalador del release. Un paso previo a `scripts/check.sh` falla si
  alguna versión instalada difiere de `constraints.txt`. setuptools llega
  fijado por `constraints.txt` porque ctranslate2 lo requiere en runtime;
  con él se construye el wheel de las pruebas.
- **Wheel publicado:** lo construye el toolchain fijado
  `setuptools==84.1.0 wheel==0.48.0` (`wheel-contract` y `release.yml`), con
  el mismo setuptools que fija `constraints.txt`; una prueba exige que
  coincidan. El smoke del wheel instalado también aplica `constraints.txt`.
- **Dependencias más recientes** (`informational.yml`,
  `latest-dependencies`): instala la última versión de cada dependencia sin
  `constraints.txt` y corre el gate en 3.12 y 3.14. Corre en su propio
  runner, sin caché ni artifacts compartidos, y publica en el resumen el
  resultado real y las versiones que difieren de `constraints.txt`. No es
  bloqueante.
- **Wheels nativos y CUDA:** no cambian. evdev y webrtcvad-wheels se
  compilan en el wheelhouse manylinux cuando PyPI no publica un wheel para
  ese CPython. El instalador instala con `--only-binary=:all:` y
  `constraints.txt`, y agrega el extra `[cuda]` sólo si CTranslate2 detecta
  una GPU.

### pip-audit

El job `pip-audit` de `informational.yml` instala pip-audit en un venv
propio desde `.github/requirements/pip-audit.txt`, un lock con versión y
SHA-256 de cada paquete instalado con `--require-hashes`. Después ejecuta
`scripts/check_dependencies.py auditar`, que corre pip-audit sin instalar
nada sobre cuatro inventarios separados:

| Inventario | Contenido |
| --- | --- |
| `constraints` | Versiones enumeradas en `constraints.txt` |
| `runtime` | Paquetes realmente instalados en un venv de runtime CPU: dependencias de ParlAR y VAD opcional con `constraints.txt`, más el pip del venv |
| `extras` | Extras de `pyproject.toml` (`vad`, `cuda`) con la versión de `constraints.txt`; los wheels CUDA no se descargan |
| `build` | Herramientas de construcción del release (`setuptools`, `wheel`) |

El resumen del job muestra el estado de cada inventario y una tabla de
avisos con el inventario donde aparece cada versión afectada. Cada paquete
afectado genera una anotación `warning`. El reporte JSON se sube como
artifact `pip-audit-report`. El job falla sólo si la auditoría no pudo
ejecutarse. Con `--estricto` también fallaría ante un aviso; se activa
cuando el modo informativo deje de ser necesario.

Que una versión aparezca en un inventario no implica que el aviso sea
explotable en ParlAR. El análisis de cada aviso está abajo.

## Avisos conocidos y remediación

pip-audit 2.10.1 reportó 16 avisos en `main` (`8d59bc7`, run 42367116551
del workflow `informational`). Todos estaban en los inventarios
`constraints` y `runtime`, salvo setuptools 84.0.0, que estaba en `build`.
La remediación del 2026-11-07 actualizó sólo los paquetes afectados:

| Paquete | Antes | Después | Avisos cerrados |
| --- | --- | --- | --- |
| certifi | 2026.6.17 | 2026.10.23 | 1 |
| filelock | 3.29.6 | 4.2.6 | 9 |
| anyio | 4.14.1 | 4.14.2 | 3 |
| setuptools (runtime) | 80.10.2 | 84.1.0 | 2 |
| setuptools (build) | 84.0.0 | 84.1.0 | 1 |

Criterio para elegir cada versión:

- **certifi 2026.10.23:** la más reciente. Es un paquete sólo de datos y
  conviene el bundle de raíces más actual. Es también la versión que ya
  resuelve pip y la del lock de pip-audit.
- **filelock 4.2.6:** incluye todas las correcciones de seguridad (4.2.1)
  y las de carreras en `SoftFileLock` de 4.2.5 y 4.2.6, que es el fallback
  de huggingface_hub. Las 4.2.7 a 4.2.9 sólo tocan `SoftFileLease`,
  `ReadWriteLock` y documentación, que ParlAR no usa, y tenían menos de 7
  días publicadas, el mismo margen que aplica Dependabot.
- **anyio 4.14.2:** el parche mínimo que corrige los tres avisos.
- **setuptools 84.1.0:** la primera versión que corrige GHSA-xmj3. Las
  84.2.x agregan funciones (abi3t) que ParlAR no necesita. Runtime y build
  usan la misma versión.

Después del cambio, pip-audit no reporta avisos en los inventarios
`constraints`, `extras` y `build`. El inventario `runtime` también incluye
el pip del venv, que no fija `constraints.txt` (ver
[pip del venv de instalación](#pip-del-venv-de-instalación-abierto)).

El análisis de cada aviso, abajo, se conserva como registro.

### certifi 2026.6.17

- **Aviso:** PYSEC-2026-4320 / GHSA-6q48-6c7w-9gw2.
- **Corregida en:** 2026.10.18. El aviso recomienda 2026.10.19 o posterior:
  2026.10.18 quitó todas las raíces emSign y 2026.10.19 restauró las no
  afectadas.
- **Condiciones de explotación:** el bundle incluye la raíz
  `emSign ECC Root CA - G3`, bajo la cual ocho intermedias tienen claves
  privadas comprometidas. Un atacante con una de esas claves que además esté
  en el camino de red puede interceptar o modificar TLS de los clientes que
  confían en el bundle de certifi.
- **Presencia:**
  - Runtime, como dependencia de httpx.
  - huggingface_hub usa httpx para descargar el modelo de Whisper, y httpx
    construye su contexto TLS con `certifi.where()`.
  - La reescritura con Ollama usa `urllib` con el almacén de certificados
    del sistema, no certifi.
  - El pip que usa el instalador trae su propio certifi (2026.10.19 en pip
    26.2.2).
- **Prioridad:** alta entre estos avisos. Afecta la integridad de la
  descarga del modelo y la corrección es sólo de datos.
- **Estado:** corregido con 2026.10.23. Ese bundle ya no incluye
  `emSign ECC Root CA - G3`, `emSign Root CA - C1` ni
  `emSign ECC Root CA - C3`, y conserva `emSign Root CA - G1`. Se verificó
  la descarga del modelo por HTTPS con huggingface_hub y httpx.

### filelock 3.29.6

- **Avisos:**
  - Corregidos en 4.2.1: CVE-2026-112907, CVE-2026-112909, CVE-2026-112910,
    CVE-2026-112913, CVE-2026-112915, CVE-2026-112935, CVE-2026-112939 y
    CVE-2026-112940.
  - Corregido en 4.0.9: CVE-2026-112936.
  - Los GHSA correspondientes figuran en el reporte de pip-audit.
- **Condiciones de explotación:** todos requieren otra cuenta local con
  capacidad de crear, renombrar o enlazar archivos en el directorio del
  lock: un directorio compartido sin sticky bit, `/tmp` con protecciones
  de hardlinks desactivadas o, en Windows, ACL heredadas permisivas.
  - Solo Windows: CVE-2026-112907, CVE-2026-112910 y CVE-2026-112913.
  - Solo en `ReadWriteLock`/`AsyncReadWriteLock` (SQLite):
    CVE-2026-112915, CVE-2026-112936 y CVE-2026-112940.
  - En `UnixFileLock`: CVE-2026-112909.
  - En los soft locks: CVE-2026-112935 y CVE-2026-112939.
- **Presencia:**
  - Runtime, como dependencia de huggingface_hub, que usa `FileLock`
    (`mode=0o664`) en la caché de modelos y `SoftFileLock` sólo si el
    sistema de archivos no soporta `flock`.
  - No usa `ReadWriteLock`.
  - La caché por defecto está en el home del usuario y otras cuentas no
    pueden escribir en ella. Además, el modelo de amenaza de ParlAR trata a
    los procesos del mismo UID como un único dominio de confianza.
- **Prioridad:** media-baja. Sólo es relevante en equipos multiusuario con
  `HF_HOME` en un directorio compartido.
- **Estado:** corregido con 4.2.6.
  - El cambio de versión mayor (4.0.0) sólo modifica el protocolo en disco
    de `SoftReadWriteLock`, que huggingface_hub no usa.
  - `WeakFileLock` de huggingface_hub 1.22.0 sigue creando el lock con
    `UnixFileLock` y modo `0o664`, y excluye a otro poseedor.
  - El fallback a soft lock en sistemas de archivos sin `flock` lo hace
    filelock en ambas versiones, con la misma lógica.
  - Se verificó la descarga del modelo, la reutilización de la caché sin
    red (`HF_HUB_OFFLINE=1`) y una transcripción idéntica a la de
    3.29.6.

### anyio 4.14.1

- **Avisos:** PYSEC-2026-4023 (CVE-2026-63349), PYSEC-2026-4024
  (CVE-2026-64847) y PYSEC-2026-4025 (CVE-2026-63374).
- **Corregida en:** 4.14.2.
- **Condiciones de explotación:**
  - PYSEC-2026-4025: nombres de dominio internacionalizados en
    `connect_tcp()` o `TLSStream.wrap()` asíncronos, con la conexión ya
    desviada a un servidor malicioso.
  - PYSEC-2026-4023: `extra_groups` mal reenviado en `run_process()` y
    `open_process()`.
  - PYSEC-2026-4024: bloqueo de workers de process pool que escriben mucho
    en stderr.
- **Presencia:**
  - Runtime, como dependencia obligatoria de httpx.
  - ParlAR no importa anyio.
  - huggingface_hub descarga con el cliente síncrono de httpx, cuyo backend
    (httpcore sync) no pasa por anyio.
  - No hay process pools ni `extra_groups`, y los hosts son ASCII.
- **Prioridad:** baja.
- **Estado:** corregido con 4.14.2, un cambio de parche.

### setuptools 80.10.2 (runtime) y 84.0.0 (build)

- **Avisos:**
  - PYSEC-2026-3447 / CVE-2026-59890 / GHSA-h35f-9h28-mq5c: corregido en
    83.0.0. Afecta sólo a 80.10.2.
  - GHSA-xmj3-9gf7-h52w: corregido en 84.1.0. Afecta a 80.10.2 y a 84.0.0.
- **Condiciones de explotación:**
  - CVE-2026-59890: las exclusiones de `MANIFEST.in` se pueden saltear al
    construir un sdist en macOS APFS/HFS+ (normalización Unicode).
  - GHSA-xmj3: en Windows, un wheel malicioso puede crear un `__init__.py`
    fuera del destino en la ruta legacy `install_as_egg`.
- **Presencia:**
  - 80.10.2 se instala en el runtime porque ctranslate2 declara
    `setuptools` como dependencia, pero ParlAR no construye ni instala
    paquetes con él en ejecución.
  - 84.0.0 sólo existe en el job de build del release, que construye un
    wheel (no un sdist) en Linux.
  - Ninguna de las dos condiciones (macOS sdist, Windows egg) se da en
    ParlAR.
- **Prioridad:** baja.
- **Estado:** corregido con 84.1.0 en runtime y en build.
  - En runtime, ctranslate2 4.8.1 no importa `setuptools` ni
    `pkg_resources`. webrtcvad-wheels sólo lo usa como fallback en Python
    anterior a 3.8.
  - El release construido dos veces con 84.1.0 da bytes idénticos. Frente a
    84.0.0, el wheel sólo cambia en la línea `Generator` de `WHEEL` y su
    hash en `RECORD`.

### Observación sobre la instalación activa

Una instalación local revisada en modo lectura (`1.1.0`) tenía
`certifi 2026.10.23`, distinta del pin de `constraints.txt` de ese
momento (2026.6.17). Esto muestra que el runtime de una instalación
existente puede no coincidir con `constraints.txt`. El inventario `runtime`
de CI describe la resolución actual del instalador, no cada instalación de
usuario.

### pip del venv de instalación (abierto)

- **Origen:** el instalador crea el venv con `python -m venv` usando el
  Python del sistema y no actualiza pip. El venv queda con el pip que trae
  ese Python (`ensurepip` o el paquete de la distribución), y
  `constraints.txt` no lo fija.
- **Versiones observadas:**
  - En CI (`actions/setup-python`), pip 26.2.2, sin avisos conocidos.
  - Con el Python 3.14 de Fedora 43, pip 25.1.1, que tiene seis avisos:
    PYSEC-2026-1795, PYSEC-2026-1796, PYSEC-2026-196, PYSEC-2026-2875,
    PYSEC-2026-2876 y PYSEC-2026-3721. Todos están corregidos en 26.2.
- **Condiciones de explotación:**
  - Todos requieren un wheel o archivo malicioso, o un índice de paquetes
    malicioso.
  - El instalador usa PyPI por HTTPS con `--only-binary=:all:` y
    `constraints.txt`, y verifica el SHA-256 del wheel de ParlAR.
  - PYSEC-2026-2876 depende del chequeo de actualización de pip, que el
    instalador desactiva con `--disable-pip-version-check`.
  - ParlAR no ejecuta pip después de instalarse.
- **Prioridad:** media-baja. El riesgo es el de una dependencia de PyPI
  comprometida, que estas correcciones acotan pero no eliminan.
- **Estado:** abierto. Corregirlo cambia el comportamiento del instalador
  (actualizar pip a una versión fijada antes de instalar dependencias), así
  que queda para un cambio separado.

## Dependabot

`.github/dependabot.yml` revisa semanalmente `github-actions` y `pip` (el
directorio raíz y `.github/requirements`):

- Agrupa en un solo PR las actualizaciones minor y patch de cada
  ecosistema y deja las major en PRs individuales.
- Espera 7 días desde la publicación de una versión antes de proponerla.
- Limita los PRs abiertos a 3 (Actions) y 5 (pip).

Las alertas y las security updates de Dependabot se habilitan en la
configuración del repositorio.
