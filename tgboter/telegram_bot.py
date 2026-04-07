from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from decimal import Decimal
from pathlib import Path
from typing import Final

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tgboter.change_tracker import FileChange, WorkspaceSnapshot
from tgboter.codex_client import CodexClient, CodexExecutionStopped, CodexStreamEvent, CodexUsage
from tgboter.config import Config, SUPPORTED_REASONING_EFFORTS, UserSessionState
from tgboter.i18n import I18n, SUPPORTED_LANGUAGES
from tgboter.openai_usage_client import OpenAIUsageClient
from tgboter.session_store import SessionStore

LOGGER = logging.getLogger(__name__)
TELEGRAM_MESSAGE_LIMIT: Final[int] = 4096
TELEGRAM_SAFE_TEXT_LIMIT: Final[int] = 3500
TYPING_HEARTBEAT_SECONDS: Final[float] = 4.0
DEFAULT_CHAT_ACTION: Final[str] = ChatAction.TYPING
MODEL_CACHE_PATH: Final[Path] = Path.home() / ".codex" / "models_cache.json"
RESTART_NOTIFY_CHAT_ID_ENV: Final[str] = "TGBOT_RESTART_NOTIFY_CHAT_ID"
RESTART_NOTIFY_USER_ID_ENV: Final[str] = "TGBOT_RESTART_NOTIFY_USER_ID"
RESTART_IN_PROGRESS_ENV: Final[str] = "TGBOT_RESTART_IN_PROGRESS"
RESTART_POLLING_GRACE_SECONDS: Final[float] = 3.0
FILE_BROWSER_PAGE_SIZE: Final[int] = 18
FILE_BROWSER_TOKEN_CACHE_LIMIT: Final[int] = 4096
DEFAULT_MODEL_OPTIONS: Final[tuple[str, ...]] = (
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5",
)
BOT_MENU_COMMAND_NAMES: Final[tuple[str, ...]] = (
    "help",
    "session_reset",
    "session_new",
    "session_list",
    "session_details",
    "session_switch",
    "project",
    "usage",
    "files",
    "stop",
    "clear_sessions",
    "status",
    "restart",
)
MAX_COMPLETION_CHANGE_FILES: Final[int] = 12


