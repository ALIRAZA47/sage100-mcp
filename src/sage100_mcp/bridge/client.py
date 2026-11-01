"""The server's side of the 32-bit driver helper (helper.py; docs/decisions.md D67).

Bridge has the parts of pyodbc's module surface that the ProvideX executor uses: connect, Error and the
getinfo codes. Its connections and cursors have the parts of pyodbc's that the executor uses. So the
executor, the capability probe and doctor run on it unchanged, and each call they make runs in the helper
process, on Sage's 32-bit driver.

One helper serves a backend. Calls from many threads share it, and each waits for its own reply. If the
helper stops (Sage's driver crashed, or something killed it), every waiting call fails with an error that
maps to BACKEND_UNREACHABLE. That helper's connections then read as closed, so the pools drop them, and
the next connect starts a new helper.
"""

from __future__ import annotations

import functools
import itertools
import logging
import os
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import IO, Any, ClassVar, Protocol, cast

from sage100_mcp.bridge.helper import PROTOCOL, ProtocolError, encode, read_message
from sage100_mcp.driver_errors import redact_secrets

logger = logging.getLogger(__name__)

LABEL = "[sage100-mcp][32-bit helper]"
START_TIMEOUT_S = 30.0  # for the helper to start and answer hello
STOP_TIMEOUT_S = 5.0  # for the helper to exit once its input ends, before it is killed
FETCHALL_BATCH = 1000


class Error(Exception):
    """pyodbc.Error, raised in the helper: args are (sqlstate, message)."""


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


_ERROR_TYPES: tuple[type[Error], ...] = (
    Error, InterfaceError, DatabaseError, DataError, OperationalError, IntegrityError, InternalError,
    ProgrammingError, NotSupportedError,
)  # fmt: skip
_ERRORS = {kind.__name__: kind for kind in _ERROR_TYPES}


class HelperFailure(Exception):
    """The helper failed on something other than the driver. That is a fault, so the server reports INTERNAL."""

    def __init__(self, text: str, remote_traceback: str = "") -> None:
        super().__init__(text)
        self.remote_traceback = remote_traceback


def _unbracketed(text: str) -> str:
    """Error mapping keeps what follows the last "]" as the message, so the detail can't contain one."""
    return text.replace("[", "(").replace("]", ")")


def not_started(reason: str) -> Error:
    """Maps to DRIVER_NOT_INSTALLED, with the helper's own hint (errors.yaml)."""
    text = f"The 32-bit driver helper could not start: {_unbracketed(reason)}"
    return InterfaceError("IM003", f"[IM003] {LABEL}{text}")


def stopped(detail: str = "") -> Error:
    """Maps to BACKEND_UNREACHABLE, with the helper's own hint (errors.yaml)."""
    return OperationalError("08S01", f"[08S01] {LABEL}The 32-bit driver helper stopped{_unbracketed(detail)}")


def _exception(payload: Mapping[str, Any]) -> BaseException:
    if "driver" in payload:
        kind = _ERRORS.get(str(payload["driver"]), Error)
        return kind(*payload.get("args", ()))
    error = HelperFailure(f"{payload.get('failure')}: {payload.get('message')}", str(payload.get("traceback") or ""))
    logger.error("The 32-bit driver helper failed: %s\n%s", error, redact_secrets(error.remote_traceback))
    return error


def _copy(error: Error) -> Error:
    """A fresh instance for each waiting call: one exception object raised in many threads tangles tracebacks."""
    return type(error)(*error.args)


# --- channels to a helper ----------------------------------------------------------------------------------


class Channel(Protocol):
    """A running helper: its replies, its requests, and how to stop it."""

    reader: IO[bytes]
    writer: IO[bytes]

    def close(self) -> None:
        """End the helper's input, so it exits; kill it if it doesn't."""

    def wait(self, timeout: float) -> int | None:
        """The helper's exit code, or None while it runs."""

    def tail(self) -> list[str]:
        """Its last lines of stderr."""


