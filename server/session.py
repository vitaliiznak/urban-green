"""Browser sessions: identity, rule overrides and agent history.

The client generates a UUID and sends it as `X-Session-Id`; a cookie
`canopy_sid` is the fallback for clients that cannot set headers, and a fresh
UUID is minted when neither is present. The id chosen for a request is echoed
in the `X-Session-Id` response header on every response.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import Request, Response

from .schemas import RuleOverride

SESSION_HEADER = "X-Session-Id"
SESSION_COOKIE = "canopy_sid"
SESSION_TTL_S = 24 * 3600
MAX_SESSIONS = 5000
HISTORY_LIMIT = 16

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass
class Session:
    id: str
    rule_overrides: dict[str, RuleOverride] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)
    last_street_id: str | None = None
    last_scenario_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    """In-memory sessions with idle expiry and a hard cap."""

    def __init__(self, ttl_s: int = SESSION_TTL_S, max_entries: int = MAX_SESSIONS) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_s
        self._max = max_entries
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> Session:
        """Return the live session for an id, creating it on first sight."""
        with self._lock:
            self._sweep()
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(id=session_id)
                self._sessions[session_id] = session
            session.last_seen = time.time()
            return session

    def __len__(self) -> int:
        return len(self._sessions)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _sweep(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s.last_seen > self._ttl]
        for sid in expired:
            del self._sessions[sid]
        if len(self._sessions) >= self._max:
            oldest = sorted(self._sessions.values(), key=lambda s: s.last_seen)
            for s in oldest[: len(self._sessions) - self._max + 1]:
                del self._sessions[s.id]


SESSIONS = SessionStore()


def valid_session_id(value: str | None) -> bool:
    """Client-supplied ids are accepted only when they look like an opaque token."""
    return bool(value) and _ID_RE.match(value) is not None


def resolve_session_id(request: Request) -> tuple[str, bool]:
    """Pick the session id for a request: header, then cookie, then a new UUID.

    Returns (id, is_new)."""
    header = request.headers.get(SESSION_HEADER)
    if valid_session_id(header):
        return header, False
    cookie = request.cookies.get(SESSION_COOKIE)
    if valid_session_id(cookie):
        return cookie, False
    return str(uuid.uuid4()), True


async def session_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Attach the session id to `request.state` and echo it on the response."""
    session_id, is_new = resolve_session_id(request)
    request.state.session_id = session_id
    response = await call_next(request)
    response.headers[SESSION_HEADER] = session_id
    if is_new:
        response.set_cookie(SESSION_COOKIE, session_id, max_age=30 * 24 * 3600, samesite="lax", path="/")
    return response


def current_session(request: Request) -> Session:
    """FastAPI dependency: the Session object for this request."""
    session_id = getattr(request.state, "session_id", None) or resolve_session_id(request)[0]
    return SESSIONS.get_or_create(session_id)


def remember_message(session: Session, items: list[dict[str, Any]], limit: int = HISTORY_LIMIT) -> None:
    """Append agent history items and trim to the last `limit` messages.

    Trimming never cuts a tool exchange in half: after taking the tail, leading
    items are dropped until the history starts with a plain user message."""
    session.history.extend(items)
    session.history = trim_history(session.history, limit)


def trim_history(items: list[dict[str, Any]], limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    """Last `limit` items, starting at a user message so tool calls keep their results."""
    tail = list(items[-limit:]) if limit > 0 else []
    while tail and not (tail[0].get("role") == "user" and isinstance(tail[0].get("content"), str)):
        tail.pop(0)
    return tail
