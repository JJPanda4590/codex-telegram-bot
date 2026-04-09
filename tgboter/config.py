from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tgboter.i18n import SUPPORTED_LANGUAGES


SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")


@dataclass(slots=True)
class Config:
    """Runtime configuration loaded from config.json."""

    telegram_bot_token: str
    whitelist: list[int]
    codex_cli_path: str = "codex"
    codex_cli_fallback_paths: list[str] = field(default_factory=list)
    codex_cli_use_shell: bool = False
    codex_shell_path: str = ""
    codex_model: str = "gpt-4.1-mini"
    codex_reasoning_effort: str = "medium"
    openai_admin_api_key: str = ""
    openai_organization_id: str = ""
    openai_project_id: str = ""
    session_store_path: str = "sessions.json"
    request_timeout_seconds: float = 28800.0
    stream_update_min_interval_seconds: float = 0.35
    stream_update_min_chars: int = 24
    project_path: str = "."
    file_browser_enabled: bool = False
    log_level: str = "INFO"
    default_language: str = "zh"
    translations_path: str = "settings/i18n.json"
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Load and validate configuration from a JSON file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}. Please create config.json first."
            )

        with config_path.open("r", encoding="utf-8") as file:
            payload: dict[str, Any] = json.load(file)

        whitelist = payload.get("whitelist", [])
        if not isinstance(whitelist, list) or not all(isinstance(item, int) for item in whitelist):
            raise ValueError("'whitelist' must be a list of Telegram user_id integers")

        config = cls(
            telegram_bot_token=payload.get("telegram_bot_token", ""),
            whitelist=whitelist,
            codex_cli_path=str(payload.get("codex_cli_path") or "codex"),
            codex_cli_fallback_paths=[
                str(item) for item in payload.get("codex_cli_fallback_paths", [])
            ],
            codex_model=payload.get("codex_model", "gpt-4.1-mini"),
            codex_reasoning_effort=str(payload.get("codex_reasoning_effort", "medium")).lower(),
            openai_admin_api_key=str(payload.get("openai_admin_api_key", "")),
            openai_organization_id=str(payload.get("openai_organization_id", "")),
            openai_project_id=str(payload.get("openai_project_id", "")),
            session_store_path=payload.get("session_store_path", "sessions.json"),
            request_timeout_seconds=float(payload.get("request_timeout_seconds", 28800.0)),
            stream_update_min_interval_seconds=float(
                payload.get("stream_update_min_interval_seconds", 0.35)
            ),
            stream_update_min_chars=int(payload.get("stream_update_min_chars", 24)),
            project_path=payload.get("project_path", "."),
            file_browser_enabled=bool(payload.get("file_browser_enabled", False)),
            log_level=str(payload.get("log_level", "INFO")).upper(),
            default_language=str(payload.get("default_language", "zh")).lower(),
            translations_path=str(payload.get("translations_path", "settings/i18n.json")),
            config_path=config_path,
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Validate required fields before startup."""
        if not self.telegram_bot_token:
            raise ValueError("'telegram_bot_token' is required")
        if self.request_timeout_seconds <= 0:
            raise ValueError("'request_timeout_seconds' must be > 0")
        if self.stream_update_min_interval_seconds < 0:
            raise ValueError("'stream_update_min_interval_seconds' must be >= 0")
        if self.stream_update_min_chars < 0:
            raise ValueError("'stream_update_min_chars' must be >= 0")
        if not self.codex_model:
            raise ValueError("'codex_model' is required")
        if self.default_language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                "'default_language' must be one of: " + ", ".join(SUPPORTED_LANGUAGES)
            )
        if self.codex_reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError(
                "'codex_reasoning_effort' must be one of: "
                + ", ".join(SUPPORTED_REASONING_EFFORTS)
            )
        self._validate_cli_config("codex", self.codex_cli_path, self.codex_cli_fallback_paths)
        self._activate_codex_cli()

    def save(self) -> None:
        """Persist the active configuration back to disk."""
        if self.config_path is None:
            raise ValueError("Config path is not set")

        payload = {
            "telegram_bot_token": self.telegram_bot_token,
            "whitelist": self.whitelist,
            "codex_cli_path": self.codex_cli_path,
            "codex_cli_fallback_paths": self.codex_cli_fallback_paths,
            "codex_model": self.codex_model,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "openai_admin_api_key": self.openai_admin_api_key,
            "openai_organization_id": self.openai_organization_id,
            "openai_project_id": self.openai_project_id,
            "session_store_path": self.session_store_path,
            "request_timeout_seconds": self.request_timeout_seconds,
            "stream_update_min_interval_seconds": self.stream_update_min_interval_seconds,
            "stream_update_min_chars": self.stream_update_min_chars,
            "project_path": self.project_path,
            "file_browser_enabled": self.file_browser_enabled,
            "log_level": self.log_level,
            "default_language": self.default_language,
            "translations_path": self.translations_path,
        }
        with self.config_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _activate_codex_cli(self) -> None:
        """Resolve and activate the configured Codex CLI."""
        resolved_path, use_shell = self.resolve_cli_path()
        if not resolved_path:
            raise ValueError(
                "Codex CLI not found. Checked: "
                + ", ".join([self.codex_cli_path.strip() or "codex", *self.codex_cli_fallback_paths])
            )

        shell_path = os.environ.get("SHELL", "/bin/sh")
        self.codex_cli_path = resolved_path
        self.codex_cli_use_shell = use_shell
        self.codex_shell_path = shell_path

    def resolve_cli_path(self) -> tuple[str | None, bool]:
        """Resolve the configured Codex CLI path or a configured fallback location."""
        configured_name = self.codex_cli_path.strip() or "codex"
        configured = Path(configured_name).expanduser()
        if configured.is_file():
            return str(configured), False

        discovered = shutil.which(configured_name)
        if discovered:
            return discovered, False

        for candidate in self.codex_cli_fallback_paths:
            candidate_path = Path(candidate).expanduser()
            if candidate_path.is_file():
                return str(candidate_path), False
            discovered = shutil.which(candidate)
            if discovered:
                return discovered, False

        if self._is_available_in_login_shell(configured_name):
            return configured_name, True

        return None, False

    def cli_path(self) -> str:
        """Return the resolved executable path for Codex."""
        return self.codex_cli_path

    def cli_use_shell(self) -> bool:
        """Return whether Codex should be invoked via a login shell."""
        return self.codex_cli_use_shell

    def cli_shell_path(self) -> str:
        """Return the shell path used to launch Codex."""
        return self.codex_shell_path

    @staticmethod
    def _validate_cli_config(cli_name: str, cli_path: str, fallback_paths: list[str]) -> None:
        """Validate configured CLI locator fields."""
        if not cli_path.strip():
            raise ValueError(f"'{cli_name}' CLI path must be a non-empty string")
        if not isinstance(fallback_paths, list):
            raise ValueError(f"'{cli_name}' CLI fallback paths must be a list of strings")
        if not all(isinstance(path, str) and path.strip() for path in fallback_paths):
            raise ValueError(f"'{cli_name}' CLI fallback paths must contain non-empty strings")

    @staticmethod
    def _is_available_in_login_shell(command: str) -> bool:
        """Check whether a command is available via the user's login shell."""
        shell = os.environ.get("SHELL", "/bin/sh")
        result = subprocess.run(
            [shell, "-lc", f"command -v {shlex.quote(command)} >/dev/null 2>&1"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0


@dataclass(slots=True)
class UserSessionState:
    """All sessions for a Telegram user."""

    current_session: str | None = None
    sessions: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    backend_sessions: dict[str, dict[str, str]] = field(default_factory=dict)
    session_project_paths: dict[str, str] = field(default_factory=dict)
    language: str = "zh"