def helper_environment() -> dict[str, str]:
    """This process's environment, less what the helper must not see or be steered by."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("SAGE100_MCP_", "PYTHON"))  # credentials; a 64-bit PYTHONHOME/PATH
    }


class ProcessChannel:
    """The helper as a child process: requests on its stdin, replies on its stdout, stderr logged."""

    def __init__(self, command: Sequence[str], env: Mapping[str, str] | None = None) -> None:
        if not command:
            raise not_started(
                "no 32-bit helper is installed. Reinstall sage100-mcp from its MSI, or set the backend's "
                "helper to a 32-bit python.exe that has pyodbc"
            )
        self.command = tuple(command)
        try:
            self.process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env) if env is not None else helper_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console window under the service
            )
        except OSError as exc:
            raise not_started(f"{command[0]}: {exc.strerror or exc}") from None
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.writer: IO[bytes] = self.process.stdin
        self.reader: IO[bytes] = self.process.stdout
        self._tail: deque[str] = deque(maxlen=20)
        threading.Thread(target=self._log_stderr, name="sage100-helper-stderr", daemon=True).start()

    def _log_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw in self.process.stderr:
            line = redact_secrets(raw.decode("utf-8", "replace").rstrip())
            if line:
                self._tail.append(line)
                logger.warning("32-bit driver helper: %s", line)

    def tail(self) -> list[str]:
        return list(self._tail)

    def wait(self, timeout: float) -> int | None:
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        with suppress(OSError, ValueError):
            self.writer.close()
        if self.wait(STOP_TIMEOUT_S) is None:
            self.process.kill()
            self.wait(STOP_TIMEOUT_S)


# --- one helper process and the calls waiting on it ---------------------------------------------------------


class _Waiter:
    __slots__ = ("done", "error", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class Session:
    """One helper process: requests out, replies matched back to the calls waiting for them."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.info: dict[str, Any] = {}
        self.alive = True
        self.closing = False
        self.failure: Error | None = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Waiter] = {}
        self._ids = itertools.count(1)
        self._reader = threading.Thread(target=self._read, name="sage100-helper-replies", daemon=True)
        self._reader.start()

    def call(self, op: str, *, within: float | None = None, **fields: Any) -> Any:
        """Send one request and wait for its reply (at most ``within`` seconds, else the helper is stopped)."""
        waiter = _Waiter()
        with self._lock:
            if not self.alive:
                raise _copy(self.failure or stopped())
            ident = next(self._ids)
            self._pending[ident] = waiter
        try:
            data = encode({"id": ident, "op": op, **fields})
        except (TypeError, ValueError, ProtocolError) as exc:  # a parameter value with no wire form
            with self._lock:
                self._pending.pop(ident, None)
            raise ProgrammingError("HY105", f"[HY105] {LABEL}Invalid parameter type: {exc}") from None
        try:
            with self._write_lock:
                self.channel.writer.write(data)
                self.channel.writer.flush()
        except (OSError, ValueError):
            self._end(": its input closed")
        if not waiter.done.wait(within):
            self.closing = True
            self.channel.close()  # unanswered: stop it, which fails any other call waiting on it
            raise TimeoutError(f"no answer within {within:g} s")
        if waiter.error is not None:
            raise waiter.error
        return waiter.result

    def _read(self) -> None:
        reason = ""
        try:
            while True:
                message = read_message(self.channel.reader)
                if message is None:
                    break
                with self._lock:
                    waiter = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
                if waiter is None:
                    continue
                error = message.get("error")
                if error is None:
                    waiter.result = message.get("result")
                else:
                    waiter.error = _exception(error)
                waiter.done.set()
        except (ProtocolError, OSError, ValueError) as exc:
            reason = f": {exc}"
        self._end(reason)

    def _end(self, reason: str) -> None:
        code = self.channel.wait(STOP_TIMEOUT_S)
        if self.closing:
            failure = stopped(" (sage100-mcp stopped it)")
        else:
            failure = stopped(f"{f' (exit code {code})' if code is not None else ''}{reason}")
        with self._lock:
            if not self.alive:
                return
            self.alive = False
            self.failure = failure
            waiting = list(self._pending.values())
            self._pending.clear()
        if not self.closing:
            logger.warning("%s; its last lines: %s", failure.args[1], " | ".join(self.channel.tail()) or "none")
        for waiter in waiting:
            waiter.error = _copy(failure)
            waiter.done.set()

    def close(self) -> None:
        self.closing = True
        self.channel.close()
        self._reader.join(STOP_TIMEOUT_S + 1)


# --- pyodbc's surface ---------------------------------------------------------------------------------------


class Row(tuple[Any, ...]):
    """A row like pyodbc's: a tuple whose columns are also attributes (``row.table_name``)."""

    __slots__ = ()
    _names: ClassVar[dict[str, int]] = {}

    def __getattr__(self, name: str) -> Any:
        try:
            return self[self._names[name]]
        except KeyError:
            raise AttributeError(name) from None


@functools.lru_cache(maxsize=256)
def row_type(names: tuple[str, ...]) -> type[Row]:
    columns = {name: index for index, name in reversed(list(enumerate(names)))}  # the first of a repeated name
    return cast(type[Row], type("Row", (Row,), {"__slots__": (), "_names": columns}))


class Connection:
    """A connection the helper holds open. Its timeout applies, as in pyodbc, to each new cursor."""

    def __init__(self, session: Session, handle: int, autocommit: bool) -> None:
        self._session = session
        self._handle = handle
        self.autocommit = autocommit
        self.timeout = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        """Closed, or its helper has stopped: either way the pools drop it."""
        return self._closed or not self._session.alive

    def cursor(self) -> Cursor:
        reply = self._session.call("cursor", connection=self._handle, timeout=int(self.timeout or 0))
        return Cursor(self, self._session, int(reply["cursor"]))

    def getinfo(self, code: int) -> Any:
        return self._session.call("getinfo", connection=self._handle, code=code)

    def close(self) -> None:
        if not self.closed:
            self._closed = True
            self._session.call("close", connection=self._handle)
        self._closed = True


