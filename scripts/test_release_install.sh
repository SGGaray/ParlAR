#!/usr/bin/env bash
# Prueba de mantenimiento: instala el release real en un HOME descartable.
#
#   scripts/test_release_install.sh DIR_RELEASE [opciones]
#
# DIR_RELEASE contiene parlar-<versión>-linux-x86_64.tar.gz y SHA256SUMS.
#
#   --python RUTA      Python de bootstrap (por defecto: el que elija install.sh)
#   --legacy-ref REF   ref git de la versión legacy a migrar (por defecto v1.0.1)
#   --skip-legacy      omite la prueba de upgrade desde 1.0.x
#   --pip-cache DIR    caché de pip compartida entre fases (por defecto, en el
#                      directorio temporal)
#   --keep             conserva el directorio temporal para inspección
#
# Nunca escribe en el HOME real: HOME, XDG_*, PATH y caché apuntan a mktemp y
# systemctl/update-desktop-database/gtk-update-icon-cache son shims que sólo
# registran la llamada. Requiere red (pip descarga wheels de PyPI).
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
REAL_HOME="$HOME"
RELEASE_DIR=""
BOOTSTRAP=""
LEGACY_REF="v1.0.1"
SKIP_LEGACY=0
PIP_CACHE=""
KEEP=0

while (($#)); do
    case "$1" in
        --python) BOOTSTRAP="$2"; shift ;;
        --legacy-ref) LEGACY_REF="$2"; shift ;;
        --skip-legacy) SKIP_LEGACY=1 ;;
        --pip-cache) PIP_CACHE="$2"; shift ;;
        --keep) KEEP=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        -*) echo "opción desconocida: $1" >&2; exit 2 ;;
        *) RELEASE_DIR="$1" ;;
    esac
    shift
done
[[ -n "$RELEASE_DIR" ]] || { echo "falta DIR_RELEASE" >&2; exit 2; }
RELEASE_DIR="$(CDPATH= cd -- "$RELEASE_DIR" && pwd -P)"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/parlar-release-test.XXXXXX")"
if ((KEEP)); then
    trap 'echo "temporal conservado: $TMP"' EXIT
else
    trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf -- "$TMP"' EXIT
fi
PASOS=0

falla() { echo "FAIL: $*" >&2; exit 1; }
ok() { PASOS=$((PASOS + 1)); echo "  ok: $*"; }
igual() { [[ "$1" == "$2" ]] || falla "$3: '$1' != '$2'"; ok "$3"; }
existe() { [[ -e "$1" || -L "$1" ]] || falla "falta $1"; }
ausente() { [[ ! -e "$1" && ! -L "$1" ]] || falla "no debería existir: $1"; }
huella() { (cd "$1" && find . -printf '%p %y %l\n' | sort && find . -type f -exec sha256sum {} + | sort); }

