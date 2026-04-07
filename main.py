from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path

from tgboter.codex_client import CodexClient
from tgboter.config import Config
from tgboter.logging_config import configure_logging
from tgboter.openai_usage_client import OpenAIUsageClient
from tgboter.runtime_paths import lock_path
from tgboter.session_store import SessionStore
from tgboter.telegram_bot import TelegramCodexBot

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
RESTART_LOG_PATH = PROJECT_ROOT / ".bot_restart.log"
RESTART_IN_PROGRESS_ENV = "TGBOT_RESTART_IN_PROGRESS"
RESTART_GRACE_SECONDS = 3.0


class SingleInstanceLock:
    """Prevent multiple local bot processes from running at the same time."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            LOGGER.error("Another bot instance is already running. lock=%s owner_pid=%s", self.path, owner)
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return

        self._handle.seek(0)
        self._handle.truncate()
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


async def async_main() -> None:
    """Application entrypoint."""
    os.chdir(PROJECT_ROOT)
    config = Config.load(PROJECT_ROOT / "settings" / "bot_config.json")
    configure_logging(config.log_level)
    instance_lock = SingleInstanceLock(lock_path())
    if not instance_lock.acquire():
        return
    store = SessionStore(config.session_store_path, default_language=config.default_language)
    codex_client = CodexClient(config)
    usage_client = OpenAIUsageClient(config)
    bot = TelegramCodexBot(config, store, codex_client, usage_client)
    try:
        await bot.run()
    except asyncio.CancelledError:
        if not bot.restart_requested:
            raise
        LOGGER.warning("Bot run loop was cancelled during restart; continuing with restart flow")
    finally:
        instance_lock.release()

    if bot.restart_requested:
        restart_argv = [sys.executable, str(PROJECT_ROOT / "main.py"), *sys.argv[1:]]
        restart_env = os.environ.copy()
        restart_env.setdefault("PYTHONUNBUFFERED", "1")
        restart_env[RESTART_IN_PROGRESS_ENV] = "1"
        LOGGER.warning(
            "Restarting bot process after %.1fs grace period: executable=%s argv=%s",
            RESTART_GRACE_SECONDS,
            sys.executable,
            restart_argv,
        )
        await asyncio.sleep(RESTART_GRACE_SECONDS)
        try:
            os.execve(sys.executable, restart_argv, restart_env)
        except Exception:
            LOGGER.exception("Exec restart failed, falling back to detached child process")
            with RESTART_LOG_PATH.open("ab") as restart_log:
                subprocess.Popen(
                    restart_argv,
                    cwd=PROJECT_ROOT,
                    env=restart_env,
                    stdin=subprocess.DEVNULL,
                    stdout=restart_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )


if __name__ == "__main__":
    asyncio.run(async_main())
