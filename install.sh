#!/usr/bin/env bash
# Instalación de usuario de ParlAR. No activa el servicio.
#
# Funciona desde un release extraído (VERSION + wheels/ + share/: instala sólo
# wheels, sin compilar) o desde un checkout de mantenimiento. No depende de
# .git. La lógica de staging y swap atómico vive en parlar_installer.py.
set -euo pipefail

ORIGEN="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

uso() {
    cat <<'EOF'
Uso: ./install.sh [opciones]

  --install-service  instala unit systemd static y autostart gráfico XDG
  --preload-model    descarga/precarga Whisper small
  --cpu-only         no instala las runtimes CUDA aunque haya NVIDIA
  -h, --help         muestra esta ayuda sin modificar el sistema

Instala en ~/.local/share/parlar y crea parlar, parlarctl y parlar-uninstall
en ~/.local/bin. Actualizar: extraé la release nueva y ejecutá su install.sh.
EOF
}

for argumento in "$@"; do
    case "$argumento" in
        --install-service|--preload-model|--cpu-only) ;;
        -h|--help) uso; exit 0 ;;
        *) echo "!! opción desconocida: $argumento" >&2; uso >&2; exit 2 ;;
    esac
done

if [[ -f "$ORIGEN/VERSION" && -d "$ORIGEN/wheels" && -d "$ORIGEN/share" ]]; then
    MODO=release
    INSTALADOR="$ORIGEN/share/parlar_installer.py"
    mapfile -t VERSIONES < "$ORIGEN/share/python-versions.txt"
elif [[ -f "$ORIGEN/pyproject.toml" && -f "$ORIGEN/scripts/parlar_installer.py" ]]; then
    MODO=checkout
    INSTALADOR="$ORIGEN/scripts/parlar_installer.py"
    VERSIONES=()
else
    echo "!! $ORIGEN no es un release de ParlAR ni un checkout del proyecto" >&2
    exit 1
fi

version_de() {
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

version_soportada() {
    local version
    version="$(version_de "$1")" || return 1
    if [[ "$MODO" == release ]]; then
        local soportada
        for soportada in "${VERSIONES[@]}"; do
            [[ "$version" == "$soportada" ]] && return 0
        done
        return 1
    fi
    "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null
}

# La Configuración usa Tk: un Python sin tkinter instalaría un ParlAR cuya
# ventana principal no abre. Se exige junto con venv para todo candidato.
con_tk_y_venv() {
    "$1" -c 'import tkinter' >/dev/null 2>&1 \
        && "$1" -c 'import ensurepip, venv' >/dev/null 2>&1
}

# Orden: Python pedido explícitamente, python3 del sistema y luego versiones
# soportadas de la más nueva a la más vieja.
CANDIDATOS=()
if [[ -n "${PARLAR_BOOTSTRAP_PYTHON:-}" ]]; then
    CANDIDATOS+=("$PARLAR_BOOTSTRAP_PYTHON")
else
    CANDIDATOS+=(python3)
    for ((i = ${#VERSIONES[@]} - 1; i >= 0; i--)); do
        CANDIDATOS+=("python${VERSIONES[i]}")
    done
fi

PYTHON=""
SIN_TK=()
for candidato in "${CANDIDATOS[@]}"; do
    resuelto="$(command -v -- "$candidato" 2>/dev/null)" || continue
    version_soportada "$resuelto" || continue
    if con_tk_y_venv "$resuelto"; then
        PYTHON="$resuelto"
        break
    fi
    SIN_TK+=("$resuelto (Python $(version_de "$resuelto"))")
done

if [[ -z "$PYTHON" && ${#SIN_TK[@]} -gt 0 ]]; then
    if [[ "$MODO" == release ]]; then
        echo "!! ParlAR necesita Python ${VERSIONES[0]}–${VERSIONES[-1]} con Tk (tkinter) y venv." >&2
    else
        echo "!! ParlAR necesita Python 3.12 o posterior con Tk (tkinter) y venv." >&2
    fi
    echo "   La ventana de Configuración usa Tk. Encontrados sin tkinter o sin venv:" >&2
    printf '     %s\n' "${SIN_TK[@]}" >&2
    echo "   Debian/Ubuntu: sudo apt install python3-tk python3-venv" >&2
    echo "   Fedora:        sudo dnf install python3-tkinter" >&2
    echo "   No se modificó nada." >&2
    exit 1
fi

if [[ -z "$PYTHON" ]]; then
    encontrado="$(version_de "${CANDIDATOS[0]}" || true)"
    if [[ "$MODO" == release ]]; then
        echo "!! Esta versión de ParlAR soporta Python ${VERSIONES[0]}–${VERSIONES[-1]}." >&2
        echo "   Instalá una versión compatible o descargá una release más nueva." >&2
    else
        echo "!! ParlAR requiere Python 3.12 o posterior." >&2
    fi
    if [[ -n "$encontrado" ]]; then
        echo "   Encontrado: Python $encontrado (${CANDIDATOS[0]})" >&2
    fi
    exit 1
fi

exec "$PYTHON" "$INSTALADOR" --origen "$ORIGEN" "$@"
