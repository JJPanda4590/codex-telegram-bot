#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-}"
CONFIG_FILE="$ROOT_DIR/settings/bot_config.json"
EXAMPLE_CONFIG="$ROOT_DIR/settings/bot_config.example.json"

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
require_command "$PYTHON_BIN" "Install Python 3.10+ first, or run with PYTHON_BIN=/path/to/python3.11 ./setup_env.sh"

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
  echo "Created virtual environment: $VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -q --upgrade pip
"$VENV_DIR/bin/python" -m pip install -q -r "$ROOT_DIR/requirements.txt"

if [[ ! -f "$CONFIG_FILE" && -f "$EXAMPLE_CONFIG" ]]; then
  cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
  echo "Created config from template: $CONFIG_FILE"
fi

if [[ -x "/Applications/Codex.app/Contents/Resources/codex" ]]; then
  echo "Found Codex CLI at /Applications/Codex.app/Contents/Resources/codex"
elif command -v codex >/dev/null 2>&1; then
  echo "Found Codex CLI at $(command -v codex)"
else
  echo "Codex CLI not found."
  echo "Install Codex.app, or make sure the 'codex' command is available in PATH."
fi

echo "Environment setup complete."
echo "Run the bot with: ./run_bot.sh"
