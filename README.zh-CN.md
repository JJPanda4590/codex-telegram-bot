MIT License

中文 | [English](./README.md)

# Codex Telegram Command Center

版本：`1.0.1`

这是一个偏生产可用的 Telegram 机器人项目：把白名单中的 Telegram 用户消息转发给本地 `codex` CLI，再把结果回传到 Telegram。

简要描述：
一个面向生产环境的高可用 Telegram + Codex 桥接机器人，主打安全鉴权、多会话持久化、本地线程延续，以及可直接在聊天中完成高质量命令协作。

Brief description:
An elegant production-ready Telegram bridge for Codex, designed for secure multi-session collaboration, persistent local threads, and smooth command execution directly from chat.

## Amazing 功能

- 白名单鉴权：每一条更新都会校验用户是否在允许列表中。
- 多会话持久化：每个 Telegram 用户都可以创建、切换、重置多个会话，并持久保存。
- 本地 Codex 驱动：直接调用本地 `codex` CLI，而不是额外套一层远程服务。
- 流式与长消息友好：回复过程支持流式更新，并兼容 Telegram 消息长度限制。
- 机器人内运行控制：可直接在 Telegram 中查看状态、切换项目目录、列文件、停止任务、查看用量。
- 双语界面：支持默认语言配置，也支持用户侧切换语言。
- JSON 存储：会话默认保存在 `sessions.json`，进程重启后仍可恢复。

这个机器人现在已经有一点“自举”味道了：项目里不少功能，本身就是开发过程中直接通过和它聊天，一边提需求、一边调试、一边迭代做出来的。换句话说，它不只是接入了 Codex，也已经开始参与把自己继续开发下去。

## 工作流程

```text
Telegram -> python-telegram-bot -> SessionStore -> local Codex CLI -> Telegram
```

## 如何申请 TG 机器人

### 1. 通过 BotFather 创建机器人

在 Telegram 中：

1. 打开 `@BotFather`
2. 发送 `/newbot`
3. 按提示设置机器人名称和用户名
4. 复制生成的 Bot Token

然后写入 `settings/bot_config.json`：

```json
{
  "telegram_bot_token": "123456:your-bot-token"
}
```

### 2. 获取你的 Telegram 用户 ID

项目白名单需要填写数字类型的 Telegram 用户 ID。

常见方式：

- 给 `@userinfobot` 发消息
- 使用任意可信的 Telegram ID 查询机器人
- 如果你本来就知道自己的数字 ID，直接填入即可

然后修改：

```json
{
  "whitelist": [123456789]
}
```

只有在 `whitelist` 中的用户才能使用该机器人。

## 如何修改配置

主配置文件：

- `settings/bot_config.json`

建议流程：

1. 复制 `settings/bot_config.example.json` 为 `settings/bot_config.json`
2. 填入 `telegram_bot_token`
3. 把你的 Telegram 用户 ID 加入 `whitelist`
4. 按需调整其余配置

配置示例：

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
  "stream_update_min_interval_seconds": 0.35,
  "stream_update_min_chars": 24,
  "project_path": ".",
  "log_level": "INFO",
  "default_language": "zh",
  "translations_path": "settings/i18n.json"
}
```

关键字段说明：

- `telegram_bot_token`：BotFather 生成的 Token，必填。
- `whitelist`：允许访问机器人的 Telegram 用户 ID 列表，必填。
- `codex_cli_path`：可选，默认就是 `codex`。如果系统里已经全局可用，可以不填。
- `codex_cli_fallback_paths`：可选的备用绝对路径列表；当 `codex_cli_path` 不是可执行文件且 `PATH` 中也找不到时，会按顺序尝试这里的路径。
- `codex_model`：传给 Codex CLI 的模型名。
- `codex_reasoning_effort`：可选 `low`、`medium`、`high`、`xhigh`。
- `project_path`：`/project` 和 `/files` 使用的工作目录。
- `session_store_path`：会话 JSON 持久化文件路径。
- `stream_update_min_interval_seconds`：流式预览编辑的最小间隔。值越小，显示越顺滑，但 Telegram 消息编辑频率也越高。
- `stream_update_min_chars`：在达到最小间隔前，预览新增多少字符后才允许跳过节流。
- `default_language`：机器人默认界面语言。
- `translations_path`：国际化文案文件路径。
- `openai_admin_api_key`：可选，配置后可启用 `/usage`。

## 运行要求

- Python 3.10+
- 本地已安装 Codex CLI，并满足以下任一条件：`PATH` 中可找到 `codex`、登录 shell 中存在名为 `codex` 的 alias/function，或已通过 `codex_cli_path` / `codex_cli_fallback_paths` 显式配置
- 已申请 Telegram Bot Token

如果 `codex` 已经是全局命令，可以完全不写 `codex_cli_path`，因为默认值就是 `codex`。

如果 `codex` 只是 shell 里的 alias 或 function，机器人也会自动尝试通过登录 shell 调起。

如果你的 Codex 安装位置不在 `PATH` 里，可以把绝对路径写到 `codex_cli_fallback_paths`。例如：

```json
{
  "codex_cli_fallback_paths": [
    "/Applications/Codex.app/Contents/Resources/codex"
  ]
}
```

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

## 如何运行项目

### 方式一：标准启动

```bash
pip install -r requirements.txt
python3 main.py
```

### 方式二：使用辅助脚本

初始化环境：

```bash
./setup_env.sh
```

启动机器人：

```bash
./run_bot.sh
```

### 快速语法检查

```bash
python3 -m compileall .
```

## 常用机器人命令

- `/help`：查看帮助和快捷操作
- `/status`：查看运行状态、会话状态、模型和项目目录
- `/session_new`：新建会话
- `/session_list`：列出会话
- `/session_details`：查看详细会话信息
- `/session_switch <id>`：切换当前会话
- `/session_reset`：清空当前会话历史
- `/project [path]`：打开按钮式项目选择器；如果带路径参数则直接切换
- `/files`：列出当前项目目录文件
- `/usage`：在配置了 OpenAI 管理接口凭据时查看用量
- `/stop`：停止当前运行中的 Codex 任务
- `/restart`：从 Telegram 请求重启机器人
- `/clear_sessions`：清空持久化会话

## 项目结构

- `main.py`：程序入口
- `settings/bot_config.json`：运行配置
- `settings/bot_config.example.json`：配置模板
- `tgboter/config.py`：配置加载与校验
- `tgboter/session_store.py`：会话持久化
- `tgboter/codex_client.py`：本地 Codex CLI 封装
- `tgboter/telegram_bot.py`：Telegram 指令处理与消息转发

## 补充说明

- Telegram 单条消息长度有限，机器人会自动安全分片。
- 如果 Markdown 被 Telegram 拒绝，程序会自动降级为普通文本回复。
- 默认会话数据存储在本地 JSON 文件中。

power by codex pro
