#!/usr/bin/env bash
# Instalación de ParlAR: dependencias del sistema + venv de Python.
# Objetivo principal Ubuntu/Debian; Fedora también soportado.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Instalación de ParlAR"

# ------------------------------------------------------ dependencias de sistema
instalar_debian() {
    sudo apt-get update
    sudo apt-get install -y \
        python3 python3-venv python3-dev python3-tk \
        portaudio19-dev libnotify-bin \
        xdotool xclip
    # Herramientas de tipeo para Wayland (en Ubuntu reciente; ignorar si faltan)
    sudo apt-get install -y wtype wl-clipboard ydotool 2>/dev/null || \
        echo "    (wtype/ydotool no están en tus repos; no importa si usás X11)"
}

instalar_fedora() {
    sudo dnf install -y \
        python3 python3-devel python3-tkinter \
        portaudio-devel libnotify \
        xdotool xclip wtype wl-clipboard ydotool 2>/dev/null || true
}

if command -v apt-get >/dev/null 2>&1; then
    instalar_debian
elif command -v dnf >/dev/null 2>&1; then
    instalar_fedora
else
    echo "!! Distro no soportada. Instalá a mano: cabeceras de portaudio, tk,"
    echo "   xdotool (X11) o wtype/ydotool (Wayland), xclip/wl-clipboard, libnotify."
fi

# ------------------------------------------------------ entorno de Python
echo "==> Creando entorno virtual"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel

echo "==> Instalando dependencias de Python"
pip install -r requirements.txt

# webrtcvad no publica wheel para algunas versiones de Python; probar y avisar
pip install webrtcvad 2>/dev/null || \
    echo "    (falló la compilación de webrtcvad; se usará el VAD de energía)"

# ------------------------------------------------------ chequeo de CUDA
python3 - << 'EOF'
try:
    import ctranslate2
    n = ctranslate2.get_cuda_device_count()
    print(f"==> Dispositivos CUDA detectados: {n} " + ("(se usará GPU)" if n else "(modo CPU int8)"))
except Exception as e:
    print(f"==> Chequeo de CUDA omitido: {e}")
EOF

# ------------------------------------------------------ precarga del modelo
echo "==> Pre-descargando el modelo Whisper 'small' (única vez, ~460MB)"
python3 - << 'EOF'
from faster_whisper import WhisperModel
WhisperModel("small", device="cpu", compute_type="int8")
print("==> Modelo en caché.")
EOF

# ------------------------------------------------------ nota sobre ydotool
if [ -n "${WAYLAND_DISPLAY:-}" ] && ! command -v wtype >/dev/null 2>&1; then
    if command -v ydotool >/dev/null 2>&1; then
        echo "==> Wayland detectado sin wtype. Habilitá el daemon de ydotool:"
        echo "    sudo systemctl enable --now ydotool"
        echo "    (o corré 'ydotoold' como tu usuario; puede requerir el grupo 'input')"
    fi
fi

cat << 'EOF'

==> Instalación completa.

Ejecutar:
    source .venv/bin/activate
    python -m parlar                        # modo frase (por defecto, español)
    python -m parlar --modo streaming       # palabras mientras hablás

Control:
    X11:      Ctrl+Alt+D alterna la grabación (atajo global)
    Wayland:  asigná `./parlarctl alternar` a un atajo en tu DE
    Siempre:  click en el punto del indicador, o `./parlarctl alternar`

EOF