class TelegramCodexBot:
    """Telegram bot that forwards authorized user messages to Codex."""

    def __init__(
        self,
        config: Config,
        store: SessionStore,
        codex_client: CodexClient,
        usage_client: OpenAIUsageClient,
    ) -> None:
        self.config = config
        self.store = store
        self.codex_client = codex_client
        self.usage_client = usage_client
        self.i18n = I18n(config.translations_path, default_language=config.default_language)
        self.project_path = Path(config.project_path).resolve()
        self._started_monotonic = time.monotonic()
        self._active_requests_lock = asyncio.Lock()
        self._active_requests: set[asyncio.Task[None]] = set()
        self._file_browser_targets: dict[str, Path] = {}
        self._shutdown_event = asyncio.Event()
        self._restart_requested = False
        self.application: Application = (
            ApplicationBuilder()
            .token(config.telegram_bot_token)
            .post_init(self._post_init)
            .concurrent_updates(True)
            .build()
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register command and message handlers."""
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("session_new", self.new_command))
        self.application.add_handler(CommandHandler("session_list", self.list_command))
        self.application.add_handler(CommandHandler("session_details", self.sessions_command))
        self.application.add_handler(CommandHandler("session_switch", self.switch_command))
        self.application.add_handler(CommandHandler("session_reset", self.reset_command))
        self.application.add_handler(CommandHandler("project", self.project_command))
        self.application.add_handler(CommandHandler("files", self.ls_command))
        self.application.add_handler(CommandHandler("usage", self.token_command))
        self.application.add_handler(CommandHandler("stop", self.stop_command))
        self.application.add_handler(CommandHandler("restart", self.restart_command))
        self.application.add_handler(CommandHandler("clear_sessions", self.clear_all_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)

    def _bot_menu_commands(self, language: str) -> list[BotCommand]:
        """Build Telegram command descriptions in the configured default language."""
        return [
            BotCommand(name, self._t(f"command.{name}", language=language))
            for name in BOT_MENU_COMMAND_NAMES
        ]

    async def _user_language(self, user_id: int) -> str:
        """Resolve the stored language for a user."""
        return self.i18n.normalize_language(await self.store.get_language(user_id))

    def _t(self, key: str, language: str | None = None, **kwargs: object) -> str:
        """Translate a UI string."""
        return self.i18n.text(key, language=language, **kwargs)

    async def _t_user(self, user_id: int, key: str, **kwargs: object) -> str:
        """Translate a UI string for a specific user."""
        return self._t(key, language=await self._user_language(user_id), **kwargs)

    def _language_name(self, language: str, display_language: str | None = None) -> str:
        """Return the localized display name for a language code."""
        resolved = self.i18n.normalize_language(language)
        return self._t(f"keyboard.language_{resolved}", language=display_language or resolved)

    async def _post_init(self, application: Application) -> None:
        """Set Telegram command list on startup."""
        await application.bot.set_my_commands(self._bot_menu_commands(self.config.default_language))
        LOGGER.info("Telegram bot commands registered")

    async def run(self) -> None:
        """Start polling updates."""
        LOGGER.info("Starting Telegram bot polling")
        await self.application.initialize()
        await self._post_init(self.application)
        await self.application.start()
        if self.application.updater is None:
            raise RuntimeError("Telegram updater is not available")
        await self._wait_for_restart_polling_window()
        await self.application.updater.start_polling(
            drop_pending_updates=False,
            bootstrap_retries=3,
            error_callback=self._handle_polling_error,
        )
        await self._send_startup_restart_notice()
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            if not self._shutdown_event.is_set():
                raise
            LOGGER.info("Run loop cancelled after shutdown request; continuing graceful shutdown")
        finally:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()

    def _handle_polling_error(self, error: TelegramError) -> None:
        """Keep polling errors concise, especially for cross-instance conflicts."""
        if isinstance(error, Conflict):
            LOGGER.warning(
                "Telegram polling conflict detected; updater will retry automatically. "
                "Another polling client may still be releasing the token."
            )
            return
        LOGGER.exception("Telegram polling failed: %s", error, exc_info=error)

    async def _wait_for_restart_polling_window(self) -> None:
        """Give Telegram time to release the previous long-poll connection after /restart."""
        if os.environ.get(RESTART_IN_PROGRESS_ENV, "").strip() != "1":
            return

        LOGGER.warning(
            "Restart startup detected; waiting %.1fs before polling to avoid transient conflicts",
            RESTART_POLLING_GRACE_SECONDS,
        )
        await asyncio.sleep(RESTART_POLLING_GRACE_SECONDS)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show a concise help view with distinct action buttons."""
        if not await self._authorize(update):
            return
        await self._reply_with_help(update)

    async def new_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a new session."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        session_id = await self.store.create_session(update.effective_user.id)
        LOGGER.info("Created new session user_id=%s session_id=%s", update.effective_user.id, session_id)
        await self._safe_reply(
            update,
            self._t("message.session_created", language=language, session_id=session_id),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List sessions for the current user."""
        if not await self._authorize(update):
            return
        await self._reply_with_session_list(update)

    async def sessions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List sessions with message counts."""
        if not await self._authorize(update):
            return
        await self._reply_with_session_list(update, detailed=True)

    async def switch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch the current session."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        if not context.args:
            await self._safe_reply(update, self._t("message.switch_usage", language=language))
            return
        session_id = context.args[0].strip()
        switched = await self.store.switch_session(update.effective_user.id, session_id)
        if not switched:
            await self._safe_reply(
                update,
                self._t("message.session_not_found", language=language, session_id=session_id),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        LOGGER.info("Switched session user_id=%s session_id=%s", update.effective_user.id, session_id)
        await self._safe_reply(
            update,
            self._t("message.session_switched", language=language, session_id=session_id),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset current session history."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        session_id = await self.store.reset_current_session(update.effective_user.id)
        LOGGER.info("Reset session user_id=%s session_id=%s", update.effective_user.id, session_id)
        await self._safe_reply(
            update,
            self._t("message.session_reset", language=language, session_id=session_id),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def clear_all_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Clear all stored sessions for all users."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        await self.store.clear_all_sessions()
        LOGGER.warning("Cleared all sessions by user_id=%s", update.effective_user.id)
        await self._safe_reply(
            update,
            self._t("message.sessions_cleared", language=language),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def restart_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Restart the bot process after acknowledging the request."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        assert update.effective_chat is not None
        language = await self._user_language(update.effective_user.id)
        task_count, process_count = await self._stop_all_running_requests(exclude=asyncio.current_task())
        os.environ[RESTART_NOTIFY_CHAT_ID_ENV] = str(update.effective_chat.id)
        os.environ[RESTART_NOTIFY_USER_ID_ENV] = str(update.effective_user.id)
        self._restart_requested = True
        LOGGER.warning(
            "Restart requested by user_id=%s chat_id=%s stopped_tasks=%s stopped_processes=%s",
            update.effective_user.id,
            update.effective_chat.id,
            task_count,
            process_count,
        )
        await self._safe_reply(
            update,
            self._t("message.restart_requested", language=language),
            parse_mode=ParseMode.MARKDOWN,
        )
        self._shutdown_event.set()

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Terminate all currently running Codex CLI tasks."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        task_count, process_count = await self._stop_all_running_requests(exclude=asyncio.current_task())
        LOGGER.warning(
            "Stop requested by user_id=%s tasks=%s processes=%s",
            update.effective_user.id,
            task_count,
            process_count,
        )
        if task_count or process_count:
            await self._safe_reply(
                update,
                self._t(
                    "message.stop_result",
                    language=language,
                    task_count=task_count,
                    process_count=process_count,
                ),
            )
            return
        await self._safe_reply(update, self._t("message.stop_none", language=language))

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show a detailed runtime and session status view."""
        if not await self._authorize(update):
            return
        await self._reply_with_status(update)

    async def ls_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open the interactive file browser or explain how to enable it."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        text, reply_markup, parse_mode = self._file_browser_disabled_view(language)
        if self.config.file_browser_enabled:
            view = self._build_directory_browser_view(self.project_path, language=language, page=0)
            text, reply_markup, parse_mode = view
        await self._safe_reply(update, text, parse_mode=parse_mode, reply_markup=reply_markup)

    async def token_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show token usage summary using OpenAI organization APIs."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        await self._safe_reply(
            update,
            await self._token_summary_text(language),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def project_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show or switch the active project path."""
        if not await self._authorize(update):
            return
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)

        if not context.args:
            await self._safe_reply(
                update,
                self._t("message.project_current", language=language, path=self.project_path),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        requested_path = Path(" ".join(context.args)).expanduser()
        if not requested_path.is_absolute():
            requested_path = (self.project_path / requested_path).resolve()

        if not requested_path.exists():
            await self._safe_reply(
                update,
                self._t("message.project_missing", language=language, path=requested_path),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not requested_path.is_dir():
            await self._safe_reply(
                update,
                self._t("message.project_not_dir", language=language, path=requested_path),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        self.project_path = requested_path
        self.config.project_path = str(requested_path)
        self.codex_client.set_project_path(requested_path)
        self.config.save()

        session_id = await self.store.reset_current_session(update.effective_user.id)
        LOGGER.info("Switched project path to %s and reset session=%s", requested_path, session_id)
        await self._safe_reply(
            update,
            self._t(
                "message.project_switched",
                language=language,
                path=requested_path,
                session_id=session_id,
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward user text messages to Codex and return the answer."""
        if not await self._authorize(update):
            return

        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        language = await self._user_language(user.id)
        user_text = (message.text or "").strip()
        if not user_text:
            await self._safe_reply(update, self._t("message.empty_ignored", language=language))
            return
        if self._is_stop_message(user_text):
            await self.stop_command(update, context)
            return

        session_id = await self.store.get_current_session_id(user.id)
        await self.store.append_message(user.id, "user", user_text, session_id=session_id)
        LOGGER.info("Forwarding message user_id=%s session_id=%s text=%s", user.id, session_id, user_text[:200])

        processing_text = self._t("message.processing", language=language)
        stream_message = await self._safe_reply_message(update, processing_text)
        stream_state: dict[str, object] = {
            "messages": [stream_message] if stream_message is not None else [],
            "rendered_chunks": [processing_text] if stream_message is not None else [],
            "finalized_chunks": 0,
            "last_preview": "",
            "last_sent_at": 0.0,
            "assistant_offset": 0,
            "last_assistant_text": "",
            "chat_action": DEFAULT_CHAT_ACTION,
            "language": language,
        }
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_heartbeat(message.chat_id, typing_stop, stream_state))

        current_task = asyncio.current_task()
        if current_task is not None:
            await self._register_active_request(current_task)

        try:
            before_snapshot = self._capture_workspace_snapshot()
            backend_session_id = await self.store.get_backend_session_id(user.id, session_id=session_id)
            started_at = time.perf_counter()

            async def on_event(event: CodexStreamEvent) -> None:
                if event.kind == "assistant_text" and event.text is not None:
                    self._set_stream_chat_action(stream_state, ChatAction.TYPING)
                    await self._update_stream_message(
                        update,
                        event.text,
                        stream_state,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                if event.kind == "tool_started":
                    await self._send_tool_event_message(update, stream_state, event, started=True)
                    return
                if event.kind == "tool_completed":
                    await self._send_tool_event_message(update, stream_state, event, started=False)

            result = await asyncio.wait_for(
                self.codex_client.send_message(
                    user_text,
                    backend_session_id=backend_session_id,
                    on_event=on_event,
                ),
                timeout=self.config.request_timeout_seconds,
            )
            reply = result.text
            if not await self.store.session_exists(user.id, session_id):
                LOGGER.warning(
                    "Skipping persistence for cleared session user_id=%s session_id=%s",
                    user.id,
                    session_id,
                )
                await self._finalize_stream_message(
                    update,
                    stream_state,
                    reply,
                    parse_mode=ParseMode.MARKDOWN,
                )
                elapsed_seconds = time.perf_counter() - started_at
                await self._safe_reply(
                    update,
                    self._build_completion_text(elapsed_seconds, result.usage, language=language),
                )
                file_changes = self._workspace_changes_since(before_snapshot)
                if file_changes:
                    await self._safe_reply(
                        update,
                        self._build_change_summary_text(file_changes, language=language),
                    )
                return
            if result.backend_session_id:
                await self.store.set_backend_session_id(
                    user.id,
                    result.backend_session_id,
                    session_id=session_id,
                )
            await self._finalize_stream_message(
                update,
                stream_state,
                reply,
                parse_mode=ParseMode.MARKDOWN,
            )
            elapsed_seconds = time.perf_counter() - started_at
            file_changes = self._workspace_changes_since(before_snapshot)
            await self._safe_reply(
                update,
                self._build_completion_text(elapsed_seconds, result.usage, language=language),
            )
            if file_changes:
                await self._safe_reply(
                    update,
                    self._build_change_summary_text(file_changes, language=language),
                )
            await self.store.append_message(user.id, "assistant", reply, session_id=session_id)
        except TimeoutError:
            LOGGER.exception("Codex request timeout user_id=%s session_id=%s", user.id, session_id)
            active_message = self._active_stream_message(stream_state)
            if active_message is not None:
                await self._safe_edit_text(active_message, self._t("message.timeout", language=language))
            else:
                await self._safe_reply(update, self._t("message.timeout", language=language))
        except CodexExecutionStopped:
            LOGGER.warning("Codex request stopped user_id=%s session_id=%s", user.id, session_id)
            active_message = self._active_stream_message(stream_state)
            if active_message is not None:
                await self._safe_edit_text(active_message, self._t("message.stopped_by_stop", language=language))
            else:
                await self._safe_reply(update, self._t("message.stopped_by_stop", language=language))
        except asyncio.CancelledError:
            LOGGER.warning("Codex request cancelled user_id=%s session_id=%s", user.id, session_id)
            active_message = self._active_stream_message(stream_state)
            if active_message is not None:
                await self._safe_edit_text(active_message, self._t("message.stopped", language=language))
            else:
                await self._safe_reply(update, self._t("message.stopped", language=language))
        except Exception as exc:
            LOGGER.exception("Codex request failed user_id=%s session_id=%s", user.id, session_id)
            error_text = self._t(
                "message.request_failed",
                language=language,
                error_type=type(exc).__name__,
                error=exc,
            )
            active_message = self._active_stream_message(stream_state)
            if active_message is not None:
                await self._safe_edit_text(active_message, error_text)
            else:
                await self._safe_reply(update, error_text)
        finally:
            typing_stop.set()
            await asyncio.gather(typing_task, return_exceptions=True)
            if current_task is not None:
                await self._unregister_active_request(current_task)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log unhandled telegram errors."""
        LOGGER.exception("Telegram update handling failed: %s", context.error)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard actions for help/status views and configuration."""
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        if not await self._authorize(update):
            if query:
                await query.answer(
                    self._t("callback.unauthorized", language=self.config.default_language),
                    show_alert=True,
                )
            return
        language = await self._user_language(user.id)

        data = query.data or ""
        await query.answer()

        if data in {"view:help", "selector:refresh"}:
            await self._safe_edit_text(
                query.message,
                await self._help_text(user.id),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="help"),
            )
            return

        if data == "view:status":
            await self._safe_edit_text(
                query.message,
                await self._status_text(user.id),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="status"),
            )
            return

        if data == "view:sessions":
            await self._safe_edit_text(
                query.message,
                await self._session_list_text(user.id, detailed=True),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="status"),
            )
            return

        if data == "view:token":
            await self._safe_edit_text(
                query.message,
                await self._token_summary_text(language),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="status"),
            )
            return

        if data == "view:files":
            if not self.config.file_browser_enabled:
                text, reply_markup, parse_mode = self._file_browser_disabled_view(language)
                await self._safe_edit_text(
                    query.message,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return
            text, reply_markup, parse_mode = self._build_directory_browser_view(
                self.project_path,
                language=language,
                page=0,
            )
            await self._safe_edit_text(
                query.message,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return

        if data == "action:new_session":
            session_id = await self.store.create_session(user.id)
            LOGGER.info("Created new session from callback user_id=%s session_id=%s", user.id, session_id)
            await self._safe_edit_text(
                query.message,
                await self._status_text(
                    user.id,
                    extra=self._t("message.session_created_and_switched", language=language, session_id=session_id),
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="status"),
            )
            return

        if data in {"selector:models", "view:models"}:
            await self._safe_edit_text(
                query.message,
                await self._status_text(user.id, extra=self._t("message.select_model", language=language)),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="models"),
            )
            return

        if data in {"selector:reasoning", "view:reasoning"}:
            await self._safe_edit_text(
                query.message,
                await self._status_text(user.id, extra=self._t("message.select_reasoning", language=language)),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="reasoning"),
            )
            return

        if data == "view:language":
            await self._safe_edit_text(
                query.message,
                await self._help_text(user.id),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="language"),
            )
            return

        if data.startswith("set:model:"):
            model_name = data.removeprefix("set:model:")
            self.config.codex_model = model_name
            self.config.save()
            LOGGER.info("Model changed by user_id=%s model=%s", user.id, model_name)
            await self._safe_edit_text(
                query.message,
                await self._status_text(
                    user.id,
                    extra=self._t("message.model_switched", language=language, model_name=model_name),
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="status"),
            )
            return

        if data.startswith("set:reasoning:"):
            effort = data.removeprefix("set:reasoning:")
            if effort not in SUPPORTED_REASONING_EFFORTS:
                await query.answer(self._t("callback.invalid_reasoning", language=language), show_alert=True)
                return
            self.config.codex_reasoning_effort = effort
            self.config.save()
            LOGGER.info("Reasoning effort changed by user_id=%s effort=%s", user.id, effort)
            await self._safe_edit_text(
                query.message,
                await self._status_text(
                    user.id,
                    extra=self._t("message.reasoning_switched", language=language, effort=effort),
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=language, mode="status"),
            )
            return

        if data.startswith("set:lang:"):
            new_language = self.i18n.normalize_language(data.removeprefix("set:lang:"))
            if new_language not in SUPPORTED_LANGUAGES:
                return
            await self.store.set_language(user.id, new_language)
            await self._safe_edit_text(
                query.message,
                await self._help_text(
                    user.id,
                    extra=self._t(
                        "language.changed",
                        language=new_language,
                        language_name=self._language_name(new_language, display_language=new_language),
                    ),
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._build_selector_keyboard(language=new_language, mode="help"),
            )
            return

        if data.startswith("fs:dir:"):
            path, page = self._resolve_file_browser_target(data, prefix="fs:dir:")
            if path is None:
                await query.answer(self._t("files.invalid_target", language=language), show_alert=True)
                return
            text, reply_markup, parse_mode = self._build_directory_browser_view(
                path,
                language=language,
                page=page,
            )
            await self._safe_edit_text(
                query.message,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return

        if data.startswith("fs:file:"):
            path, _ = self._resolve_file_browser_target(data, prefix="fs:file:")
            if path is None:
                await query.answer(self._t("files.invalid_target", language=language), show_alert=True)
                return
            text, reply_markup, parse_mode = self._build_file_preview_view(path, language=language)
            await self._safe_edit_text(
                query.message,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return

    async def _authorize(self, update: Update) -> bool:
        """Check whitelist authorization for every incoming update."""
        user = update.effective_user
        chat = update.effective_chat
        if user is None:
            LOGGER.warning(
                "[SECURITY] Rejecting update without effective_user: update_id=%s chat_id=%s chat_type=%s",
                getattr(update, "update_id", None),
                getattr(chat, "id", None),
                getattr(chat, "type", None),
            )
            return False
        LOGGER.info(
            "Auth check update_id=%s user_id=%s username=%s chat_id=%s chat_type=%s whitelist=%s",
            getattr(update, "update_id", None),
            user.id,
            user.username,
            getattr(chat, "id", None),
            getattr(chat, "type", None),
            self.config.whitelist,
        )
        if user.id not in self.config.whitelist:
            LOGGER.warning(
                "[SECURITY] Unauthorized access attempt: user_id=%s username=%s chat_id=%s chat_type=%s whitelist=%s",
                user.id,
                user.username,
                getattr(chat, "id", None),
                getattr(chat, "type", None),
                self.config.whitelist,
            )
            if update.effective_message:
                await update.effective_message.reply_text(
                    self._t("auth.unauthorized", user_id=user.id)
                )
            return False
        LOGGER.info("Access granted user_id=%s username=%s", user.id, user.username)
        return True

    async def _reply_with_session_list(self, update: Update, detailed: bool = False) -> None:
        """Format and send session list output."""
        assert update.effective_user is not None
        await self._safe_reply(
            update,
            await self._session_list_text(update.effective_user.id, detailed=detailed),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _reply_with_help(self, update: Update) -> None:
        """Show help text focused on usage guidance and quick actions."""
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        await self._safe_reply(
            update,
            await self._help_text(update.effective_user.id),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._build_selector_keyboard(language=language, mode="help"),
        )

    async def _reply_with_status(self, update: Update) -> None:
        """Show a richer status view for the current user."""
        assert update.effective_user is not None
        language = await self._user_language(update.effective_user.id)
        await self._safe_reply(
            update,
            await self._status_text(update.effective_user.id),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._build_selector_keyboard(language=language, mode="status"),
        )

    async def _help_text(self, user_id: int, extra: str | None = None) -> str:
        """Render a concise help page without duplicating the status screen."""
        state = await self.store.get_user_state(user_id)
        language = self.i18n.normalize_language(state.language)
        current_session_id = state.current_session or ""
        current_title = self._session_title(state.sessions.get(current_session_id, []), language=language)
        parts: list[str] = []
        if extra:
            parts.extend([extra, ""])
        parts.extend(
            [
                self._t("help.intro", language=language),
                "",
                self._t("help.body", language=language),
                "",
                self._t("help.context", language=language),
                self._t("help.current_session", language=language, session_id=current_session_id),
                self._t("help.current_title", language=language, title=current_title),
                self._t("help.session_count", language=language, count=len(state.sessions)),
                self._t(
                    "help.current_language",
                    language=language,
                    language_name=self._language_name(language, display_language=language),
                ),
                "",
                self._t("help.actions", language=language),
                self._t("help.action.status", language=language),
                self._t("help.action.new_session", language=language),
                self._t("help.action.session_details", language=language),
                self._t("help.action.project", language=language),
                self._t("help.action.file_browser", language=language),
                self._t("help.action.stop", language=language),
                self._t("help.action.restart", language=language),
                "",
                self._t("help.commands", language=language),
                "`/help /status /session_new /session_list /session_details /session_switch /session_reset /project /files /usage /stop /restart /clear_sessions`",
            ]
        )
        return "\n".join(parts)

    @property
    def restart_requested(self) -> bool:
        """Expose whether a user-triggered restart was requested."""
        return self._restart_requested

    async def _status_text(self, user_id: int, extra: str | None = None) -> str:
        """Render a detailed runtime and session snapshot."""
        state = await self.store.get_user_state(user_id)
        language = self.i18n.normalize_language(state.language)
        current_session_id = state.current_session or ""
        current_messages = state.sessions.get(current_session_id, [])
        backend_session_id = state.backend_sessions.get(current_session_id)
        total_messages = sum(len(messages) for messages in state.sessions.values())
        current_user_messages = sum(1 for message in current_messages if message.get("role") == "user")
        current_assistant_messages = sum(1 for message in current_messages if message.get("role") == "assistant")
        active_requests = await self._active_request_count()
        titled_sessions = sum(
            1
            for messages in state.sessions.values()
            if self._session_title(messages, language=language) != self._t("session.empty", language=language)
        )
        parts = [self._t("status.title", language=language), ""]
        if extra:
            parts.extend([extra, ""])

        parts.extend(
            [
                self._t("status.runtime", language=language),
                self._t("status.model", language=language, model=self.config.codex_model),
                self._t("status.reasoning", language=language, effort=self.config.codex_reasoning_effort),
                self._t("status.project_path", language=language, path=self.project_path),
                self._t(
                    "status.file_browser",
                    language=language,
                    state=self._t(
                        "state.enabled" if self.config.file_browser_enabled else "state.disabled",
                        language=language,
                    ),
                ),
                self._t("status.active_requests", language=language, count=active_requests),
                self._t("status.uptime", language=language, uptime=self._format_uptime(language=language)),
                "",
                self._t("status.current_session", language=language),
                self._t("status.session_id", language=language, session_id=current_session_id),
                self._t(
                    "status.title_line",
                    language=language,
                    title=self._session_title(current_messages, language=language),
                ),
                self._t(
                    "status.thread",
                    language=language,
                    thread=backend_session_id or self._t("session.new_thread", language=language),
                ),
                self._t("status.message_count", language=language, count=len(current_messages)),
                self._t("status.user_messages", language=language, count=current_user_messages),
                self._t("status.assistant_messages", language=language, count=current_assistant_messages),
                self._t(
                    "status.last_message",
                    language=language,
                    summary=self._last_message_summary(
                        current_messages[-1] if current_messages else None,
                        language=language,
                    ),
                ),
                "",
                self._t("status.all_sessions", language=language),
                self._t("status.total_sessions", language=language, count=len(state.sessions)),
                self._t("status.bound_sessions", language=language, count=len(state.backend_sessions)),
                self._t("status.titled_sessions", language=language, count=titled_sessions),
                self._t("status.total_messages", language=language, count=total_messages),
                "",
                self._t("status.preview", language=language),
            ]
        )
        parts.extend(self._session_preview_lines(state, language=language))
        parts.extend(["", self._t("status.token_usage", language=language), *await self._token_status_lines(language)])
        return "\n".join(parts)

    async def _token_summary_text(self, language: str | None = None) -> str:
        """Render the standalone token usage summary."""
        language = self.i18n.normalize_language(language)
        if not self.usage_client.is_configured():
            return self._t("token.not_configured_full", language=language)

        try:
            summary = await self.usage_client.get_usage_summary()
        except Exception as exc:
            LOGGER.exception("Token usage query failed: %s", exc)
            return self._t(
                "token.query_failed",
                language=language,
                error_type=type(exc).__name__,
                error=exc,
            )

        cost_text = "N/A"
        if summary.last_30d_cost_value is not None:
            cost_value = summary.last_30d_cost_value.quantize(Decimal("0.0001"))
            cost_text = f"{cost_value} {summary.last_30d_cost_currency or 'USD'}"

        return self._t(
            "token.summary",
            language=language,
            date=summary.local_date_label,
            scope=summary.project_scope,
            today_input=f"{summary.today_input_tokens:,}",
            today_output=f"{summary.today_output_tokens:,}",
            today_total=f"{summary.today_total_tokens:,}",
            month_input=f"{summary.last_30d_input_tokens:,}",
            month_output=f"{summary.last_30d_output_tokens:,}",
            month_total=f"{summary.last_30d_total_tokens:,}",
            cost=cost_text,
        )

    async def _token_status_lines(self, language: str) -> list[str]:
        """Render short token status lines for embedding in /status."""
        if not self.usage_client.is_configured():
            return [self._t("token.not_configured_short", language=language)]

        try:
            summary = await self.usage_client.get_usage_summary()
        except Exception as exc:
            LOGGER.exception("Token usage query failed during status build: %s", exc)
            return [
                self._t(
                    "token.query_failed_short",
                    language=language,
                    error_type=type(exc).__name__,
                )
            ]

        cost_text = "N/A"
        if summary.last_30d_cost_value is not None:
            cost_value = summary.last_30d_cost_value.quantize(Decimal("0.0001"))
            cost_text = f"{cost_value} {summary.last_30d_cost_currency or 'USD'}"

        return [
            self._t("token.status.date", language=language, date=summary.local_date_label),
            self._t("token.status.scope", language=language, scope=summary.project_scope),
            self._t(
                "token.status.today",
                language=language,
                today_input=f"{summary.today_input_tokens:,}",
                today_output=f"{summary.today_output_tokens:,}",
                today_total=f"{summary.today_total_tokens:,}",
            ),
            self._t("token.status.month_total", language=language, month_total=f"{summary.last_30d_total_tokens:,}"),
            self._t("token.status.cost", language=language, cost=cost_text),
        ]

    async def _session_list_text(self, user_id: int, detailed: bool = False) -> str:
        """Render session list output with optional titles and backend thread ids."""
        state = await self.store.get_user_state(user_id)
        language = self.i18n.normalize_language(state.language)
        lines = [
            self._t("session.list.current", language=language, session_id=state.current_session),
            self._t("session.list.total", language=language, count=len(state.sessions)),
            "",
        ]
        for session_id, messages in state.sessions.items():
            marker = self._t("session.list.current_marker", language=language) if session_id == state.current_session else ""
            title = self._session_title(messages, language=language)
            if detailed:
                backend_session_id = state.backend_sessions.get(
                    session_id,
                    self._t("session.new_thread", language=language),
                )
                lines.append(
                    self._t(
                        "session.list.detail",
                        language=language,
                        session_id=session_id,
                        marker=marker,
                        message_count=len(messages),
                        thread=backend_session_id,
                        title=title,
                    )
                )
            else:
                lines.append(
                    self._t(
                        "session.list.simple",
                        language=language,
                        session_id=session_id,
                        marker=marker,
                        title=title,
                    )
                )
        return "\n".join(lines)

    async def _active_request_count(self) -> int:
        """Return the number of in-flight user requests."""
        async with self._active_requests_lock:
            return sum(1 for task in self._active_requests if not task.done())

    def _format_uptime(self, language: str) -> str:
        """Render process uptime as a short human-readable duration."""
        total_seconds = max(0, int(time.monotonic() - self._started_monotonic))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts: list[str] = []
        if days:
            parts.append(self._t("duration.day", language=language, count=days))
        if hours or days:
            parts.append(self._t("duration.hour", language=language, count=hours))
        if minutes or hours or days:
            parts.append(self._t("duration.minute", language=language, count=minutes))
        parts.append(self._t("duration.second", language=language, count=seconds))
        return "".join(parts)

    def _session_title(self, messages: list[dict[str, str]], language: str) -> str:
        """Derive a compact session title from the first user message."""
        for message in messages:
            if message.get("role") != "user":
                continue
            content = " ".join((message.get("content") or "").strip().split())
            if not content:
                continue
            preview = content[:48]
            if len(content) > 48:
                preview += "..."
            return preview
        return self._t("session.empty", language=language)

    def _last_message_summary(self, message: dict[str, str] | None, language: str) -> str:
        """Render a short summary for the latest message."""
        if not message:
            return self._t("session.none", language=language)
        role = message.get("role", "unknown")
        content = " ".join((message.get("content") or "").strip().split())
        if not content:
            return f"{role}: {self._t('session.empty', language=language)}"
        preview = content[:60]
        if len(content) > 60:
            preview += "..."
        return f"{role}: {preview}"

    def _session_preview_lines(self, state: UserSessionState, language: str) -> list[str]:
        """Format a short list of the most recent sessions."""
        session_items = list(state.sessions.items())[-3:]
        if not session_items:
            return [f"- {self._t('session.none', language=language)}"]
        lines: list[str] = []
        for session_id, messages in reversed(session_items):
            marker = self._t("session.list.current_marker", language=language) if session_id == state.current_session else ""
            backend_session_id = state.backend_sessions.get(session_id, self._t("session.new_thread", language=language))
            lines.append(
                f"- `{session_id}`{marker} | {self._session_title(messages, language=language)} | "
                f"messages=`{len(messages)}` | thread=`{backend_session_id}`"
            )
        return lines

    async def _register_active_request(self, task: asyncio.Task[None]) -> None:
        """Track an in-flight Telegram request task."""
        async with self._active_requests_lock:
            self._active_requests.add(task)

    async def _unregister_active_request(self, task: asyncio.Task[None]) -> None:
        """Forget a completed Telegram request task."""
        async with self._active_requests_lock:
            self._active_requests.discard(task)

    async def _stop_all_running_requests(
        self,
        exclude: asyncio.Task[object] | None = None,
    ) -> tuple[int, int]:
        """Cancel all in-flight request handlers and stop Codex subprocesses."""
        async with self._active_requests_lock:
            tasks = [task for task in self._active_requests if task is not exclude and not task.done()]

        for task in tasks:
            task.cancel()

        process_count = await self.codex_client.stop_all()
        return len(tasks), process_count

    @staticmethod
    def _is_stop_message(text: str) -> bool:
        """Allow short stop-like text to act as a stop shortcut."""
        return text.strip().lower() in {"stop", "停止", "终止"}

    def _build_selector_keyboard(self, language: str, mode: str = "status") -> InlineKeyboardMarkup:
        """Build distinct keyboards for help, status, and config selection."""
        if mode == "models":
            rows = self._build_model_rows()
            rows.append(
                [
                    InlineKeyboardButton(self._t("keyboard.back_status", language=language), callback_data="view:status"),
                    InlineKeyboardButton(self._t("keyboard.help", language=language), callback_data="view:help"),
                ]
            )
            return InlineKeyboardMarkup(rows)

        if mode == "reasoning":
            rows = self._build_reasoning_rows()
            rows.append(
                [
                    InlineKeyboardButton(self._t("keyboard.back_status", language=language), callback_data="view:status"),
                    InlineKeyboardButton(self._t("keyboard.help", language=language), callback_data="view:help"),
                ]
            )
            return InlineKeyboardMarkup(rows)

        if mode == "language":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            self._t("keyboard.language_zh", language=language),
                            callback_data="set:lang:zh",
                        ),
                        InlineKeyboardButton(
                            self._t("keyboard.language_en", language=language),
                            callback_data="set:lang:en",
                        ),
                    ],
                    [
                        InlineKeyboardButton(self._t("keyboard.back_status", language=language), callback_data="view:status"),
                        InlineKeyboardButton(self._t("keyboard.help", language=language), callback_data="view:help"),
                    ],
                ]
            )

        if mode == "help":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(self._t("keyboard.status", language=language), callback_data="view:status"),
                        InlineKeyboardButton(self._t("keyboard.new_session", language=language), callback_data="action:new_session"),
                    ],
                    [
                        InlineKeyboardButton(self._t("keyboard.sessions", language=language), callback_data="view:sessions"),
                        InlineKeyboardButton(self._t("keyboard.token_usage", language=language), callback_data="view:token"),
                    ],
                    [
                        InlineKeyboardButton(self._t("keyboard.models", language=language), callback_data="view:models"),
                        InlineKeyboardButton(self._t("keyboard.reasoning", language=language), callback_data="view:reasoning"),
                    ],
                    [
                        InlineKeyboardButton(self._file_browser_button_text(language), callback_data="view:files"),
                    ],
                    [
                        InlineKeyboardButton(self._t("keyboard.language", language=language), callback_data="view:language"),
                    ],
                ]
            )

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(self._t("keyboard.refresh_status", language=language), callback_data="view:status"),
                    InlineKeyboardButton(self._t("keyboard.help", language=language), callback_data="view:help"),
                ],
                [
                    InlineKeyboardButton(self._t("keyboard.sessions", language=language), callback_data="view:sessions"),
                    InlineKeyboardButton(self._t("keyboard.token_usage", language=language), callback_data="view:token"),
                ],
                [
                    InlineKeyboardButton(self._t("keyboard.models", language=language), callback_data="view:models"),
                    InlineKeyboardButton(self._t("keyboard.reasoning", language=language), callback_data="view:reasoning"),
                ],
                [
                    InlineKeyboardButton(self._file_browser_button_text(language), callback_data="view:files"),
                ],
            ]
        )

    def _file_browser_button_text(self, language: str) -> str:
        """Render the file browser button label according to feature state."""
        key = "keyboard.file_browser" if self.config.file_browser_enabled else "keyboard.file_browser_disabled"
        return self._t(key, language=language)

    def _file_browser_disabled_view(
        self,
        language: str,
    ) -> tuple[str, InlineKeyboardMarkup, str | None]:
        """Render the disabled-state file browser help view."""
        text = "\n\n".join(
            [
                self._t("files.disabled.title", language=language),
                self._t("files.disabled.body", language=language),
            ]
        )
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(self._t("keyboard.back_status", language=language), callback_data="view:status"),
                    InlineKeyboardButton(self._t("keyboard.help", language=language), callback_data="view:help"),
                ]
            ]
        )
        return text, reply_markup, ParseMode.MARKDOWN

    def _build_directory_browser_view(
        self,
        path: Path,
        *,
        language: str,
        page: int,
    ) -> tuple[str, InlineKeyboardMarkup, str | None]:
        """Render a browsable directory listing with folder/file buttons."""
        resolved = path.expanduser().resolve()
        if not self.project_path.exists():
            text = self._t("files.root_missing", language=language, path=self.project_path)
            return text, self._file_browser_footer(language), ParseMode.MARKDOWN
        if not resolved.exists():
            text = self._t("files.path_missing", language=language, path=resolved)
            return text, self._file_browser_footer(language), ParseMode.MARKDOWN
        if not resolved.is_dir():
            return self._build_file_preview_view(resolved, language=language)

        try:
            entries = sorted(
                resolved.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError as exc:
            text = self._t("files.path_denied", language=language, path=resolved, error=exc)
            return text, self._file_browser_footer(language), ParseMode.MARKDOWN

        total_pages = max(1, (len(entries) + FILE_BROWSER_PAGE_SIZE - 1) // FILE_BROWSER_PAGE_SIZE)
        current_page = min(max(page, 0), total_pages - 1)
        start = current_page * FILE_BROWSER_PAGE_SIZE
        end = start + FILE_BROWSER_PAGE_SIZE
        page_entries = entries[start:end]

        if page_entries:
            text = self._t(
                "files.dir.summary",
                language=language,
                path=resolved,
                project_path=self.project_path,
                page=current_page + 1,
                total_pages=total_pages,
                count=len(entries),
            )
            if total_pages > 1:
                text = f"{text}\n\n{self._t('files.dir.truncated', language=language)}"
        else:
            text = self._t("files.dir.empty", language=language, path=resolved)

        rows: list[list[InlineKeyboardButton]] = []
        if resolved.parent != resolved:
            parent_token = self._register_file_browser_target(resolved.parent)
            rows.append(
                [
                    InlineKeyboardButton(
                        self._t("keyboard.parent", language=language),
                        callback_data=f"fs:dir:{parent_token}:0",
                    )
                ]
            )

        for entry in page_entries:
            token = self._register_file_browser_target(entry)
            prefix = "📁" if entry.is_dir() else "📄"
            action = "dir" if entry.is_dir() else "file"
            label = self._trim_button_label(f"{prefix} {entry.name}")
            rows.append([InlineKeyboardButton(label, callback_data=f"fs:{action}:{token}:0")])

        if total_pages > 1:
            current_token = self._register_file_browser_target(resolved)
            paging_row: list[InlineKeyboardButton] = []
            if current_page > 0:
                paging_row.append(
                    InlineKeyboardButton(
                        self._t("keyboard.prev_page", language=language),
                        callback_data=f"fs:dir:{current_token}:{current_page - 1}",
                    )
                )
            if current_page < total_pages - 1:
                paging_row.append(
                    InlineKeyboardButton(
                        self._t("keyboard.next_page", language=language),
                        callback_data=f"fs:dir:{current_token}:{current_page + 1}",
                    )
                )
            if paging_row:
                rows.append(paging_row)

        rows.extend(self._file_browser_footer_rows(language))
        return text, InlineKeyboardMarkup(rows), ParseMode.MARKDOWN

    def _build_file_preview_view(
        self,
        path: Path,
        *,
        language: str,
    ) -> tuple[str, InlineKeyboardMarkup, str | None]:
        """Render a file preview and a back button to the parent directory."""
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            text = self._t("files.path_missing", language=language, path=resolved)
            return text, self._file_browser_footer(language), ParseMode.MARKDOWN
        if resolved.is_dir():
            return self._build_directory_browser_view(resolved, language=language, page=0)

        content = self._read_file_preview_content(resolved, language=language)
        text = self._fit_telegram_text(
            self._t("files.file.summary", language=language, path=resolved, content=content)
        )
        rows = [
            [
                InlineKeyboardButton(
                    self._t("keyboard.parent", language=language),
                    callback_data=f"fs:dir:{self._register_file_browser_target(resolved.parent)}:0",
                )
            ],
            *self._file_browser_footer_rows(language),
        ]
        return text, InlineKeyboardMarkup(rows), None

    def _read_file_preview_content(self, path: Path, *, language: str) -> str:
        """Read a best-effort text preview while staying inside Telegram limits."""
        preview_budget = TELEGRAM_SAFE_TEXT_LIMIT - max(200, len(str(path)))
        preview_budget = max(800, preview_budget)
        raw_limit = preview_budget * 2
        try:
            raw = path.read_bytes()[:raw_limit]
        except OSError as exc:
            return self._t("files.path_denied", language=language, path=path, error=exc)

        if not raw:
            return self._t("files.file.empty", language=language)
        if b"\x00" in raw:
            return self._t("files.file.binary", language=language)

        content = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        content = content.replace("\x00", "")
        if not content.strip():
            return self._t("files.file.empty", language=language)

        trimmed = content[:preview_budget].rstrip()
        if len(content) > len(trimmed) or path.stat().st_size > len(raw):
            trimmed = f"{trimmed}{self._t('files.file.truncated', language=language)}"
        return trimmed

    def _resolve_file_browser_target(self, data: str, *, prefix: str) -> tuple[Path | None, int]:
        """Resolve a file-browser callback payload into a path and page number."""
        payload = data.removeprefix(prefix)
        token, separator, page_raw = payload.partition(":")
        path = self._file_browser_targets.get(token)
        page = 0
        if separator and page_raw.isdigit():
            page = int(page_raw)
        return path, page

    def _register_file_browser_target(self, path: Path) -> str:
        """Store a short-lived callback token for a filesystem path."""
        if len(self._file_browser_targets) >= FILE_BROWSER_TOKEN_CACHE_LIMIT:
            stale_keys = list(self._file_browser_targets)[: FILE_BROWSER_TOKEN_CACHE_LIMIT // 4]
            for key in stale_keys:
                self._file_browser_targets.pop(key, None)
        token = secrets.token_urlsafe(6)
        self._file_browser_targets[token] = path
        return token

    def _file_browser_footer(self, language: str) -> InlineKeyboardMarkup:
        """Build the common footer keyboard for file browser screens."""
        return InlineKeyboardMarkup(self._file_browser_footer_rows(language))

    def _file_browser_footer_rows(self, language: str) -> list[list[InlineKeyboardButton]]:
        """Build footer rows shared by file browser screens."""
        return [
            [
                InlineKeyboardButton(self._t("keyboard.back_status", language=language), callback_data="view:status"),
                InlineKeyboardButton(self._t("keyboard.help", language=language), callback_data="view:help"),
            ]
        ]

    @staticmethod
    def _trim_button_label(label: str, limit: int = 48) -> str:
        """Keep inline keyboard labels compact and readable."""
        if len(label) <= limit:
            return label
        return f"{label[: limit - 3].rstrip()}..."

    def _build_model_rows(self) -> list[list[InlineKeyboardButton]]:
        """Build buttons for available model options."""
        rows: list[list[InlineKeyboardButton]] = []
        current = self.config.codex_model
        buttons: list[InlineKeyboardButton] = []
        for model_name in self._available_models():
            label = f"{'• ' if model_name == current else ''}{model_name}"
            buttons.append(InlineKeyboardButton(label[:32], callback_data=f"set:model:{model_name}"))
            if len(buttons) == 2:
                rows.append(buttons)
                buttons = []
        if buttons:
            rows.append(buttons)
        return rows

    def _build_reasoning_rows(self) -> list[list[InlineKeyboardButton]]:
        """Build buttons for reasoning effort options."""
        rows: list[list[InlineKeyboardButton]] = []
        current = self.config.codex_reasoning_effort
        buttons: list[InlineKeyboardButton] = []
        for effort in SUPPORTED_REASONING_EFFORTS:
            label = f"{'• ' if effort == current else ''}{effort}"
            buttons.append(InlineKeyboardButton(label, callback_data=f"set:reasoning:{effort}"))
            if len(buttons) == 2:
                rows.append(buttons)
                buttons = []
        if buttons:
            rows.append(buttons)
        return rows

    def _available_models(self) -> list[str]:
        """Read models from the local Codex cache and fall back to common defaults."""
        models: list[str] = []
        if MODEL_CACHE_PATH.exists():
            try:
                payload = json.loads(MODEL_CACHE_PATH.read_text(encoding="utf-8"))
                for item in payload.get("models", []):
                    slug = str(item.get("slug", "")).strip()
                    if slug and slug not in models:
                        models.append(slug)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                LOGGER.exception("Failed to load models from %s", MODEL_CACHE_PATH)

        for fallback in DEFAULT_MODEL_OPTIONS:
            if fallback not in models:
                models.append(fallback)
        if self.config.codex_model not in models:
            models.insert(0, self.config.codex_model)
        return models[:8]

    async def _send_long_message(
        self,
        update: Update,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        """Send text paragraph-by-paragraph within Telegram message length limits."""
        for segment in self._render_reply_segments(text, parse_mode=parse_mode):
            await self._safe_reply(update, segment, parse_mode=parse_mode)

    async def _safe_reply(
        self,
        update: Update,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Reply with a Markdown fallback when Telegram formatting fails."""
        message = update.effective_message
        if message is None:
            return
        text = self._fit_telegram_text(text)
        try:
            await message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except (BadRequest, ValueError):
            await message.reply_text(text, reply_markup=reply_markup)

    async def _safe_reply_message(
        self,
        update: Update,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        allow_plain_fallback: bool = True,
    ):
        """Reply and return the created message when possible."""
        message = update.effective_message
        if message is None:
            return None
        text = self._fit_telegram_text(text)
        try:
            return await message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except (BadRequest, ValueError):
            if not allow_plain_fallback or parse_mode is None:
                return None
            return await message.reply_text(text, reply_markup=reply_markup)

    async def _safe_edit_text(
        self,
        message,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        allow_plain_fallback: bool = True,
    ) -> bool:
        """Edit a message with a formatting fallback."""
        if message is None:
            return False
        text = self._fit_telegram_text(text)
        try:
            await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except (BadRequest, ValueError) as exc:
            if self._is_message_not_modified_error(exc):
                return True
            LOGGER.warning(
                "Telegram edit failed; preserving existing rendered message parse_mode=%s error=%s text=%r",
                parse_mode,
                exc,
                text[:200],
            )
            if not allow_plain_fallback or parse_mode is None:
                return False
            try:
                await message.edit_text(text, reply_markup=reply_markup)
                return True
            except (BadRequest, ValueError) as fallback_exc:
                if self._is_message_not_modified_error(fallback_exc):
                    return True
                LOGGER.warning(
                    "Telegram plain-text fallback edit failed parse_mode=%s error=%s text=%r",
                    parse_mode,
                    fallback_exc,
                    text[:200],
                )
                return False

    @staticmethod
    def _is_message_not_modified_error(exc: Exception) -> bool:
        """Recognize Telegram no-op edit failures to avoid unnecessary fallback edits."""
        return "message is not modified" in str(exc).lower()

    async def _send_startup_restart_notice(self) -> None:
        """Send a restart completion notice from one-shot environment variables."""
        chat_id_raw = os.environ.get(RESTART_NOTIFY_CHAT_ID_ENV, "").strip()
        if not chat_id_raw:
            self._clear_restart_env()
            return

        user_id_raw = os.environ.get(RESTART_NOTIFY_USER_ID_ENV, "").strip()

        try:
            chat_id = int(chat_id_raw)
            user_id = int(user_id_raw) if user_id_raw else chat_id
        except ValueError:
            LOGGER.error(
                "Invalid restart notify env values chat_id=%r user_id=%r",
                chat_id_raw,
                user_id_raw,
            )
            self._clear_restart_env()
            return

        try:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=self._t("restart.completed"),
            )
        except Exception:
            LOGGER.exception("Failed to send startup restart notice chat_id=%s user_id=%s", chat_id, user_id)
            if user_id == chat_id:
                self._clear_restart_env()
                return
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=self._t("restart.completed"),
                )
            except Exception:
                LOGGER.exception("Fallback startup restart notice failed user_id=%s", user_id)
                self._clear_restart_env()
                return

        LOGGER.info("Sent startup restart notice to chat_id=%s user_id=%s", chat_id, user_id)
        self._clear_restart_env()

    @staticmethod
    def _clear_restart_env() -> None:
        """Drop one-shot restart metadata after startup completes."""
        for env_name in (RESTART_NOTIFY_CHAT_ID_ENV, RESTART_NOTIFY_USER_ID_ENV, RESTART_IN_PROGRESS_ENV):
            os.environ.pop(env_name, None)

    async def _typing_heartbeat(
        self,
        chat_id: int,
        stop_event: asyncio.Event,
        state: dict[str, object],
    ) -> None:
        """Keep the most relevant Telegram activity indicator active while Codex is still working."""
        while not stop_event.is_set():
            await self._send_chat_action(chat_id, self._stream_chat_action(state))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=TYPING_HEARTBEAT_SECONDS)
            except TimeoutError:
                continue

    async def _send_chat_action(self, chat_id: int, action: str) -> None:
        """Best-effort Telegram activity indicator for long-running operations and tool updates."""
        try:
            await self.application.bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            LOGGER.exception("Failed to send chat action chat_id=%s action=%s", chat_id, action)

    async def _send_tool_event_message(
        self,
        update: Update,
        state: dict[str, object],
        event: CodexStreamEvent,
        *,
        started: bool,
    ) -> None:
        """Send a compact Telegram message for each tool invocation and rotate the stream segment."""
        language = str(state.get("language") or self.config.default_language)
        command = (event.command or "(unknown command)").strip()
        command_summary = self._summarize_tool_command(command)
        tool_messages = self._tool_messages(state)
        chat = update.effective_chat
        action = self._select_chat_action_for_command(command)
        self._set_stream_chat_action(state, action)
        if chat is not None:
            await self._send_chat_action(chat.id, action)
        if started:
            text = self._render_tool_event_text("🛠", command_summary, language=language)
            sent_message = await self._safe_reply_message(update, text, parse_mode=ParseMode.MARKDOWN)
            if sent_message is not None and event.item_id:
                tool_messages[event.item_id] = sent_message
            self._start_new_stream_segment(state)
            return

        if event.exit_code == 0:
            text = self._render_tool_event_text("✅", command_summary, language=language)
        else:
            failure_reason = self._summarize_tool_failure(
                event.output,
                event.exit_code,
                language=language,
            )
            text = self._render_tool_event_text("❌", command_summary, language=language, detail=failure_reason)
        self._set_stream_chat_action(state, ChatAction.TYPING)

        tool_message = tool_messages.pop(event.item_id, None) if event.item_id else None
        if tool_message is not None:
            edited = await self._safe_edit_text(
                tool_message,
                text,
                parse_mode=ParseMode.MARKDOWN,
                allow_plain_fallback=False,
            )
            if edited:
                self._start_new_stream_segment(state)
                return

        await self._safe_reply_message(update, text, parse_mode=ParseMode.MARKDOWN)
        self._start_new_stream_segment(state)

    async def _update_stream_message(
        self,
        update: Update,
        text: str,
        state: dict[str, object],
        parse_mode: str | None = None,
    ) -> None:
        """Finalize completed paragraph segments and only edit the current tail."""
        now = asyncio.get_running_loop().time()
        state["last_assistant_text"] = text
        language = str(state.get("language") or self.config.default_language)
        segment_text = self._current_stream_segment_text(state, text)
        messages = self._stream_messages(state)
        rendered_chunks = self._stream_rendered_chunks(state)
        finalized_chunks = int(state["finalized_chunks"])

        committed_text, tail_text = self._split_stream_text(segment_text)
        committed_chunks = self._render_reply_segments(committed_text, parse_mode=parse_mode) if committed_text else []

        while finalized_chunks < len(committed_chunks):
            chunk = committed_chunks[finalized_chunks]
            if finalized_chunks < len(messages):
                if finalized_chunks < len(rendered_chunks) and rendered_chunks[finalized_chunks] == chunk:
                    finalized_chunks += 1
                    continue
                edited = await self._safe_edit_text(
                    messages[finalized_chunks],
                    chunk,
                    parse_mode=parse_mode,
                    allow_plain_fallback=False,
                )
                if edited:
                    if finalized_chunks < len(rendered_chunks):
                        rendered_chunks[finalized_chunks] = chunk
                    else:
                        rendered_chunks.append(chunk)
                    finalized_chunks += 1
                    continue
            else:
                sent_message = await self._safe_reply_message(
                    update,
                    chunk,
                    parse_mode=parse_mode,
                    allow_plain_fallback=False,
                )
                if sent_message is not None:
                    messages.append(sent_message)
                    rendered_chunks.append(chunk)
                    finalized_chunks += 1
                    continue
                LOGGER.warning("Telegram reply failed for committed markdown chunk text=%r", chunk[:200])
                break
            break

        state["finalized_chunks"] = finalized_chunks

        if not tail_text:
            state["last_preview"] = ""
            state["last_sent_at"] = now
            return

        preview = self._build_stream_preview(tail_text, language=language, parse_mode=parse_mode)
        previous_preview = str(state["last_preview"])
        previous_content = previous_preview.removesuffix(self._t("stream.preview_suffix", language=language))
        if preview == previous_preview:
            return
        if (
            now - float(state["last_sent_at"]) < 1.0
            and previous_content
            and preview.startswith(previous_content)
            and len(preview) - len(previous_preview) < 80
        ):
            return

        if finalized_chunks < len(messages):
            if finalized_chunks < len(rendered_chunks) and rendered_chunks[finalized_chunks] == preview:
                state["last_preview"] = preview
                state["last_sent_at"] = now
                return
            edited = await self._safe_edit_text(
                messages[finalized_chunks],
                preview,
                parse_mode=parse_mode,
                allow_plain_fallback=False,
            )
            if edited:
                if finalized_chunks < len(rendered_chunks):
                    rendered_chunks[finalized_chunks] = preview
                else:
                    rendered_chunks.append(preview)
                state["last_preview"] = preview
                state["last_sent_at"] = now
                return
        else:
            sent_message = await self._safe_reply_message(
                update,
                preview,
                parse_mode=parse_mode,
                allow_plain_fallback=False,
            )
            if sent_message is not None:
                messages.append(sent_message)
                rendered_chunks.append(preview)
                state["last_preview"] = preview
                state["last_sent_at"] = now
                return
            LOGGER.warning("Telegram reply failed for preview markdown chunk text=%r", preview[:200])
            return
        LOGGER.warning("Telegram edit failed for preview markdown chunk text=%r", preview[:200])

    async def _finalize_stream_message(
        self,
        update: Update,
        state: dict[str, object],
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        """Finalize the streamed reply using the already-sent messages when possible."""
        state["last_assistant_text"] = text
        segment_text = self._current_stream_segment_text(state, text)
        if not segment_text:
            state["finalized_chunks"] = 0
            state["last_preview"] = ""
            return

        chunks = self._render_reply_segments(segment_text, parse_mode=parse_mode)
        messages = self._stream_messages(state)
        rendered_chunks = self._stream_rendered_chunks(state)
        if not messages:
            await self._send_long_message(update, segment_text, parse_mode=parse_mode)
            return

        finalized_count = 0
        for index, chunk in enumerate(chunks):
            if index < len(messages):
                if index < len(rendered_chunks) and rendered_chunks[index] == chunk:
                    finalized_count += 1
                    continue
                edited = await self._safe_edit_text(
                    messages[index],
                    chunk,
                    parse_mode=parse_mode,
                    allow_plain_fallback=False,
                )
                if edited:
                    if index < len(rendered_chunks):
                        rendered_chunks[index] = chunk
                    else:
                        rendered_chunks.append(chunk)
                    finalized_count += 1
                    continue
                LOGGER.warning("Telegram edit failed for finalized markdown chunk index=%s text=%r", index, chunk[:200])
                break
            else:
                sent_message = await self._safe_reply_message(
                    update,
                    chunk,
                    parse_mode=parse_mode,
                    allow_plain_fallback=False,
                )
                if sent_message is not None:
                    messages.append(sent_message)
                    rendered_chunks.append(chunk)
                    finalized_count += 1
                    continue
                LOGGER.warning("Telegram reply failed for finalized markdown chunk index=%s text=%r", index, chunk[:200])
                break

        state["finalized_chunks"] = finalized_count
        if finalized_count == len(chunks):
            state["last_preview"] = ""

    @staticmethod
    def _split_stream_text(text: str) -> tuple[str, str]:
        """Split streamed text at the last completed paragraph boundary outside code fences."""
        if not text:
            return "", ""

        lines = text.splitlines(keepends=True)
        if not lines:
            return "", text

        in_fence = False
        offset = 0
        last_boundary = -1

        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("```"):
                in_fence = not in_fence

            offset += len(line)
            if in_fence:
                continue
            if stripped.strip():
                continue
            last_boundary = offset

        if last_boundary < 0:
            return "", text

        committed = text[:last_boundary].rstrip("\n")
        tail = text[last_boundary:].lstrip("\n")
        return committed, tail

    @staticmethod
    def _stream_messages(state: dict[str, object]) -> list:
        """Return the mutable list of streamed Telegram messages."""
        messages = state.get("messages")
        if isinstance(messages, list):
            return messages
        messages = []
        state["messages"] = messages
        return messages

    @staticmethod
    def _stream_rendered_chunks(state: dict[str, object]) -> list[str]:
        """Return the mutable list of last rendered raw texts for streamed Telegram messages."""
        rendered_chunks = state.get("rendered_chunks")
        if isinstance(rendered_chunks, list):
            return rendered_chunks
        rendered_chunks = []
        state["rendered_chunks"] = rendered_chunks
        return rendered_chunks

    @staticmethod
    def _tool_messages(state: dict[str, object]) -> dict[str, object]:
        """Return the mutable map of pending tool-status Telegram messages keyed by tool item id."""
        tool_messages = state.get("tool_messages")
        if isinstance(tool_messages, dict):
            return tool_messages
        tool_messages = {}
        state["tool_messages"] = tool_messages
        return tool_messages

    @staticmethod
    def _stream_chat_action(state: dict[str, object]) -> str:
        """Return the currently selected Telegram activity indicator."""
        action = state.get("chat_action")
        return action if isinstance(action, str) and action else DEFAULT_CHAT_ACTION

    @staticmethod
    def _set_stream_chat_action(state: dict[str, object], action: str) -> None:
        """Update the active Telegram activity indicator for the current request."""
        state["chat_action"] = action or DEFAULT_CHAT_ACTION

    @staticmethod
    def _active_stream_message(state: dict[str, object]):
        """Return the current editable stream message, if one exists."""
        messages = TelegramCodexBot._stream_messages(state)
        finalized_chunks = int(state.get("finalized_chunks", 0))
        if finalized_chunks < len(messages):
            return messages[finalized_chunks]
        return None

    @staticmethod
    def _current_stream_segment_text(state: dict[str, object], text: str) -> str:
        """Return only the assistant text for the current segment after the latest tool boundary."""
        offset = int(state.get("assistant_offset", 0))
        if offset <= 0:
            return text
        if offset >= len(text):
            return ""
        return text[offset:]

    @staticmethod
    def _start_new_stream_segment(state: dict[str, object]) -> None:
        """Freeze current assistant messages so later stream updates always use new Telegram messages."""
        last_text = str(state.get("last_assistant_text", ""))
        state["assistant_offset"] = len(last_text)
        state["messages"] = []
        state["rendered_chunks"] = []
        state["finalized_chunks"] = 0
        state["last_preview"] = ""
        state["last_sent_at"] = 0.0
        state["chat_action"] = DEFAULT_CHAT_ACTION

    def _build_completion_text(
        self,
        elapsed_seconds: float,
        usage: CodexUsage | None,
        *,
        language: str,
    ) -> str:
        """Build the completion notice shown after a Codex response finishes."""
        summary = self._t(
            "completion.summary",
            language=language,
            elapsed=TelegramCodexBot._format_elapsed(elapsed_seconds),
        )
        if usage is None or not usage.has_values():
            return self._t("completion.token_unavailable", language=language, summary=summary)

        token_parts: list[str] = []
        if usage.total_tokens is not None:
            token_parts.append(self._t("completion.total_tokens", language=language, count=f"{usage.total_tokens:,}"))
        if usage.input_tokens is not None:
            token_parts.append(self._t("completion.input_tokens", language=language, count=f"{usage.input_tokens:,}"))
        if usage.output_tokens is not None:
            token_parts.append(self._t("completion.output_tokens", language=language, count=f"{usage.output_tokens:,}"))
        if not token_parts:
            return self._t("completion.token_unavailable", language=language, summary=summary)
        token_joiner = "，" if language == "zh" else ", "
        return self._t("completion.with_tokens", language=language, summary=summary, tokens=token_joiner.join(token_parts))

    def _build_change_summary_text(self, changes: list[FileChange], *, language: str) -> str:
        """Build a compact file-change summary for the current Codex run."""
        lines = [self._t("completion.changes_title", language=language)]
        visible_changes = changes[:MAX_COMPLETION_CHANGE_FILES]
        for change in visible_changes:
            detail = self._format_file_change_detail(change, language=language)
            lines.append(f"- {change.path} ({detail})")
        remaining = len(changes) - len(visible_changes)
        if remaining > 0:
            lines.append(self._t("completion.changes_more", language=language, count=remaining))
        return "\n".join(lines)

    def _format_file_change_detail(self, change: FileChange, *, language: str) -> str:
        """Render per-file line deltas when text content is available."""
        if change.added_lines is None or change.removed_lines is None:
            return self._t("completion.changes_binary", language=language)
        return f"+{change.added_lines}/-{change.removed_lines}"

    def _capture_workspace_snapshot(self) -> WorkspaceSnapshot | None:
        """Capture a best-effort view of the current project tree."""
        try:
            return WorkspaceSnapshot.capture(self.project_path)
        except Exception:
            LOGGER.exception("Workspace snapshot capture failed for %s", self.project_path)
            return None

    def _workspace_changes_since(self, before_snapshot: WorkspaceSnapshot | None) -> list[FileChange]:
        """Return file changes relative to an earlier snapshot without failing the request."""
        if before_snapshot is None:
            return []
        try:
            return before_snapshot.diff(WorkspaceSnapshot.capture(self.project_path))
        except Exception:
            LOGGER.exception("Workspace snapshot diff failed for %s", self.project_path)
            return []

    @staticmethod
    def _format_elapsed(elapsed_seconds: float) -> str:
        """Render a compact elapsed-time label for the Telegram completion notice."""
        if elapsed_seconds < 1:
            return f"{elapsed_seconds * 1000:.0f}ms"
        if elapsed_seconds < 60:
            return f"{elapsed_seconds:.1f}s"

        minutes, seconds = divmod(elapsed_seconds, 60)
        if minutes < 60:
            return f"{int(minutes)}m {seconds:.1f}s"

        hours, minutes = divmod(minutes, 60)
        return f"{int(hours)}h {int(minutes)}m {seconds:.0f}s"

    @staticmethod
    def _chunk_text(
        text: str,
        limit: int = TELEGRAM_MESSAGE_LIMIT,
        parse_mode: str | None = None,
    ) -> list[str]:
        """Split long text into Telegram-safe chunks."""
        limit = min(limit, TELEGRAM_SAFE_TEXT_LIMIT)
        if len(text) <= limit:
            return [text]
        if parse_mode == ParseMode.MARKDOWN:
            return TelegramCodexBot._chunk_markdown_text(text, limit)

        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return chunks

    @staticmethod
    def _chunk_markdown_text(text: str, limit: int) -> list[str]:
        """Split Markdown text without leaving fenced code blocks unbalanced."""
        lines = text.splitlines(keepends=True)
        if not lines:
            return [text]

        chunks: list[str] = []
        current = ""
        fence_lang = ""
        in_fence = False

        def normalized(candidate: str) -> str:
            return candidate.rstrip("\n")

        def fence_prefix() -> str:
            return f"```{fence_lang}\n" if in_fence else ""

        def fence_suffix(candidate: str) -> str:
            if not in_fence:
                return ""
            if candidate.endswith("\n"):
                return "```"
            return "\n```"

        def flush() -> None:
            nonlocal current
            candidate = current
            suffix = fence_suffix(candidate) if in_fence else ""
            overflow = len(candidate) + len(suffix) - limit
            if overflow > 0:
                candidate = candidate[:-overflow]
            if in_fence:
                candidate = f"{candidate}{suffix}"
            candidate = normalized(candidate)
            if candidate:
                chunks.append(candidate)
            current = fence_prefix()

        def append_piece(piece: str) -> None:
            nonlocal current
            while piece:
                suffix = fence_suffix(current + piece)
                available = limit - len(current) - len(suffix)
                if available <= 0:
                    flush()
                    continue
                if len(piece) <= available:
                    current += piece
                    piece = ""
                    continue

                split_at = piece.rfind("\n", 0, available)
                if split_at > 0:
                    take = split_at + 1
                else:
                    split_at = piece.rfind(" ", 0, available)
                    take = split_at if split_at > 0 else available
                current += piece[:take]
                piece = piece[take:]
                flush()
                piece = piece.lstrip() if not in_fence else piece

        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("```"):
                if not in_fence:
                    fence_lang = stripped[3:].strip()
                    in_fence = True
                else:
                    in_fence = False
                    fence_lang = ""
            append_piece(line)

        if current.strip():
            chunks.append(normalized(current))

        return chunks or [text[:limit].rstrip()]

    @staticmethod
    def _render_reply_segments(
        text: str,
        parse_mode: str | None = None,
    ) -> list[str]:
        """Split a reply into paragraph-first Telegram messages."""
        if parse_mode == ParseMode.MARKDOWN:
            formatted_review = TelegramCodexBot._format_review_reply(text)
            if formatted_review != text and len(formatted_review) <= TELEGRAM_SAFE_TEXT_LIMIT:
                return [formatted_review]
            text = formatted_review

        paragraphs = TelegramCodexBot._split_paragraphs(text)
        if not paragraphs:
            return [text] if text else []

        segments: list[str] = []
        for paragraph in paragraphs:
            segments.extend(TelegramCodexBot._chunk_text(paragraph, parse_mode=parse_mode))
        return segments

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """Split text on blank lines while preserving fenced code blocks."""
        if not text:
            return []

        lines = text.splitlines(keepends=True)
        paragraphs: list[str] = []
        current: list[str] = []
        in_fence = False

        def flush() -> None:
            paragraph = "".join(current).strip()
            if paragraph:
                paragraphs.append(paragraph)
            current.clear()

        for line in lines:
            stripped = line.lstrip()
            is_fence = stripped.startswith("```")
            is_blank = not stripped.strip()

            if is_blank and not in_fence:
                flush()
                continue

            current.append(line)

            if is_fence:
                in_fence = not in_fence

        flush()
        return paragraphs

    @classmethod
    def _format_review_reply(cls, text: str) -> str:
        """Reformat review-style Findings/Open Questions/Summary output for Telegram Markdown."""
        if not text.strip():
            return text

        sections: list[tuple[str, list[str]]] = []
        current_name: str | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_name, current_lines
            if current_name is None:
                return
            content = "\n".join(current_lines).strip()
            if content:
                sections.append((current_name, content.splitlines()))
            current_name = None
            current_lines = []

        for raw_line in text.splitlines():
            heading = cls._match_review_heading(raw_line)
            if heading is not None:
                flush()
                current_name = heading
                continue
            if current_name is None:
                continue
            current_lines.append(raw_line)

        flush()
        if len(sections) < 2:
            return text

        formatted_sections: list[str] = []
        for section_name, section_lines in sections:
            formatted_body = cls._format_review_section_lines(section_lines)
            if not formatted_body:
                continue
            title = section_name.title() if section_name != "open-questions" else "Open Questions"
            formatted_sections.append(f"*{title}*\n{formatted_body}")

        return "\n\n".join(formatted_sections) if formatted_sections else text

    @staticmethod
    def _match_review_heading(line: str) -> str | None:
        """Recognize common review section headings emitted by Codex-style reviews."""
        normalized = line.strip()
        if not normalized:
            return None

        while normalized.startswith("#"):
            normalized = normalized[1:].strip()
        if normalized.startswith("**") and normalized.endswith("**") and len(normalized) > 4:
            normalized = normalized[2:-2].strip()
        if normalized.startswith("*") and normalized.endswith("*") and len(normalized) > 2:
            normalized = normalized[1:-1].strip()
        normalized = normalized.rstrip(":").strip().lower()
        normalized = " ".join(normalized.split())
        normalized = normalized.replace(" ", "-")

        if normalized in {"findings", "open-questions", "summary"}:
            return normalized
        return None

    @classmethod
    def _format_review_section_lines(cls, lines: list[str]) -> str:
        """Render section content as compact Telegram-friendly bullets."""
        blocks: list[str] = []
        current: list[str] = []

        def flush() -> None:
            paragraph = " ".join(part.strip() for part in current if part.strip()).strip()
            if paragraph:
                blocks.append(paragraph)
            current.clear()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                flush()
                continue
            if cls._looks_like_list_item(stripped):
                flush()
                blocks.append(stripped)
                continue
            if stripped.startswith(("```", ">")):
                flush()
                blocks.append(stripped)
                continue
            current.append(stripped)
        flush()

        rendered: list[str] = []
        for block in blocks:
            bullet_text = cls._strip_list_marker(block)
            if bullet_text.startswith("```"):
                rendered.append(bullet_text)
                continue
            rendered.append(f"- {cls._escape_markdown_text(bullet_text)}")
        return "\n".join(rendered)

    @staticmethod
    def _looks_like_list_item(text: str) -> bool:
        """Recognize simple bullet/numbered list items."""
        if text.startswith(("- ", "* ", "+ ")):
            return True
        if not text or not text[0].isdigit():
            return False

        index = 0
        while index < len(text) and text[index].isdigit():
            index += 1
        return index < len(text) and text[index:index + 2] == ". "

    @staticmethod
    def _strip_list_marker(text: str) -> str:
        """Remove common list prefixes so section bullets stay visually consistent."""
        stripped = text.lstrip()
        for prefix in ("- ", "* ", "+ "):
            if stripped.startswith(prefix):
                return stripped[len(prefix):].strip()

        if len(stripped) > 2 and stripped[0].isdigit():
            index = 0
            while index < len(stripped) and stripped[index].isdigit():
                index += 1
            if index < len(stripped) and stripped[index:index + 2] == ". ":
                return stripped[index + 2 :].strip()
        return stripped

    @staticmethod
    def _escape_markdown_text(text: str) -> str:
        """Escape Telegram legacy Markdown control characters in plain review content."""
        escaped = text.replace("\\", "\\\\")
        for char in ("`", "*", "_", "["):
            escaped = escaped.replace(char, f"\\{char}")
        return escaped

    def _build_stream_preview(self, text: str, language: str, parse_mode: str | None = None) -> str:
        """Create a single preview message safe for Telegram edits."""
        preview_suffix = self._t("stream.preview_suffix", language=language)
        preview_limit = TELEGRAM_SAFE_TEXT_LIMIT - len(preview_suffix)
        chunks = TelegramCodexBot._chunk_text(text, limit=preview_limit, parse_mode=parse_mode)
        preview = chunks[0]
        if len(chunks) > 1 or len(text) > len(preview):
            preview = preview.rstrip()
            if preview:
                preview = f"{preview}{preview_suffix}"
            else:
                preview = preview_suffix.strip()
        return preview

    @staticmethod
    def _fit_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> str:
        """Force a single Telegram message below the max length."""
        limit = min(limit, TELEGRAM_SAFE_TEXT_LIMIT)
        if len(text) <= limit:
            return text
        return text[:limit].rstrip()

    @staticmethod
    def _markdown_code(text: str, limit: int = 160) -> str:
        """Render compact inline code that stays safe for Telegram Markdown."""
        normalized = " ".join(text.split()).replace("`", "'").strip()
        if not normalized:
            normalized = "(empty)"
        if len(normalized) > limit:
            normalized = f"{normalized[:limit].rstrip()}..."
        return f"`{normalized}`"

    def _render_tool_event_text(
        self,
        emoji: str,
        command_summary: str,
        language: str,
        detail: str | None = None,
    ) -> str:
        """Render a compact Telegram Markdown status message for tool updates."""
        lines = [f"{emoji} {self._markdown_code(command_summary, limit=140)}"]
        if detail:
            lines.append(
                self._t(
                    "tool.reason",
                    language=language,
                    detail=self._markdown_code(detail, limit=120),
                )
            )
        return self._fit_telegram_text("\n".join(lines), 1000)

    @staticmethod
    def _summarize_tool_command(command: str, limit: int = 160) -> str:
        """Render a short one-line summary for a tool command."""
        normalized = " ".join(command.split())
        if not normalized:
            return "(unknown command)"
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[:limit].rstrip()}..."

    def _summarize_tool_failure(
        self,
        output: str | None,
        exit_code: int | None,
        *,
        language: str,
        limit: int = 180,
    ) -> str:
        """Extract a short failure reason from tool output."""
        if output:
            for line in output.splitlines():
                candidate = " ".join(line.split())
                if candidate:
                    if len(candidate) <= limit:
                        return candidate
                    return f"{candidate[:limit].rstrip()}..."
        if exit_code is not None:
            return self._t("tool.exit_code", language=language, exit_code=exit_code)
        return self._t("tool.unknown_error", language=language)

    @staticmethod
    def _select_chat_action_for_command(command: str) -> str:
        """Infer the most appropriate Telegram activity indicator from a tool command."""
        normalized = f" {command.lower()} "

        if TelegramCodexBot._command_mentions_any(
            normalized,
            (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".gif",
                ".svg",
                " screenshot",
                " image",
                " photo",
                " convert",
                " magick ",
                " ffmpeg -i",
            ),
        ):
            return ChatAction.UPLOAD_PHOTO

        if TelegramCodexBot._command_mentions_any(
            normalized,
            (
                ".mp4",
                ".mov",
                ".mkv",
                ".avi",
                ".webm",
                " video",
            ),
        ):
            return ChatAction.UPLOAD_VIDEO

        if TelegramCodexBot._command_mentions_any(
            normalized,
            (
                ".mp3",
                ".wav",
                ".m4a",
                ".ogg",
                ".flac",
                " audio",
                " voice",
                " whisper",
            ),
        ):
            return ChatAction.UPLOAD_VOICE

        return ChatAction.UPLOAD_DOCUMENT

    @staticmethod
    def _command_mentions_any(command: str, needles: tuple[str, ...]) -> bool:
        """Check whether a normalized command contains any of the provided markers."""
        return any(needle in command for needle in needles)
