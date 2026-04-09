from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from pathlib import Path

from tgboter.config import UserSessionState


class SessionStore:
    """Manage in-memory sessions with optional JSON persistence."""

    def __init__(
        self,
        storage_path: str,
        default_language: str = "zh",
        default_project_path: str = ".",
    ) -> None:
        self.storage_path = Path(storage_path)
        self._default_language = default_language
        self._default_project_path = str(Path(default_project_path).expanduser().resolve())
        self._lock = asyncio.Lock()
        self._users: dict[int, UserSessionState] = {}
        self._load()

    def _load(self) -> None:
        """Load persisted sessions if a store file exists."""
        if not self.storage_path.exists():
            return

        with self.storage_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        users: dict[int, UserSessionState] = {}
        for user_id_str, state in payload.items():
            raw_backend_sessions = state.get("backend_sessions", {})
            backend_sessions: dict[str, dict[str, str]] = {}
            for session_id, value in raw_backend_sessions.items():
                if isinstance(value, dict):
                    backend_sessions[session_id] = {
                        str(name): str(backend_id)
                        for name, backend_id in value.items()
                        if str(name).strip() and str(backend_id).strip()
                    }
                elif isinstance(value, str) and value.strip():
                    backend_sessions[session_id] = {"codex": value}
            users[int(user_id_str)] = UserSessionState(
                current_session=state.get("current_session"),
                sessions=state.get("sessions", {}),
                backend_sessions=backend_sessions,
                session_project_paths={
                    str(session_id): str(project_path)
                    for session_id, project_path in state.get("session_project_paths", {}).items()
                    if str(session_id).strip() and str(project_path).strip()
                },
                language=state.get("language", self._default_language),
            )
        self._users = users

    async def _save(self) -> None:
        """Persist all sessions to disk."""
        serializable = {str(user_id): asdict(state) for user_id, state in self._users.items()}
        with self.storage_path.open("w", encoding="utf-8") as file:
            json.dump(serializable, file, ensure_ascii=False, indent=2)

    async def ensure_user(self, user_id: int) -> UserSessionState:
        """Ensure the user state exists and has an active session."""
        async with self._lock:
            state = self._users.get(user_id)
            if state is None:
                state = UserSessionState(language=self._default_language)
                self._users[user_id] = state
            if not state.current_session:
                session_id = self._new_session_id()
                state.current_session = session_id
                state.sessions[session_id] = []
                state.session_project_paths[session_id] = self._default_project_path
                await self._save()
            return state

    async def create_session(self, user_id: int) -> str:
        """Create and switch to a new session for a user."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            session_id = self._new_session_id()
            state.sessions[session_id] = []
            state.session_project_paths[session_id] = self._default_project_path
            state.current_session = session_id
            await self._save()
            return session_id

    async def list_sessions(self, user_id: int) -> dict[str, list[dict[str, str]]]:
        """Return all sessions for a user."""
        state = await self.ensure_user(user_id)
        return state.sessions

    async def get_current_session_id(self, user_id: int) -> str:
        """Return the active session id for a user."""
        state = await self.ensure_user(user_id)
        assert state.current_session is not None
        return state.current_session

    async def switch_session(self, user_id: int, session_id: str) -> bool:
        """Switch the active session if it exists."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            if session_id not in state.sessions:
                return False
            state.current_session = session_id
            await self._save()
            return True

    async def reset_current_session(self, user_id: int) -> str:
        """Clear the current session history while keeping the session id."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            if not state.current_session:
                state.current_session = self._new_session_id()
            state.session_project_paths.setdefault(state.current_session, self._default_project_path)
            state.sessions[state.current_session] = []
            state.backend_sessions.pop(state.current_session, None)
            await self._save()
            return state.current_session

    async def clear_all_sessions(self) -> None:
        """Remove every stored session for every user."""
        async with self._lock:
            self._users.clear()
            await self._save()

    async def session_exists(self, user_id: int, session_id: str) -> bool:
        """Check whether a session is still stored for a user."""
        async with self._lock:
            state = self._users.get(user_id)
            return state is not None and session_id in state.sessions

    async def get_history(self, user_id: int, session_id: str | None = None) -> list[dict[str, str]]:
        """Get conversation history for a session."""
        state = await self.ensure_user(user_id)
        selected_session = session_id or state.current_session
        if not selected_session:
            return []
        return list(state.sessions.get(selected_session, []))

    async def append_message(self, user_id: int, role: str, content: str, session_id: str | None = None) -> str:
        """Append a message to a session history."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            if not state.current_session:
                state.current_session = self._new_session_id()
                state.sessions[state.current_session] = []
                state.session_project_paths[state.current_session] = self._default_project_path

            selected_session = session_id or state.current_session
            state.sessions.setdefault(selected_session, []).append({"role": role, "content": content})
            state.session_project_paths.setdefault(selected_session, self._default_project_path)
            await self._save()
            return selected_session

    async def get_user_state(self, user_id: int) -> UserSessionState:
        """Return the full state for a user."""
        return await self.ensure_user(user_id)

    async def get_backend_session_id(
        self,
        user_id: int,
        backend_name: str,
        session_id: str | None = None,
    ) -> str | None:
        """Return the backend session id mapped to a Telegram session."""
        state = await self.ensure_user(user_id)
        selected_session = session_id or state.current_session
        if not selected_session:
            return None
        return state.backend_sessions.get(selected_session, {}).get(backend_name)

    async def set_backend_session_id(
        self,
        user_id: int,
        backend_name: str,
        backend_session_id: str,
        session_id: str | None = None,
    ) -> None:
        """Map a Telegram session to a backend session id."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            if not state.current_session:
                state.current_session = self._new_session_id()
                state.sessions[state.current_session] = []
                state.session_project_paths[state.current_session] = self._default_project_path

            selected_session = session_id or state.current_session
            state.sessions.setdefault(selected_session, [])
            state.session_project_paths.setdefault(selected_session, self._default_project_path)
            state.backend_sessions.setdefault(selected_session, {})[backend_name] = backend_session_id
            await self._save()

    async def get_project_path(self, user_id: int, session_id: str | None = None) -> str:
        """Return the project path bound to a session."""
        state = await self.ensure_user(user_id)
        selected_session = session_id or state.current_session
        if not selected_session:
            return self._default_project_path
        return state.session_project_paths.get(selected_session, self._default_project_path)

    async def set_project_path(
        self,
        user_id: int,
        project_path: str,
        session_id: str | None = None,
    ) -> None:
        """Persist the project path bound to a session."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            if not state.current_session:
                state.current_session = self._new_session_id()
                state.sessions[state.current_session] = []
            selected_session = session_id or state.current_session
            state.sessions.setdefault(selected_session, [])
            state.session_project_paths[selected_session] = str(Path(project_path).expanduser().resolve())
            await self._save()

    async def get_language(self, user_id: int) -> str:
        """Return the user's preferred UI language."""
        state = await self.ensure_user(user_id)
        return state.language

    async def set_language(self, user_id: int, language: str) -> None:
        """Persist the user's preferred UI language."""
        async with self._lock:
            state = self._users.setdefault(user_id, UserSessionState(language=self._default_language))
            state.language = language
            await self._save()

    @staticmethod
    def _new_session_id() -> str:
        """Create a compact session id."""
        return uuid.uuid4().hex[:12]
