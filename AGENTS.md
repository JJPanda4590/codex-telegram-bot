# AGENTS.md

## Highest Priority Rule

- Codex must never restart, reload, stop-and-start, or otherwise cycle this project or its bot process.
- Only the user is allowed to restart the project.
- If a restart is required for any reason, Codex must notify the user and wait for the user to perform the restart.

## Project Overview

This repository contains a production-oriented Python Telegram bot that forwards authorized Telegram user messages to the local Codex CLI.

Message flow:

`Telegram -> python-telegram-bot -> SessionStore -> local Codex CLI -> Telegram`

Default behavior:
- The bot invokes the local `codex` CLI directly
- Each Telegram session is mapped to a persisted Codex thread id
- Only users in the whitelist are allowed to use the bot

## Key Files

- `main.py`: application entrypoint
- `settings/bot_config.json`: main runtime configuration, including Telegram token and whitelist
- `settings/bot_config.example.json`: config template
- `tgboter/config.py`: config loading and validation
- `tgboter/session_store.py`: user/session management and JSON persistence
- `tgboter/codex_client.py`: local Codex CLI wrapper
- `tgboter/telegram_bot.py`: Telegram command handlers, auth, forwarding, streaming, chunked replies
- `requirements.txt`: Python dependencies

## Runtime Commands

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the bot:

```bash
python3 main.py
```

Quick syntax check:

```bash
python3 -m compileall .
```

## Config Notes

Primary config location:

- `settings/bot_config.json`

Important fields:
- `telegram_bot_token`: required
- `whitelist`: required list of Telegram user IDs
- `codex_cli_path`: optional; defaults to `codex`
- `codex_model`: model name passed to the Codex CLI
- `project_path`: directory used by `/ls`

## Behavioral Constraints

- Do not restart the project or bot process under any circumstance; notify the user if a restart is needed
- Authorization is enforced on every incoming update
- Unauthorized access must log `[SECURITY] Unauthorized access attempt: user_id=...`
- Telegram replies must handle the 4096-character message limit
- Bot supports multiple sessions per Telegram user
- Session data is persisted to JSON via `session_store_path`
- Markdown output should gracefully fall back to plain text if Telegram rejects formatting

## Change Guidance

When modifying this project:
- Never restart the running project yourself; only notify the user if a restart is needed
- Keep the code modular; avoid collapsing everything into `main.py`
- Preserve whitelist enforcement in `TelegramCodexBot._authorize`
- Preserve per-user multi-session structure in `SessionStore`
- Keep local Codex defaults intact unless explicitly changing deployment assumptions
- Prefer small, testable changes
- If changing command behavior, update both handlers and Telegram command registration

## Safe Extension Ideas

Good next changes:
- Add README deployment instructions
- Add Docker/systemd files
- Replace JSON session persistence with SQLite
- Add unit tests for config validation, auth checks, and session switching
- Add health checks for the configured Codex endpoint

## Assumptions

This project assumes the local Codex desktop/CLI is installed and available as `codex` in `PATH`.
