#!/usr/bin/env bash
# Instalación de usuario autocontenida de ParlAR. No activa el servicio.
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
INSTALL_HOME="${PARLAR_INSTALL_HOME:-$DATA_BASE/parlar}"
BIN_DIR="${PARLAR_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_HOME/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
DESKTOP_PATH="$DATA_BASE/applications/parlar.desktop"
UNIT_PATH="$CONFIG_BASE/systemd/user/parlar.service"
AUTOSTART_PATH="$CONFIG_BASE/autostart/parlar-systemd.desktop"
LEGACY_ENABLE_PATH="$CONFIG_BASE/systemd/user/default.target.wants/parlar.service"
INSTALL_SERVICE=0
PRELOAD_MODEL=0
CPU_ONLY=0

uso() {
    cat <<'EOF'
Uso: ./install.sh [opciones]

  --install-service  instala unit systemd static y autostart gráfico XDG
  --preload-model    descarga/precarga Whisper small
  --cpu-only         no instala las runtimes CUDA aunque haya NVIDIA
  -h, --help         muestra esta ayuda sin modificar el sistema
EOF
}

while (($#)); do
    case "$1" in
        --install-service) INSTALL_SERVICE=1 ;;
        --preload-model) PRELOAD_MODEL=1 ;;
        --cpu-only) CPU_ONLY=1 ;;
        -h|--help) uso; exit 0 ;;
        *) echo "!! opción desconocida: $1" >&2; uso >&2; exit 2 ;;
    esac
    shift
done

if [[ "$INSTALL_HOME" != /* \
        || "$(basename -- "$INSTALL_HOME")" != "parlar" \
        || "$(dirname -- "$INSTALL_HOME")" == "/" ]]; then
    echo "!! ruta de instalación insegura: $INSTALL_HOME" >&2
    exit 1
fi
case "$INSTALL_HOME" in
    /|"$HOME"|"$HOME"/)
        echo "!! ruta de instalación insegura: $INSTALL_HOME" >&2
        exit 1
        ;;
esac

BOOTSTRAP_PYTHON="${PARLAR_BOOTSTRAP_PYTHON:-python3}"
"$BOOTSTRAP_PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(
        f"ParlAR requiere Python 3.12 o posterior; encontrado "
        f"{sys.version.split()[0]}"
    )
PY

if [[ -x "$VENV_PYTHON" ]]; then
    echo "==> Reutilizando instalación en $INSTALL_HOME"
elif [[ -e "$VENV_DIR" ]]; then
    echo "!! $VENV_DIR existe pero no contiene un Python ejecutable" >&2
    exit 1
else
    echo "==> Creando entorno aislado en $INSTALL_HOME"
    mkdir -p -- "$INSTALL_HOME"
    "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
fi

echo "==> Instalando ParlAR"
"$VENV_PYTHON" -m pip install "$REPO_DIR"

if ((CPU_ONLY)); then
    echo "==> Instalación CPU solicitada; se omiten runtimes NVIDIA"
elif "$VENV_PYTHON" - <<'PYCUDA' >/dev/null 2>&1
import ctranslate2

raise SystemExit(
    0 if ctranslate2.get_cuda_device_count() > 0 else 1
)
PYCUDA
then
    echo "==> GPU CUDA detectada por CTranslate2; instalando runtime NVIDIA de ParlAR"
    "$VENV_PYTHON" -m pip install "$REPO_DIR[cuda]"
else
    echo "==> CTranslate2 no detectó GPU CUDA; ParlAR usará CPU"
fi

echo "==> Intentando instalar WebRTC VAD opcional"
if ! "$VENV_PYTHON" -m pip install --no-deps 'webrtcvad-wheels==2.0.14'; then
    echo "!! WebRTC VAD no disponible; se usará el VAD de energía probado" >&2
fi

if ((PRELOAD_MODEL)); then
    "$VENV_PYTHON" - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("small", device="cpu", compute_type="int8")
print("==> Modelo small disponible en caché")
PY
fi

mkdir -p -- "$BIN_DIR"
instalar_enlace() {
    local origen="$1"
    local destino="$2"
    if [[ -L "$destino" && "$(readlink -- "$destino")" == "$origen" ]]; then
        return
    fi
    if [[ -e "$destino" || -L "$destino" ]]; then
        echo "!! $destino existe y no pertenece a esta instalación" >&2
        exit 1
    fi
    ln -s -- "$origen" "$destino"
}
instalar_enlace "$VENV_DIR/bin/parlar" "$BIN_DIR/parlar"
instalar_enlace "$VENV_DIR/bin/parlarctl" "$BIN_DIR/parlarctl"

"$VENV_PYTHON" "$REPO_DIR/scripts/render_desktop.py" \
    --executable "$VENV_DIR/bin/parlar" --output "$DESKTOP_PATH"

if ((INSTALL_SERVICE)); then
    "$VENV_PYTHON" "$REPO_DIR/scripts/render_service.py" \
        --workdir "$INSTALL_HOME" \
        --executable "$VENV_DIR/bin/parlar" \
        --output "$UNIT_PATH"
    "$VENV_PYTHON" "$REPO_DIR/scripts/render_desktop.py" \
        --autostart-service --output "$AUTOSTART_PATH"
    if command -v systemctl >/dev/null 2>&1; then
        # Upgrade desde la unit antigua habilitada en default.target. La unit
        # nueva es static y el login gráfico es dueño del autostart.
        systemctl --user disable parlar.service >/dev/null 2>&1 || true
    fi
    if [[ -L "$LEGACY_ENABLE_PATH" ]]; then
        rm -f -- "$LEGACY_ENABLE_PATH"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload || echo \
            "!! daemon-reload queda pendiente en la sesión gráfica" >&2
    fi
fi

echo "==> ParlAR instalado. Comandos: $BIN_DIR/parlar y $BIN_DIR/parlarctl"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "!! $BIN_DIR no está en PATH; agregalo para invocar los comandos por nombre" >&2
fi
if ((INSTALL_SERVICE)); then
    echo "    Autostart gráfico instalado: $AUTOSTART_PATH"
    echo "    Inicio opcional ahora: systemctl --user start parlar.service"
fi
