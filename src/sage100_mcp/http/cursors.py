"""run_query paging (SPEC.md §13.3): signed cursors and the page snapshots behind them.

A truncated run_query result carries ``next_cursor``, an HMAC-signed token naming the principal it was
issued to, the company, a hash of the query (sql, params, mode and dialect), where the next page
starts, a hash of the rows already shown, and an expiry. A cursor that was altered, comes from an
earlier run of the server, belongs to another principal, company or query, or is more than 10 minutes
old is refused with CURSOR_INVALID.

The first page costs what it always did. The first time a cursor is used the query runs once more and
up to ``max_pushdown_rows`` rows are kept in a DuckDB snapshot; the rows already shown must come back
first (the same rows, in any order), or the cursor is refused because the data changed underneath it.
Every later page reads the snapshot, so it costs nothing on the driver and the pages agree with each
other. A snapshot lives 10 minutes from when it was taken (docs/decisions.md D53).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sage100_mcp.errors import ErrorCode, SageError

TTL_S = 600  # §13.3: snapshots are kept for 10 minutes
MAX_SNAPSHOTS = 16  # the oldest is dropped beyond this, whoever it belongs to
_LOAD_CHUNK = 20_000
_AGAIN = "Run the query again without cursor."


def query_key(sql: str, params: Sequence[object] | None, mode: str, dialect: str) -> str:
    """What makes two calls the same query: page size, timeout and company are not part of it."""
    text = json.dumps([sql, list(params or []), mode, dialect], default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def rows_digest(rows: Sequence[Sequence[Any]]) -> str:
    """The same rows in any order give the same digest; a row that differs, or one more or less, does not."""
    lines = sorted(json.dumps(list(row), default=str, separators=(",", ":")) for row in rows)
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8") + b"\n")
    return digest.hexdigest()[:32]


def invalid(message: str, hint: str = _AGAIN) -> SageError:
    return SageError(ErrorCode.CURSOR_INVALID, message, hint=hint)


@dataclass(frozen=True)
class Position:
    """A verified cursor: where the next page starts, and what it pages through."""

    token: str
    principal: str
    company: str
    query: str
    offset: int
    snapshot: str | None  # None until the cursor is first used
    shown: str | None  # rows_digest of the rows shown before the snapshot was taken
    expires: float


@dataclass
class Snapshot:
    """A kept result. ``head`` holds what every page repeats: columns, mode, warnings, as_of."""

    id: str
    principal: str
    company: str
    query: str
    count: int
    capped: bool  # the result had more rows than the snapshot keeps
    head: Any  # query.QueryResult without rows
    expires: float


class Cursors:
    """Issues and checks cursors, and keeps snapshots in an in-memory DuckDB. Thread-safe."""

    def __init__(self, temp_directory: Path, *, ttl_s: float = TTL_S, clock: Callable[[], float] = time.time) -> None:
        self.temp_directory = temp_directory
        self.ttl_s = ttl_s
        self._clock = clock
        self._key = secrets.token_bytes(32)  # per process: a restart retires every cursor with its snapshots
        self._lock = threading.Lock()
        self._db: Any = None
        self._snapshots: dict[str, Snapshot] = {}
        self._opened: dict[str, str] = {}  # first-use token -> its snapshot, so a retried call reuses it

    # --- tokens ------------------------------------------------------------------------------------

    def _sign(self, body: bytes) -> str:
        mac = hmac.new(self._key, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")

    def issue(
        self,
        principal: str,
        company: str,
        query: str,
        offset: int,
        *,
        snapshot: Snapshot | None = None,
        shown: str | None = None,
    ) -> str:
        expires = snapshot.expires if snapshot else self._clock() + self.ttl_s
        payload = {"v": 1, "p": principal, "c": company, "q": query, "o": offset, "e": int(expires)}
        payload |= {"s": snapshot.id} if snapshot else {"h": shown}
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).rstrip(b"=")
        return f"{body.decode('ascii')}.{self._sign(body)}"

    def read(self, token: str, *, principal: str, query: str) -> Position:
        """The cursor's position, once it is shown to be this server's, this caller's, current and this query's."""
        body, _, signature = token.strip().partition(".")
        expected = self._sign(body.encode("utf-8")).encode("ascii")
        if not body or not hmac.compare_digest(signature.encode("utf-8"), expected):
            raise invalid("This cursor is not valid: it was altered, or it is from before the server restarted.")
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            position = Position(
                token=token.strip(),
                principal=str(payload["p"]),
                company=str(payload["c"]),
                query=str(payload["q"]),
                offset=int(payload["o"]),
                snapshot=payload.get("s"),
                shown=payload.get("h"),
                expires=float(payload["e"]),
            )
        except (binascii.Error, ValueError, KeyError, TypeError):
            raise invalid("This cursor is not valid.") from None
        if position.principal != principal:
            raise invalid("This cursor was issued to another caller.")
        if self._clock() > position.expires:
            raise invalid(f"This cursor has expired; pages are kept for {int(self.ttl_s // 60)} minutes.")
        if position.query != query:
            raise invalid(
                "This cursor belongs to a different query.",
                hint="Send the same sql, params, mode and dialect as the first page, or run the new query "
                "without cursor.",
            )
        return position

    # --- snapshots ---------------------------------------------------------------------------------

    def _connection(self) -> Any:
        if self._db is None:
            import duckdb

            self._db = duckdb.connect(":memory:")
            self._db.execute("SET memory_limit = '1GB'")
            self.temp_directory.mkdir(parents=True, exist_ok=True)
            self._db.execute("SET temp_directory = ?", [str(self.temp_directory)])
        return self._db

    def _expire(self) -> None:
        """Drop snapshots past their time, then the oldest beyond MAX_SNAPSHOTS. Holds the lock."""
        now = self._clock()
        by_age = sorted(self._snapshots.values(), key=lambda snapshot: snapshot.expires)
        stale = [s for s in by_age if s.expires < now]
        live = [s for s in by_age if s.expires >= now]
        for snapshot in stale + live[: max(0, len(live) - MAX_SNAPSHOTS)]:
            del self._snapshots[snapshot.id]
            self._connection().execute(f"DROP TABLE IF EXISTS s_{snapshot.id}")
        self._opened = {token: id_ for token, id_ in self._opened.items() if id_ in self._snapshots}

    def opened(self, position: Position) -> Snapshot | None:
        """The snapshot this cursor reads: its own, or the one its first use took."""
        with self._lock:
            self._expire()
            wanted = position.snapshot or self._opened.get(position.token)
            snapshot = self._snapshots.get(wanted) if wanted else None
        if snapshot is None and position.snapshot is not None:
            raise invalid(f"The rows behind this cursor are no longer kept (at most {int(self.ttl_s // 60)} minutes).")
        return snapshot

    def save(self, position: Position, rows: Sequence[Sequence[Any]], *, capped: bool, head: Any) -> Snapshot:
        snapshot = Snapshot(
            id=secrets.token_hex(8),
            principal=position.principal,
            company=position.company,
            query=position.query,
            count=len(rows),
            capped=capped,
            head=head,
            expires=self._clock() + self.ttl_s,
        )
        with self._lock:
            db = self._connection()
            db.execute(f"CREATE TABLE s_{snapshot.id} (n BIGINT, row VARCHAR)")
            insert = f"INSERT INTO s_{snapshot.id} SELECT UNNEST(?::BIGINT[]), UNNEST(?::VARCHAR[])"
            for start in range(0, len(rows), _LOAD_CHUNK):
                chunk = rows[start : start + _LOAD_CHUNK]
                lines = [json.dumps(list(row), default=str, separators=(",", ":")) for row in chunk]
                db.execute(insert, [list(range(start, start + len(chunk))), lines])
            self._snapshots[snapshot.id] = snapshot
            self._opened[position.token] = snapshot.id
            self._expire()
        return snapshot

    def page(self, snapshot: Snapshot, offset: int, limit: int) -> list[list[Any]]:
        with self._lock:
            if snapshot.id not in self._snapshots:
                raise invalid("The rows behind this cursor are no longer kept.")
            found = (
                self._connection()
                .execute(f"SELECT row FROM s_{snapshot.id} WHERE n >= ? ORDER BY n LIMIT ?", [offset, limit])
                .fetchall()
            )
        return [json.loads(line) for (line,) in found]

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._snapshots.clear()
            self._opened.clear()