# Configura un HOME aislado en $1 y exporta las variables del entorno de prueba.
preparar_home() {
    local raiz="$1"
    H="$raiz/home"
    DATA="$H/.local/share"
    CONF="$H/.config"
    CACHE="$raiz/cache"
    RUNTIME="$raiz/run"
    SHIMS="$raiz/shims"
    NOCC="$raiz/sin-compilador"
    mkdir -p "$H" "$CACHE" "$RUNTIME" "$SHIMS" "$NOCC"
    chmod 700 "$RUNTIME"
    # El release no debe compilar: cualquier compilador invocado falla.
    for compilador in cc gcc c++ g++ clang x86_64-linux-gnu-gcc; do
        printf '#!/bin/sh\necho "compilador invocado: %s" >&2\nexit 1\n' \
            "$compilador" > "$NOCC/$compilador"
        chmod +x "$NOCC/$compilador"
    done
    BLOQUEO_CC="$NOCC:"
    for shim in systemctl update-desktop-database gtk-update-icon-cache; do
        printf '#!/bin/sh\nprintf "%%s %%s\\n" "%s" "$*" >> "%s/shims.log"\nexit 0\n' \
            "$shim" "$raiz" > "$SHIMS/$shim"
        chmod +x "$SHIMS/$shim"
    done
    # Assert inicial: ningún destino puede caer en el HOME real.
    local ruta
    for ruta in "$H" "$DATA" "$CONF" "$CACHE" "$RUNTIME"; do
        case "$ruta" in
            "$REAL_HOME"|"$REAL_HOME"/*) falla "ruta de prueba dentro del HOME real: $ruta" ;;
        esac
        [[ "$ruta" == "$TMP"/* ]] || falla "ruta de prueba fuera del temporal: $ruta"
    done
}

en_home() {
    local entorno=(
        HOME="$H" XDG_DATA_HOME="$DATA" XDG_CONFIG_HOME="$CONF"
        XDG_CACHE_HOME="$CACHE" XDG_RUNTIME_DIR="$RUNTIME"
        PATH="$SHIMS:$BLOQUEO_CC/usr/bin:/bin" LANG=C.UTF-8
        PIP_CACHE_DIR="${PIP_CACHE:-$TMP/pip-cache}"
        PIP_DISABLE_PIP_VERSION_CHECK=1
    )
    [[ -n "$BOOTSTRAP" ]] && entorno+=(PARLAR_BOOTSTRAP_PYTHON="$BOOTSTRAP")
    env -i "${entorno[@]}" "$@"
}

echo "==> Verificando SHA256SUMS"
(cd "$RELEASE_DIR" && sha256sum -c SHA256SUMS) || falla "sha256sum -c"
TARBALL="$(find "$RELEASE_DIR" -maxdepth 1 -name 'parlar-*-linux-x86_64.tar.gz' | head -n1)"
[[ -n "$TARBALL" ]] || falla "no hay tarball en $RELEASE_DIR"
mkdir -p "$TMP/extraido"
tar -xzf "$TARBALL" -C "$TMP/extraido"
REL="$(find "$TMP/extraido" -mindepth 1 -maxdepth 1 -type d)"
VERSION="$(cat "$REL/VERSION")"
[[ ! -e "$REL/.git" ]] || falla "el release contiene .git"
ok "release $VERSION extraído sin .git"

# ---------------------------------------------------------------------------
echo "==> Instalación limpia"
preparar_home "$TMP/limpio"
en_home "$REL/install.sh" --cpu-only >"$TMP/limpio/install1.log" 2>&1 \
    || { tail -30 "$TMP/limpio/install1.log"; falla "install.sh"; }
INST="$DATA/parlar"
CURRENT1="$(readlink "$INST/current")"
[[ "$CURRENT1" == versions/"$VERSION"-* ]] || falla "current inesperado: $CURRENT1"
ok "current -> $CURRENT1"
igual "$(en_home "$H/.local/bin/parlar" --version)" "ParlAR $VERSION" "parlar --version"
for comando in parlar parlarctl; do
    igual "$(readlink "$H/.local/bin/$comando")" "$INST/current/venv/bin/$comando" "enlace $comando"
done
igual "$(readlink "$H/.local/bin/parlar-uninstall")" "$INST/uninstall.sh" "enlace parlar-uninstall"
DESKTOP="$DATA/applications/parlar.desktop"
grep -qxF "Exec=\"$INST/current/venv/bin/parlar\" --abrir-configuracion" "$DESKTOP" \
    || falla "Exec del launcher no apunta a current"
ok "desktop apunta a current"
if grep -rqF "$REL" "$DESKTOP" "$INST/installed.json"; then falla "ruta del release en la instalación"; fi
if grep -rqF "$REPO_DIR" "$DESKTOP" "$INST/installed.json"; then falla "ruta del repo en la instalación"; fi
for enlace in "$H"/.local/bin/*; do
    case "$(readlink "$enlace")" in "$REL"*|"$REPO_DIR"*) falla "enlace al release: $enlace" ;; esac
done
ok "sin rutas al release ni al repo"
existe "$DATA/icons/hicolor/scalable/apps/parlar.svg"
python3 - "$INST/installed.json" "$VERSION" "$CURRENT1" <<'PY' || falla "installed.json"
import json, sys
datos = json.load(open(sys.argv[1]))
assert set(datos) == {"app", "version", "current", "previous", "installed_at"}, datos
assert datos["app"] == "ParlAR" and datos["version"] == sys.argv[2], datos
assert datos["current"] == sys.argv[3] and datos["previous"] is None, datos
PY
ok "installed.json"
en_home "$INST/current/venv/bin/python" -m pip check >/dev/null || falla "pip check"
ok "pip check"
SITE="$(en_home "$INST/current/venv/bin/python" -c 'import parlar, os; print(os.path.dirname(parlar.__file__))')"
en_home "$INST/current/venv/bin/python" -m compileall -q "$SITE" >/dev/null || falla "compileall"
ok "compileall del paquete instalado"
en_home "$INST/current/venv/bin/python" -c 'import evdev, webrtcvad' || falla "wheels nativos"
ok "evdev y webrtcvad importan sin compilador"
if grep -q "compilador invocado" "$TMP/limpio/install1.log"; then falla "se invocó un compilador"; fi
ok "la instalación no invocó compiladores"

echo "==> Reinstalación (idempotente) con servicio"
mkdir -p "$CONF/parlar" "$INST/sesiones"
printf '{"schema_version": 1, "hotkey_toggle": "<ctrl>+<alt>+d"}\n' > "$CONF/parlar/config.json"
printf 'transcript ñ\n' > "$INST/sesiones/parlar-prueba.txt"
DATOS_ANTES="$(sha256sum "$CONF/parlar/config.json" "$INST/sesiones/parlar-prueba.txt")"
DESKTOP_ANTES="$(sha256sum < "$DESKTOP")"
en_home "$REL/install.sh" --cpu-only --install-service >"$TMP/limpio/install2.log" 2>&1 \
    || { tail -30 "$TMP/limpio/install2.log"; falla "reinstalación"; }
CURRENT2="$(readlink "$INST/current")"
[[ "$CURRENT2" != "$CURRENT1" ]] || falla "la reinstalación no creó versión nueva"
igual "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["previous"])' "$INST/installed.json")" \
    "$CURRENT1" "previous = instalación anterior"
igual "$(sha256sum < "$DESKTOP")" "$DESKTOP_ANTES" "desktop idéntico"
# Un proceso que sigue corriendo desde la versión previa relanza la vigente.
igual "$(en_home "$INST/$CURRENT1/venv/bin/python" -I -c \
    'from parlar.restart import comando_reinicio; print(comando_reinicio()[0])')" \
    "$INST/current/venv/bin/python" "reinicio desde la versión previa usa current"
igual "$(en_home "$INST/current/venv/bin/python" -I -c \
    'import os, sys; print(os.path.realpath(sys.prefix))')" \
    "$(realpath "$INST/$CURRENT2/venv")" "el Python de current carga la versión nueva"
igual "$(sha256sum "$CONF/parlar/config.json" "$INST/sesiones/parlar-prueba.txt")" "$DATOS_ANTES" \
    "config y sesiones intactas"
grep -qF "ExecStart=:\"$INST/current/venv/bin/parlar\"" "$CONF/systemd/user/parlar.service" \
    || falla "unit no apunta a current"
ok "unit apunta a current"
existe "$CONF/autostart/parlar-systemd.desktop"

echo "==> Fallo antes del swap"
cp -a "$REL" "$TMP/roto"
: > "$TMP/roto/wheels/parlar-$VERSION-py3-none-any.whl"
HUELLA_ANTES="$(huella "$H")"
if en_home "$TMP/roto/install.sh" --cpu-only >"$TMP/limpio/install-roto.log" 2>&1; then
    falla "el release roto se instaló"
fi
igual "$(huella "$H")" "$HUELLA_ANTES" "fallo preserva current, launcher y datos"

echo "==> Tercera instalación poda versiones viejas"
en_home "$REL/install.sh" --cpu-only >"$TMP/limpio/install3.log" 2>&1 || falla "tercera instalación"
igual "$(find "$INST/versions" -mindepth 1 -maxdepth 1 | wc -l)" "2" "se conservan current + previous"
ausente "$INST/$CURRENT1"

echo "==> Desinstalación por defecto"
rm -rf -- "$TMP/extraido"  # parlar-uninstall no necesita el release
mkdir -p "$H/.cache/huggingface/hub/models--Systran--faster-whisper-small"
en_home "$H/.local/bin/parlar-uninstall" >"$TMP/limpio/uninstall.log" 2>&1 || falla "parlar-uninstall"
for ruta in "$INST/current" "$INST/versions" "$INST/installed.json" "$INST/uninstall.sh" \
        "$DESKTOP" "$DATA/icons/hicolor/scalable/apps/parlar.svg" \
        "$CONF/systemd/user/parlar.service" "$CONF/autostart/parlar-systemd.desktop" \
        "$H/.local/bin/parlar" "$H/.local/bin/parlarctl" "$H/.local/bin/parlar-uninstall"; do
    ausente "$ruta"
done
ok "binarios, launcher, servicio y versiones eliminados"
igual "$(sha256sum "$CONF/parlar/config.json" "$INST/sesiones/parlar-prueba.txt")" "$DATOS_ANTES" \
    "uninstall preserva config y sesiones"

echo "==> --purge-data"
tar -xzf "$TARBALL" -C "$TMP" && REL="$TMP/$(basename "$REL")"
en_home "$REL/install.sh" --cpu-only >"$TMP/limpio/install4.log" 2>&1 || falla "reinstalación"
en_home "$H/.local/bin/parlar-uninstall" --purge-data >"$TMP/limpio/purge.log" 2>&1 || falla "purge"
ausente "$CONF/parlar"
ausente "$INST"
existe "$H/.cache/huggingface/hub/models--Systran--faster-whisper-small"
grep -qF "rm -rf ~/.cache/huggingface/hub/models--Systran--faster-whisper-*" "$TMP/limpio/purge.log" \
    || falla "falta la instrucción manual para la caché de Hugging Face"
ok "--purge-data borra config y sesiones; conserva ~/.cache/huggingface"

# ---------------------------------------------------------------------------
if ((SKIP_LEGACY == 0)); then
    echo "==> Upgrade desde $LEGACY_REF"
    preparar_home "$TMP/legacy"
    INST="$DATA/parlar"
    DESKTOP="$DATA/applications/parlar.desktop"
    mkdir -p "$TMP/legacy/src"
    git -C "$REPO_DIR" archive "$LEGACY_REF" | tar -x -C "$TMP/legacy/src"
    # 1.0.x compilaba evdev en la máquina: sólo esta fase usa compilador.
    BLOQUEO_CC=""
    en_home "$TMP/legacy/src/install.sh" --cpu-only --install-service \
        >"$TMP/legacy/install-legacy.log" 2>&1 \
        || { tail -30 "$TMP/legacy/install-legacy.log"; falla "install $LEGACY_REF"; }
    BLOQUEO_CC="$NOCC:"
    existe "$INST/venv/bin/parlar"
    ausente "$INST/current"
    igual "$(readlink "$H/.local/bin/parlar")" "$INST/venv/bin/parlar" "layout legacy creado"
    mkdir -p "$CONF/parlar" "$INST/sesiones"
    printf '{"schema_version": 1, "hotkey_toggle": "<ctrl>+<alt>+h", "mode": "streaming"}\n' \
        > "$CONF/parlar/config.json"
    printf 'sesión legacy\n' > "$INST/sesiones/parlar-legacy.txt"
    DATOS_ANTES="$(sha256sum "$CONF/parlar/config.json" "$INST/sesiones/parlar-legacy.txt")"
    VENV_LEGACY_ANTES="$(huella "$INST/venv")"

    en_home "$REL/install.sh" --cpu-only >"$TMP/legacy/upgrade.log" 2>&1 \
        || { tail -30 "$TMP/legacy/upgrade.log"; falla "upgrade"; }
    grep -q "Instalación 1.0.x detectada" "$TMP/legacy/upgrade.log" || falla "legacy no detectado"
    grep -q "cerralo y volvé a abrirlo" "$TMP/legacy/upgrade.log" || falla "falta aviso de reinicio"
    ok "legacy detectado y aviso de reinicio"
    igual "$(sha256sum "$CONF/parlar/config.json" "$INST/sesiones/parlar-legacy.txt")" "$DATOS_ANTES" \
        "config, hotkey y sesiones idénticas"
    igual "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hotkey_toggle"])' \
        "$CONF/parlar/config.json")" "<ctrl>+<alt>+h" "hotkey existente sin migrar"
    igual "$(huella "$INST/venv")" "$VENV_LEGACY_ANTES" "venv legacy preservado como previous"
    en_home "$INST/venv/bin/parlar" --help >/dev/null || falla "el venv legacy dejó de funcionar"
    ok "una instancia legacy abierta conserva su entorno"
    igual "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["previous"])' "$INST/installed.json")" \
        "venv" "previous = venv legacy"
    igual "$(readlink "$H/.local/bin/parlar")" "$INST/current/venv/bin/parlar" "enlace migrado a current"
    grep -qxF "Exec=\"$INST/current/venv/bin/parlar\" --abrir-configuracion" "$DESKTOP" \
        || falla "desktop no migrado"
    ok "desktop nuevo apunta a current"
    grep -qF "ExecStart=:\"$INST/current/venv/bin/parlar\"" "$CONF/systemd/user/parlar.service" \
        || falla "unit legacy no migrada"
    ok "unit legacy re-renderizada a current"
    igual "$(en_home "$H/.local/bin/parlar" --version)" "ParlAR $VERSION" "versión nueva vigente"

    en_home "$REL/install.sh" --cpu-only >"$TMP/legacy/upgrade2.log" 2>&1 || falla "segunda actualización"
    ausente "$INST/venv"
    ok "la actualización siguiente poda el venv legacy"
    en_home "$H/.local/bin/parlar-uninstall" >/dev/null 2>&1 || falla "uninstall tras upgrade"
    igual "$(sha256sum "$CONF/parlar/config.json" "$INST/sesiones/parlar-legacy.txt")" "$DATOS_ANTES" \
        "datos legacy preservados al desinstalar"
fi

echo "==> Prueba de instalación aislada: OK ($PASOS comprobaciones)"
