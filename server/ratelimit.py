"""Sliding-window rate limits shared by the HTTP API and the agent.

Counters are kept in memory per process. Every limit is a named window:
per-session ones are keyed by the session id, global ones by a constant.
The maxima are read from the environment on every check so an operator (or
a test) can change them without restarting the process.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass

GLOBAL_KEY = "*"


@dataclass(frozen=True)
class Limit:
    name: str
    env: str
    default: int
    window_s: int
    scope: str          # "session" | "global"


LIMITS: dict[str, Limit] = {
    "agent_hour": Limit("agent_hour", "AGENT_TURNS_PER_HOUR", 30, 3600, "session"),
    "agent_day": Limit("agent_day", "AGENT_TURNS_PER_DAY", 3000, 86400, "global"),
    "street_hour": Limit("street_hour", "STREET_REQUESTS_PER_HOUR", 60, 3600, "session"),
    "plan_hour": Limit("plan_hour", "PLAN_REQUESTS_PER_HOUR", 300, 3600, "session"),
}


class RateLimited(Exception):
    """Raised when a window is full; carries the seconds until it frees up."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = max(1, int(retry_after))


class RateLimiter:
    """Exact sliding-window log: one timestamp per accepted request."""

    def __init__(self, limits: dict[str, Limit] | None = None) -> None:
        self._limits = dict(limits or LIMITS)
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def max_for(self, name: str) -> int:
        """Maximum requests allowed in the window, from the env or the default."""
        limit = self._limits[name]
        raw = os.environ.get(limit.env, "").strip()
        try:
            return max(0, int(raw)) if raw else limit.default
        except ValueError:
            return limit.default

    def remaining(self, name: str, session_id: str) -> int:
        """Requests still allowed in the current window."""
        with self._lock:
            hits = self._pruned(name, session_id)
            return max(0, self.max_for(name) - len(hits))

    def check(self, name: str, session_id: str) -> None:
        """Raise RateLimited if the next request would exceed the window."""
        with self._lock:
            self._check_locked(name, session_id)

    def hit(self, name: str, session_id: str) -> None:
        """Record one accepted request."""
        with self._lock:
            self._pruned(name, session_id).append(time.monotonic())

    def consume(self, name: str, session_id: str) -> None:
        """Check and record in one step (atomic)."""
        with self._lock:
            self._check_locked(name, session_id)
            self._pruned(name, session_id).append(time.monotonic())

    def consume_agent(self, session_id: str) -> None:
        """One agent turn: both the hourly session window and the daily global window."""
        with self._lock:
            self._check_locked("agent_hour", session_id)
            self._check_locked("agent_day", session_id)
            now = time.monotonic()
            self._pruned("agent_hour", session_id).append(now)
            self._pruned("agent_day", session_id).append(now)

    def reset(self) -> None:
        """Forget every counter (tests)."""
        with self._lock:
            self._hits.clear()

    # ------------------------------------------------------------------ internals
    def _key(self, name: str, session_id: str) -> tuple[str, str]:
        limit = self._limits[name]
        return (name, session_id if limit.scope == "session" else GLOBAL_KEY)

    def _pruned(self, name: str, session_id: str) -> deque[float]:
        """The hit log for a key with expired entries dropped (call with the lock held)."""
        key = self._key(name, session_id)
        window = self._limits[name].window_s
        hits = self._hits.setdefault(key, deque())
        cutoff = time.monotonic() - window
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def _check_locked(self, name: str, session_id: str) -> None:
        limit = self._limits[name]
        hits = self._pruned(name, session_id)
        maximum = self.max_for(name)
        if len(hits) < maximum:
            return
        retry_after = limit.window_s - (time.monotonic() - hits[0]) if hits else limit.window_s
        scope = "this session" if limit.scope == "session" else "this server"
        raise RateLimited(
            f"Rate limit reached: {maximum} {limit.name.replace('_', ' per ')} for {scope}. "
            f"Try again in about {_humanize(retry_after)}.",
            retry_after,
        )


def _humanize(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600, 1):g} h"


LIMITER = RateLimiter()
