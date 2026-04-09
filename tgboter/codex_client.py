from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

from tgboter.config import Config

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CodexResult:
    """Result payload returned by the local Codex CLI."""

    text: str
    backend_session_id: str | None = None
    usage: CodexUsage | None = None


@dataclass(slots=True)
class CodexUsage:
    """Token usage reported by the local Codex CLI, when available."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def has_values(self) -> bool:
        """Whether at least one token field is populated."""
        return any(value is not None for value in (self.input_tokens, self.output_tokens, self.total_tokens))


@dataclass(slots=True)
class CodexStreamEvent:
    """Structured stream event emitted while the Codex CLI runs."""

    kind: Literal[
        "assistant_text",
        "tool_started",
        "tool_completed",
        "file_change_started",
        "file_change_completed",
    ]
    item_id: str | None = None
    text: str | None = None
    command: str | None = None
    output: str | None = None
    exit_code: int | None = None
    changes: list[dict[str, str]] | None = None


class CodexExecutionStopped(RuntimeError):
    """Raised when a running Codex CLI task is intentionally stopped."""


class CodexClient:
    """Thin wrapper around the local Codex CLI."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._process_lock = asyncio.Lock()
        self._active_processes: dict[int, asyncio.subprocess.Process] = {}
        self._stopped_pids: set[int] = set()

    async def send_message(
        self,
        prompt: str,
        project_path: str | Path,
        backend_session_id: str | None = None,
        on_event: Callable[[CodexStreamEvent], Awaitable[None]] | None = None,
    ) -> CodexResult:
        """Send a prompt to Codex CLI and return the assistant reply."""
        resolved_project_path = Path(project_path).expanduser().resolve()
        command = self._build_command(
            prompt,
            project_path=resolved_project_path,
            backend_session_id=backend_session_id,
        )
        LOGGER.info(
            "Executing Codex CLI session=%s model=%s cwd=%s",
            backend_session_id or "(new)",
            self.config.codex_model,
            resolved_project_path,
        )
        if self.config.cli_use_shell():
            process = await asyncio.create_subprocess_exec(
                self.config.cli_shell_path(),
                "-lc",
                shlex.join(command),
                cwd=str(resolved_project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(resolved_project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        if process.pid is not None:
            async with self._process_lock:
                self._active_processes[process.pid] = process
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        thread_id = backend_session_id
        reply_chunks: list[str] = []
        last_pushed_text = ""
        usage: CodexUsage | None = None

        async def push_update() -> None:
            nonlocal last_pushed_text
            if on_event is None:
                return
            combined = "".join(reply_chunks)
            if not combined.strip() or combined == last_pushed_text:
                return
            last_pushed_text = combined
            await on_event(CodexStreamEvent(kind="assistant_text", text=combined))

        async def iter_lines(stream: asyncio.StreamReader):
            """Yield UTF-8 lines without relying on StreamReader.readline()."""
            buffer = bytearray()
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    if buffer:
                        yield buffer.decode("utf-8", errors="replace")
                    break

                buffer.extend(chunk)
                while True:
                    newline_index = buffer.find(b"\n")
                    if newline_index < 0:
                        break
                    raw_line = buffer[: newline_index + 1]
                    del buffer[: newline_index + 1]
                    yield raw_line.decode("utf-8", errors="replace")

        async def read_stdout() -> None:
            nonlocal thread_id, usage
            assert process.stdout is not None
            async for raw_line in iter_lines(process.stdout):
                line = raw_line.rstrip("\n")
                stdout_lines.append(line)

                payload = line.strip()
                if not payload.startswith("{"):
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                extracted_usage = self._extract_usage(event)
                if extracted_usage is not None:
                    usage = extracted_usage

                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id") or thread_id
                    continue

                event_type = event.get("type")
                item = event.get("item", {})
                item_type = item.get("type")

                if item_type == "command_execution":
                    if on_event is None:
                        continue
                    if event_type == "item.started":
                        await on_event(
                            CodexStreamEvent(
                                kind="tool_started",
                                item_id=item.get("id"),
                                command=item.get("command"),
                            )
                        )
                        continue
                    if event_type == "item.completed":
                        await on_event(
                            CodexStreamEvent(
                                kind="tool_completed",
                                item_id=item.get("id"),
                                command=item.get("command"),
                                output=item.get("aggregated_output"),
                                exit_code=item.get("exit_code"),
                            )
                        )
                        continue

                if item_type == "file_change":
                    if on_event is None:
                        continue
                    raw_changes = item.get("changes")
                    changes = [
                        {
                            "path": str(change.get("path", "")),
                            "kind": str(change.get("kind", "update")),
                        }
                        for change in raw_changes
                        if isinstance(change, dict)
                    ] if isinstance(raw_changes, list) else None
                    if event_type == "item.started":
                        await on_event(
                            CodexStreamEvent(
                                kind="file_change_started",
                                item_id=item.get("id"),
                                changes=changes,
                            )
                        )
                        continue
                    if event_type == "item.completed":
                        await on_event(
                            CodexStreamEvent(
                                kind="file_change_completed",
                                item_id=item.get("id"),
                                changes=changes,
                            )
                        )
                        continue

                if event_type == "item.completed" and item_type == "agent_message" and item.get("text"):
                    reply_chunks.append(item["text"])
                    await push_update()

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for raw_line in iter_lines(process.stderr):
                stderr_lines.append(raw_line.rstrip("\n"))

        try:
            await asyncio.gather(read_stdout(), read_stderr())
            await process.wait()
        except asyncio.CancelledError:
            if process.pid is not None:
                async with self._process_lock:
                    self._stopped_pids.add(process.pid)
            await self._wait_for_stop(process)
            raise
        finally:
            if process.pid is not None:
                async with self._process_lock:
                    self._active_processes.pop(process.pid, None)

        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines).strip()

        if process.returncode != 0:
            if process.pid is not None:
                async with self._process_lock:
                    if process.pid in self._stopped_pids:
                        self._stopped_pids.discard(process.pid)
                        raise CodexExecutionStopped("Codex CLI task was stopped")
            detail = stderr_text or stdout_text.strip() or f"exit code {process.returncode}"
            raise RuntimeError(f"Codex CLI request failed: {detail}")

        reply_text = "".join(reply_chunks).strip()
        if not reply_text:
            detail = stderr_text or stdout_text.strip() or "empty Codex CLI response"
            raise RuntimeError(f"Codex CLI returned no agent message: {detail}")

        if on_event is not None and reply_text != last_pushed_text:
            await on_event(CodexStreamEvent(kind="assistant_text", text=reply_text))

        return CodexResult(text=reply_text, backend_session_id=thread_id, usage=usage)

    async def stop_all(self) -> int:
        """Terminate all currently running Codex CLI subprocesses."""
        async with self._process_lock:
            processes = list(self._active_processes.values())
            for process in processes:
                if process.pid is not None:
                    self._stopped_pids.add(process.pid)

        stopped = 0
        for process in processes:
            if process.returncode is not None:
                continue
            try:
                process.terminate()
            except ProcessLookupError:
                continue
            stopped += 1

        if not processes:
            return 0

        await asyncio.gather(*(self._wait_for_stop(process) for process in processes))
        return stopped

    async def _wait_for_stop(self, process: asyncio.subprocess.Process) -> None:
        """Wait briefly for a process to exit, then force kill if needed."""
        if process.returncode is not None:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    return
                await process.wait()

    def _build_command(
        self,
        prompt: str,
        *,
        project_path: Path,
        backend_session_id: str | None,
    ) -> list[str]:
        """Build the command line for a new or resumed Codex session."""
        reasoning_args = [
            "-c",
            f'model_reasoning_effort="{self.config.codex_reasoning_effort}"',
        ]

        if backend_session_id:
            return [
                self.config.cli_path(),
                "exec",
                "resume",
                backend_session_id,
                "--skip-git-repo-check",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "-m",
                self.config.codex_model,
                *reasoning_args,
                prompt,
            ]

        return [
            self.config.cli_path(),
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            self.config.codex_model,
            *reasoning_args,
            "-C",
            str(project_path),
            prompt,
        ]

    @classmethod
    def _extract_usage(cls, payload: object) -> CodexUsage | None:
        """Best-effort extraction of token usage from arbitrary Codex JSON events."""
        if isinstance(payload, dict):
            usage = cls._usage_from_mapping(payload)
            if usage is not None:
                return usage
            for value in payload.values():
                nested_usage = cls._extract_usage(value)
                if nested_usage is not None:
                    return nested_usage
            return None

        if isinstance(payload, list):
            for item in payload:
                nested_usage = cls._extract_usage(item)
                if nested_usage is not None:
                    return nested_usage
        return None

    @staticmethod
    def _usage_from_mapping(payload: dict[object, object]) -> CodexUsage | None:
        """Map known token field aliases into a normalized usage payload."""
        field_aliases = {
            "input_tokens": ("input_tokens", "prompt_tokens", "input_token_count"),
            "output_tokens": ("output_tokens", "completion_tokens", "output_token_count"),
            "total_tokens": ("total_tokens", "total_token_count"),
        }
        resolved: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }

        for target_field, aliases in field_aliases.items():
            for alias in aliases:
                value = payload.get(alias)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    resolved[target_field] = value
                    break
                if isinstance(value, float) and value.is_integer():
                    resolved[target_field] = int(value)
                    break

        if not any(value is not None for value in resolved.values()):
            return None

        if resolved["total_tokens"] is None:
            known_parts = [value for value in (resolved["input_tokens"], resolved["output_tokens"]) if value is not None]
            if known_parts:
                resolved["total_tokens"] = sum(known_parts)

        usage = CodexUsage(**resolved)
        if usage.has_values():
            return usage
        return None