class Cursor:
    def __init__(self, connection: Connection, session: Session, handle: int) -> None:
        self.connection = connection
        self._session = session
        self._handle = handle
        self.description: list[tuple[Any, ...]] | None = None
        self._row = row_type(())
        self._closed = False

    def _call(self, op: str, **fields: Any) -> Any:
        return self._session.call(op, cursor=self._handle, **fields)

    def _results(self, description: list[list[Any]] | None) -> Cursor:
        self.description = [tuple(column) for column in description] if description else None
        self._row = row_type(tuple(str(column[0]) for column in self.description or ()))
        return self

    def execute(self, sql: str, *params: Any) -> Cursor:
        if len(params) == 1 and isinstance(params[0], list | tuple):  # pyodbc takes one sequence, too
            params = tuple(params[0])
        return self._results(self._call("execute", sql=sql, params=list(params))["description"])

    def fetchmany(self, size: int = 1) -> list[Row]:
        return [self._row(values) for values in self._call("fetch", size=size)["rows"]]

    def fetchone(self) -> Row | None:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def fetchall(self) -> list[Row]:
        rows: list[Row] = []
        while True:
            batch = self.fetchmany(FETCHALL_BATCH)
            rows.extend(batch)
            if len(batch) < FETCHALL_BATCH:
                return rows

    def _catalog(self, op: str, **arguments: str | None) -> Cursor:
        given = {name: value for name, value in arguments.items() if value is not None}
        return self._results(self._call(op, arguments=given)["description"])

    def tables(
        self, table: str | None = None, catalog: str | None = None, schema: str | None = None,
        tableType: str | None = None,  # pyodbc's keyword
    ) -> Cursor:  # fmt: skip
        return self._catalog("tables", table=table, catalog=catalog, schema=schema, tableType=tableType)

    def columns(
        self, table: str | None = None, catalog: str | None = None, schema: str | None = None, column: str | None = None
    ) -> Cursor:
        return self._catalog("columns", table=table, catalog=catalog, schema=schema, column=column)

    def primaryKeys(self, table: str, catalog: str | None = None, schema: str | None = None) -> Cursor:  # pyodbc's name
        return self._catalog("primaryKeys", table=table, catalog=catalog, schema=schema)

    def cancel(self) -> None:
        """SQLCancel in the helper, alongside the call this cursor is making."""
        self._call("cancel")

    def close(self) -> None:
        if not self._closed and self._session.alive:
            self._closed = True
            self._call("close_cursor")
        self._closed = True


class Bridge:
    """pyodbc, as the ProvideX executor uses it, with Sage's driver loaded in the 32-bit helper."""

    Error = Error
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    OperationalError = OperationalError
    ProgrammingError = ProgrammingError
    SQL_DRIVER_NAME, SQL_DRIVER_VER, SQL_DBMS_NAME, SQL_DBMS_VER = 6, 7, 17, 18  # SQLGetInfo codes

    def __init__(
        self,
        command: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        opener: Callable[[], Channel] | None = None,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self.command = tuple(command)
        self._opener = opener or (lambda: ProcessChannel(self.command, env))
        self.start_timeout_s = start_timeout_s
        self.starts = 0  # helpers started so far
        self._session: Session | None = None
        self._lock = threading.Lock()

    @property
    def bits(self) -> int | None:
        """The helper's bitness, once it has started."""
        session = self._session
        return int(session.info["bits"]) if session is not None and "bits" in session.info else None

    def describe(self) -> str:
        session = self._session
        program = self.command[0] if self.command else "the 32-bit helper"
        if session is None or not session.alive:
            return f"{program} (not running)"
        info = session.info
        return (f"{program} (pid {info.get('pid')}: Python {info.get('python')} {info.get('bits')}-bit, "
                f"pyodbc {info.get('odbc') or 'unknown'})")  # fmt: skip

    def connect(self, connection_string: str, autocommit: bool = False, timeout: int = 0, **_: Any) -> Connection:
        session = self._started()
        reply = session.call("connect", text=connection_string, autocommit=autocommit, timeout=int(timeout or 0))
        return Connection(session, int(reply["connection"]), autocommit)

    def _started(self) -> Session:
        with self._lock:
            if self._session is not None and self._session.alive:
                return self._session
            session = Session(self._opener())
            try:
                info = session.call("hello", within=self.start_timeout_s)
            except TimeoutError as exc:
                session.close()
                raise not_started(str(exc)) from None
            except Error:
                session.close()
                code, tail = session.channel.wait(0), " | ".join(session.channel.tail())
                raise not_started(f"it exited with code {code}{f': {tail}' if tail else ''}") from None
            if info.get("protocol") != PROTOCOL:
                session.close()
                spoken = info.get("protocol")
                raise not_started(
                    f"it speaks protocol {spoken} and this server speaks {PROTOCOL}; reinstall sage100-mcp"
                )
            session.info = info
            self._session = session
            self.starts += 1
        logger.info("Started the 32-bit driver helper: %s", self.describe())
        return session

    def shutdown(self) -> None:
        """Stop the helper; the next connect starts another."""
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()
