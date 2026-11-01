"""The audit log (SPEC.md §9.4): one JSON line per tool call, a file per day, kept 90 days.

Earlier days' files are gzipped once a day (a soak of 20 concurrent callers wrote about 1 KB a call).

Each line says who called (principal id and name, transport, client), what (tool, argument names,
company), what reached
the database (the SQL as sent, mode), and how it ended (rows, elapsed ms, outcome, error code and the
correlation id an INTERNAL error shows the caller). Argument values are never written. With
``server.audit_redact_literals`` (the default) quoted and numeric literals in the SQL are replaced, so
the log holds no customer names, amounts or other values. Bearer tokens never reach it.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sage100_mcp.driver_errors import redact

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
_NUMBER = re.compile(r"(?<![\w.$@#])[-+]?\d+(?:\.\d+)?(?![\w.])")
_CORRELATION = re.compile(r"correlation id ([0-9a-f]{12})")


def redact_sql(sql: str) -> str:
    """Quoted literals (dates in {d '...'} included) become '…', and numbers become ?."""
    return _NUMBER.sub("?", redact(sql))


class AuditLog:
    """Appends call records to ``<directory>/YYYY-MM-DD.jsonl`` (UTC days). Thread-safe; never raises."""

    def __init__(self, directory: Path, *, redact_literals: bool = True, keep_days: int = KEEP_DAYS) -> None:
        self.directory = directory
        self.redact_literals = redact_literals
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._pruned: date | None = None

    def path_for(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.jsonl"

    def record(self, entry: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        line = {"ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), **entry}
        if self.redact_literals and line.get("sql"):
            line["sql"] = [redact_sql(sql) for sql in line["sql"]]
        text = json.dumps(line, ensure_ascii=False, default=str)
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.path_for(now.date()).open("a", encoding="utf-8", newline="\n") as handle:  # LF everywhere
                    handle.write(text + "\n")
                if self._pruned != now.date():
                    self._pruned = now.date()
                    self._prune(now.date())
        except OSError as exc:  # an unwritable audit directory must not take tools down
            logger.error("Could not write the audit log in %s: %s", self.directory, exc)

    def _prune(self, today: date) -> None:
        """Once a day: delete files past keep_days, and gzip earlier days' files (JSON text shrinks ~10x)."""
        oldest = today - timedelta(days=self.keep_days)
        for path in self.directory.glob("*.jsonl*"):
            try:
                day = date.fromisoformat(path.name.split(".", 1)[0])
            except ValueError:
                continue
            if day < oldest:
                path.unlink(missing_ok=True)
            elif day < today and path.suffix == ".jsonl":
                packed = path.with_name(path.name + ".gz")
                with path.open("rb") as source, gzip.open(packed, "wb") as target:
                    shutil.copyfileobj(source, target)
                path.unlink()


def call_entry(
    *,
    principal: str,
    name: str,
    transport: str,
    client: str | None,
    tool: str,
    arguments: dict[str, Any],
    structured: dict[str, Any] | None,
    is_error: bool,
    elapsed_ms: int,
) -> dict[str, Any]:
    """One audit record from a finished tool call and its result."""
    data = structured or {}
    entry: dict[str, Any] = {
        "principal": principal,
        "name": name,
        "transport": transport,
        "client": client,
        "tool": tool,
        "args": sorted(arguments),
        "company": data.get("company"),
        "sql": list(data.get("sent_sql") or []),
        "mode": data.get("mode"),
        "rows": None if is_error else data.get("row_count"),
        "elapsed_ms": elapsed_ms,
        "outcome": "error" if is_error else "ok",
        "code": data.get("code") if is_error else None,
    }
    if is_error and data.get("code") is None:
        entry["code"] = "INVALID_ARGUMENTS"  # the SDK refused the arguments before the tool ran
    found = _CORRELATION.search(str(data.get("hint") or ""))
    if found:
        entry["correlation_id"] = found.group(1)
    return {key: value for key, value in entry.items() if value not in (None, [], "")}
