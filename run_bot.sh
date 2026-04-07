#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-}"

select_python() {
  local candidates=()
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("$PYTHON_BIN")
  fi
  candidates+=(python3.11 python3.10 python3)

  local candidate
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done

  return 1
}

require_command() {
  local command_name="$1"
  local help_text="${2:-}"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name"
    if [[ -n "$help_text" ]]; then
      echo "$help_text"
    fi
    exit 1
  fi
}

PYTHON_BIN="$(select_python)"
require_command "$PYTHON_BIN" "Install Python 3.10+ first, or run with PYTHON_BIN=/path/to/python3.11 ./run_bot.sh"

python_version_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
}

if ! python_version_ok "$PYTHON_BIN"; then
  echo "Python 3.10+ is required. Current interpreter: $("$PYTHON_BIN" --version 2>&1)"
  exit 1
fi

if [[ -x "$VENV_DIR/bin/python" ]] && ! python_version_ok "$VENV_DIR/bin/python"; then
  echo "Existing virtual environment uses an older Python. Recreating $VENV_DIR"
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Kill old bot instances from this project before starting a new one.
# This only targets processes whose command line includes this project's main.py.
stop_duplicate_instances() {
  local old_pids still_running

  old_pids="$(
    ps -ax -o pid=,command= | awk -v main="$ROOT_DIR/main.py" '
      index($0, main) { print $1 }
    '
  )"

  if [[ -n "${old_pids:-}" ]]; then
    echo "Stopping duplicate bot instances: ${old_pids}"
    kill ${old_pids} 2>/dev/null || true
    sleep 2

    still_running="$(
      ps -ax -o pid=,command= | awk -v main="$ROOT_DIR/main.py" '
        index($0, main) { print $1 }
      '
    )"

    if [[ -n "${still_running:-}" ]]; then
      echo "Force stopping stuck bot instances: ${still_running}"
      kill -9 ${still_running} 2>/dev/null || true
    fi
  fi
}

stop_duplicate_instances

if [[ ! -x "/Applications/Codex.app/Contents/Resources/codex" ]] && ! command -v codex >/dev/null 2>&1; then
  echo "Codex CLI not found."
  echo "Install Codex.app, or make sure the 'codex' command is available in PATH."
  exit 1
fi

"$VENV_DIR/bin/python" -m pip install -q --upgrade pip
"$VENV_DIR/bin/python" -m pip install -q -r "$ROOT_DIR/requirements.txt"
exec "$VENV_DIR/bin/python" "$ROOT_DIR/main.py"
