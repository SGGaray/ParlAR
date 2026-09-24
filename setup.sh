#!/usr/bin/env bash
# Instalación reproducible de ParlAR desde un checkout local.
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$REPO_DIR"

INSTALL_SERVICE=0
PRELOAD_MODEL=0
SKIP_SYSTEM=0

uso() {
    cat <<'EOF'
Uso: ./setup.sh [opciones]

  --install-service       instala unit static y autostart gráfico XDG
  --preload-model         descarga/precarga Whisper small (requiere red, ~460 MB)
  --skip-system-packages  omite apt/dnf; las dependencias del sistema ya deben existir
  -h, --help              muestra esta ayuda sin modificar el sistema
EOF
}

while (($#)); do
    case "$1" in
        --install-service) INSTALL_SERVICE=1 ;;
        --preload-model) PRELOAD_MODEL=1 ;;
        --skip-system-packages) SKIP_SYSTEM=1 ;;
        -h|--help) uso; exit 0 ;;
        *) echo "!! opción desconocida: $1" >&2; uso >&2; exit 2 ;;
    esac
    shift
done

instalar_opcionales() {
    local gestor="$1"
    shift
    local paquete
    for paquete in "$@"; do
        if ! sudo "$gestor" install -y "$paquete"; then
            echo "!! opcional no instalado: $paquete" >&2
        fi
    done
}

echo "==> Instalación de ParlAR en $REPO_DIR"

if ((SKIP_SYSTEM == 0)); then
    if command -v apt-get >/dev/null 2>&1; then
        echo "==> Dependencias requeridas (Debian/Ubuntu)"
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv portaudio19-dev
        echo "==> Integraciones opcionales de escritorio y compilación"
        instalar_opcionales apt-get \
            python3-dev python3-tk libnotify-bin xdotool xclip \
            wtype wl-clipboard ydotool
    elif command -v dnf >/dev/null 2>&1; then
        echo "==> Dependencias requeridas (Fedora)"
        sudo dnf install -y python3 portaudio-devel
        echo "==> Integraciones opcionales de escritorio y compilación"
        instalar_opcionales dnf \
            python3-devel python3-tkinter libnotify xdotool xclip \
            wtype wl-clipboard ydotool
    else
        echo "!! distribución no soportada automáticamente." >&2
        echo "   Usá Debian/Ubuntu o Fedora, o instalá las dependencias y repetí" >&2
        echo "   con --skip-system-packages." >&2
        exit 1
    fi
else
    echo "==> Dependencias del sistema omitidas por solicitud"
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(
        f"ParlAR requiere Python 3.12 o posterior; encontrado {sys.version.split()[0]}"
    )
print(f"==> Python {sys.version.split()[0]}")
PY

if [[ -x .venv/bin/python ]]; then
    echo "==> Reutilizando entorno virtual existente"
elif [[ -e .venv ]]; then
    echo "!! .venv existe pero no contiene un Python ejecutable; no se modifica." >&2
    exit 1
else
    echo "==> Creando entorno virtual"
    python3 -m venv .venv
fi

VENV_PYTHON="$REPO_DIR/.venv/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip wheel

echo "==> Instalando dependencias Python requeridas"
"$VENV_PYTHON" -m pip install -r requirements.txt

echo "==> Instalando ParlAR y sus comandos"
"$VENV_PYTHON" -m pip install --no-deps "$REPO_DIR"

echo "==> Intentando instalar VAD opcional"
if "$VENV_PYTHON" -m pip install -r requirements-optional.txt; then
    echo "==> webrtcvad disponible"
else
    echo "!! webrtcvad no se pudo instalar; se usará el VAD de energía probado" >&2
fi

"$VENV_PYTHON" - <<'PY'
try:
    import ctranslate2
    cantidad = ctranslate2.get_cuda_device_count()
    modo = (
        "GPU CUDA visible; ParlAR intentará preparar el runtime al iniciar"
        if cantidad else
        "modo CPU int8"
    )
    print(f"==> Dispositivos CUDA detectados: {cantidad} ({modo})")
except Exception as exc:
    print(f"==> Chequeo de CUDA omitido: {type(exc).__name__}")
PY

if ((PRELOAD_MODEL)); then
    echo "==> Descargando/precargando Whisper small (~460 MB)"
    "$VENV_PYTHON" - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("small", device="cpu", compute_type="int8")
print("==> Modelo disponible en caché")
PY
else
    echo "==> Modelo no descargado; se obtendrá en el primer inicio"
    echo "    Para precargarlo ahora: ./setup.sh --skip-system-packages --preload-model"
fi

if ((INSTALL_SERVICE)); then
    CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
    UNIT_PATH="$CONFIG_BASE/systemd/user/parlar.service"
    AUTOSTART_PATH="$CONFIG_BASE/autostart/parlar-systemd.desktop"
    LEGACY_ENABLE_PATH="$CONFIG_BASE/systemd/user/default.target.wants/parlar.service"
    "$VENV_PYTHON" scripts/render_service.py --repo "$REPO_DIR" --output "$UNIT_PATH"
    "$VENV_PYTHON" scripts/render_desktop.py \
        --autostart-service --output "$AUTOSTART_PATH"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable parlar.service >/dev/null 2>&1 || true
    fi
    if [[ -L "$LEGACY_ENABLE_PATH" ]]; then
        rm -f -- "$LEGACY_ENABLE_PATH"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl --user daemon-reload; then
            echo "!! unit instalada; daemon-reload queda pendiente en la sesión gráfica" >&2
        fi
    fi
    echo "    Autostart gráfico instalado: $AUTOSTART_PATH"
    echo "    Inicio opcional ahora: systemctl --user start parlar.service"
fi

if ! command -v xdotool >/dev/null 2>&1 \
        && ! command -v wtype >/dev/null 2>&1 \
        && ! command -v ydotool >/dev/null 2>&1 \
        && ! command -v xclip >/dev/null 2>&1 \
        && ! command -v wl-copy >/dev/null 2>&1; then
    echo "!! no se detectó backend de inyección/clipboard; revisá el README" >&2
fi

cat <<'EOF'

==> Instalación estructural completa.

Ejecutar:
    source .venv/bin/activate
    parlar

Validar sin hardware:
    ./scripts/check.sh

Control:
    X11: mantené Ctrl derecho + Shift derecho; Esc cancela
    Wayland: asigná `parlarctl alternar` a un atajo del escritorio
EOF
