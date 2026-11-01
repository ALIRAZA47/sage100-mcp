"""Who is calling, and what the server keeps per caller (SPEC.md §9.3, §10.3).

Over stdio the caller is the process. Over HTTP it is the authenticated principal: a static bearer
token's id, or an OAuth access token's issuer and subject. The active company and the rate limit are
kept per principal. An active company set over HTTP lapses after 8 hours; a token limited to some
companies sees only those, everywhere a company can be named or listed.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sage100_mcp.errors import ErrorCode, SageError

ACTIVE_FOR_S = 8 * 3600  # §10.3: an HTTP principal's active company expires after 8 hours
WINDOW_S = 60.0


@dataclass(frozen=True)
class Principal:
    id: str  # the state key: "stdio", "token:<id>" or "oauth:<issuer>#<subject>"
    label: str  # the name the audit log shows: "local", a token's principal name, an OAuth subject
    companies: frozenset[str] | None = None  # the companies this caller may use; None means all
    transport: str = "stdio"

    def may_use(self, code: str) -> bool:
        return self.companies is None or code in self.companies


LOCAL = Principal("stdio", "local")
_current: ContextVar[Principal] = ContextVar("sage100_principal", default=LOCAL)


def current() -> Principal:
    return _current.get()


@contextmanager
def acting_as(principal: Principal) -> Iterator[None]:
    """Run the block as ``principal``; worker threads the block starts inherit it."""
    token = _current.set(principal)
    try:
        yield
    finally:
        _current.reset(token)


class Principals:
    """The active company and recent call times of each principal. Thread-safe."""

    def __init__(self, rate_per_min: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate_per_min = rate_per_min
        self._clock = clock
        self._lock = threading.Lock()
        self._active: dict[str, tuple[str, float]] = {}
        self._calls: dict[str, deque[float]] = {}

    def active(self, principal: Principal) -> str | None:
        with self._lock:
            found = self._active.get(principal.id)
            if found is None:
                return None
            code, since = found
            if principal.transport != "stdio" and self._clock() - since > ACTIVE_FOR_S:
                del self._active[principal.id]
                return None
            return code if principal.may_use(code) else None

    def set_active(self, principal: Principal, code: str | None) -> None:
        with self._lock:
            if code is None:
                self._active.pop(principal.id, None)
            else:
                self._active[principal.id] = (code, self._clock())

    def admit(self, principal: Principal) -> None:
        """Count one call; RATE_LIMITED once the last minute already holds ``rate_per_min`` of them."""
        now = self._clock()
        with self._lock:
            calls = self._calls.setdefault(principal.id, deque())
            while calls and now - calls[0] >= WINDOW_S:
                calls.popleft()
            if len(calls) >= self.rate_per_min:
                wait = max(1, math.ceil(WINDOW_S - (now - calls[0])))
                raise SageError(
                    ErrorCode.RATE_LIMITED,
                    f"More than {self.rate_per_min} calls in a minute.",
                    hint=f"Wait {wait} s, then retry. Combine questions into fewer, broader calls where you can.",
                    retry_after_s=wait,
                )
            calls.append(now)
