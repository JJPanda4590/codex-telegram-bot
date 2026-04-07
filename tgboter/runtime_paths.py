from __future__ import annotations

import os
import sys
from pathlib import Path


APP_RUNTIME_DIRNAME = "my_tg_bot"


def _default_runtime_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_RUNTIME_DIRNAME / "runtime"

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / APP_RUNTIME_DIRNAME / "runtime"
        return Path.home() / "AppData" / "Local" / APP_RUNTIME_DIRNAME / "runtime"

    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / APP_RUNTIME_DIRNAME
    return Path.home() / ".local" / "state" / APP_RUNTIME_DIRNAME


def runtime_dir() -> Path:
    """Return the OS-level runtime directory for bot state outside the repo."""
    configured = os.environ.get("TGBOT_RUNTIME_DIR", "").strip()
    base_dir = Path(configured).expanduser() if configured else _default_runtime_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def lock_path() -> Path:
    return runtime_dir() / "bot.lock"


def log_path() -> Path:
    return runtime_dir() / "bot.log"
