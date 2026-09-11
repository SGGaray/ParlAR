#!/usr/bin/env bash
# Puerta de calidad única para desarrollo local y CI. No requiere hardware.
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_DIR"

if [[ -n "${PARLAR_PYTHON:-}" ]]; then
    PYTHON="$PARLAR_PYTHON"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
elif [[ -x .venv/bin/python ]]; then
    PYTHON="$REPO_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "!! Python no ejecutable: $PYTHON" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/parlar-check.XXXXXX")"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$TEMP_DIR/pycache"

ESTADO_ANTES=""
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    ESTADO_ANTES="$(git status --porcelain=v1 --untracked-files=all)"
fi

echo "==> Shell y bytecode"
bash -n setup.sh scripts/check.sh
"$PYTHON" -m py_compile parlar/*.py scripts/render_service.py tests/*.py

echo "==> Smokes CLI"
"$PYTHON" -m parlar --help >"$TEMP_DIR/parlar-help.txt"
set +e
./parlarctl >"$TEMP_DIR/parlarctl-out.txt" 2>"$TEMP_DIR/parlarctl-err.txt"
CTL_STATUS=$?
set -e
if [[ $CTL_STATUS -ne 2 ]]; then
    echo "!! ./parlarctl sin argumentos devolvió $CTL_STATUS; se esperaba 2" >&2
    exit 1
fi

echo "==> 58 checks legacy"
"$PYTHON" tests/run_tests.py

echo "==> Suites unittest"
"$PYTHON" -m unittest discover -s tests -p 'test_*.py'

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "==> Higiene Git"
    git diff --check
    ESTADO_DESPUES="$(git status --porcelain=v1 --untracked-files=all)"
    if [[ "$ESTADO_ANTES" != "$ESTADO_DESPUES" ]]; then
        echo "!! el runner modificó el worktree" >&2
        diff -u <(printf '%s\n' "$ESTADO_ANTES") \
            <(printf '%s\n' "$ESTADO_DESPUES") || true
        exit 1
    fi
fi

echo "==> Puerta completa: OK"
