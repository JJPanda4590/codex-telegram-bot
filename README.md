[中文](./README.zh-CN.md) | English

# Codex Telegram Command Center

Version: `1.0.0`

A production-oriented Telegram bot that forwards messages from authorized Telegram users to the local `codex` CLI and sends the response back to Telegram.

Brief description:
Control the Codex running on your computer anytime through Telegram chat, with support for multi-session workflows plus rich in-chat commands for model selection and runtime adjustments.

简要描述：
通过 Telegram 聊天随时控制电脑上的 Codex，同时支持多会话协作，以及模型切换、运行参数调整等丰富指令。

## Highlights

- Authorized access only: every update is checked against a whitelist.
- Persistent multi-session chat: each Telegram user can create, switch, reset, and keep multiple sessions.
- Local Codex integration: prompts are executed by the local `codex` CLI, not a remote custom wrapper.
- Stream-friendly replies: long outputs are streamed and split safely for Telegram message limits.
- Runtime controls from Telegram: inspect status, list files, switch project path, stop tasks, and view usage.
- Bilingual UI support: default language plus per-user language switching.
- JSON-backed persistence: session state survives process restarts through `sessions.json`.

This bot has already reached a small but very real self-bootstrapping stage: a good portion of its features were planned, refined, and implemented by directly chatting with the bot itself during development. In practice, that means it is not just a wrapper around Codex, but also one of the tools used to keep building itself.

## How It Works

```text
Telegram -> python-telegram-bot -> SessionStore -> local Codex CLI -> Telegram
```

## Apply For Your Telegram Bot

### 1. Create a bot with BotFather

In Telegram:

1. Open `@BotFather`
2. Run `/newbot`
3. Set a bot name and username
4. Copy the bot token

Put the token into `settings/bot_config.json`:

```json
{
  "telegram_bot_token": "123456:your-bot-token"
}
```

### 2. Get your Telegram user ID

The whitelist requires numeric Telegram user IDs.

Common ways:

- Message `@userinfobot`
- Use any trusted Telegram ID bot
- If you already know your numeric user ID, add it directly

Then update:

```json
{
  "whitelist": [123456789]
}
```

Only users listed in `whitelist` can use the bot.

## Configuration

Main config file:

- `settings/bot_config.json`

Quick start:

1. Copy `settings/bot_config.example.json` to `settings/bot_config.json`
2. Fill in the Telegram token
3. Add your Telegram user ID to `whitelist`
4. Adjust the rest if needed

Example:

```json
{
  "telegram_bot_token": "",
  "whitelist": [123456789],
  "codex_cli_fallback_paths": [],
  "codex_model": "gpt-4.1-mini",
  "codex_reasoning_effort": "medium",
  "openai_admin_api_key": "",
  "openai_organization_id": "",
  "openai_project_id": "",
  "session_store_path": "sessions.json",
  "request_timeout_seconds": 28800,
  "project_path": ".",
  "log_level": "INFO",
  "default_language": "zh",
  "translations_path": "settings/i18n.json"
}
```

Important fields:

- `telegram_bot_token`: BotFather token, required.
- `whitelist`: allowed Telegram user IDs, required.
- `codex_cli_path`: optional. Defaults to `codex`, so you can omit it when `codex` is already available globally.
- `codex_cli_fallback_paths`: optional fallback absolute paths checked when `codex_cli_path` is not directly executable or not found in `PATH`.
- `codex_model`: model passed to the Codex CLI.
- `codex_reasoning_effort`: one of `low`, `medium`, `high`, `xhigh`.
- `project_path`: working directory used by `/project` and `/files`.
- `session_store_path`: JSON file used to persist chat sessions.
- `default_language`: default bot UI language.
- `translations_path`: i18n file path.
- `openai_admin_api_key`: optional, enables `/usage`.

## Requirements

- Python 3.10+
- Local Codex CLI available as `codex` in `PATH`, as a login-shell alias/function named `codex`, or configured via `codex_cli_path` / `codex_cli_fallback_paths`
- A Telegram bot token

If `codex` is already available globally, you can omit `codex_cli_path` entirely because the default is `codex`.

If `codex` only exists as a shell alias or shell function, the bot will also try to launch it through your login shell automatically.

If your Codex installation is not in `PATH`, add its absolute binary path to `codex_cli_fallback_paths`. Example:

```json
{
  "codex_cli_fallback_paths": [
    "/Applications/Codex.app/Contents/Resources/codex"
  ]
}
```

Python dependency:

```bash
pip install -r requirements.txt
```

## Run The Project

### Option 1. Standard manual run

```bash
pip install -r requirements.txt
python3 main.py
```

### Option 2. Helper scripts

Set up environment:

```bash
./setup_env.sh
```

Run the bot:

```bash
./run_bot.sh
```

### Quick syntax check

```bash
python3 -m compileall .
```

## Useful Bot Commands

- `/help`: show help and quick actions
- `/status`: show runtime, session, model, and project status
- `/session_new`: create a new session
- `/session_list`: list your sessions
- `/session_details`: show detailed session information
- `/session_switch <id>`: switch active session
- `/session_reset`: clear the current session history
- `/project [path]`: show or switch the active project directory
- `/files`: list files in the configured project path
- `/usage`: show OpenAI usage summary when admin API credentials are configured
- `/stop`: stop running Codex tasks
- `/restart`: request a bot restart from Telegram
- `/clear_sessions`: clear persisted sessions

## Project Structure

- `main.py`: application entrypoint
- `settings/bot_config.json`: runtime config
- `settings/bot_config.example.json`: config template
- `tgboter/config.py`: config loading and validation
- `tgboter/session_store.py`: session persistence
- `tgboter/codex_client.py`: local Codex CLI wrapper
- `tgboter/telegram_bot.py`: Telegram handlers and reply flow

## Notes

- Telegram messages have a size limit; the bot splits long replies safely.
- If Markdown formatting is rejected by Telegram, the bot falls back gracefully.
- Session data is stored locally in JSON by default.
