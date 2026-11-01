"""The 32-bit driver helper (docs/decisions.md D67): Sage's 32-bit ODBC driver behind the 64-bit server.

A process loads only ODBC drivers of its own bitness, and the server's own dependencies no longer ship
32-bit builds (D3). So where Sage's driver is installed only as 32-bit, the 64-bit server starts this
helper under a 32-bit Python. It sends the helper the DB-API calls it would otherwise make on pyodbc:
connect, the timeout, cursor, execute, fetch, the catalog functions, getinfo, cancel and close. The helper makes those
calls and sends back what the driver returned. It never writes or changes SQL. It runs only the
statements the server's guard and planner sent, on connections the server opened.

The helper needs only the standard library and pyodbc. It runs as a script under any 32-bit CPython 3.9 or
later that has pyodbc, or as the frozen sage100-mcp-odbc32.exe that the MSI installs next to
sage100-mcp.exe.

The protocol runs over the helper's stdin (requests) and stdout (replies). Each message is a 4-byte
big-endian length, then that many bytes of JSON. Values JSON has no type for travel tagged, such as
{"$d": "1.50"} for a Decimal (see _tagged and _untagged). Each request has an id, which its reply
repeats. Each connection's calls run in order on that connection's own thread, so a slow read on one
connection doesn't hold up another. A cancel runs at once, alongside the call it cancels; SQLCancel is
made for that. The helper exits when its stdin ends, so it never outlives the server, even with a driver
call still running.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import queue
import signal
import struct
import sys
import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import IO, Any

PROTOCOL = 2  # 2: the timeout is set by its own request, when the server assigns it
MAX_MESSAGE = 64 * 2**20  # bytes; the server fetches in batches far smaller than this
CLOSED = "[sage100-mcp][32-bit helper]Attempt to use a closed connection or cursor"
_LENGTH = struct.Struct(">I")
_TYPES = {kind.__name__: kind for kind in (str, int, float, bool, bytes, bytearray, Decimal, date, datetime, time,
                                           uuid.UUID)}  # fmt: skip
_OTHER_TYPES: dict[str, type] = {}


class ProtocolError(Exception):
    """Bytes that are not a message: the other side has gone, or speaks something else."""


class _Closed(Exception):
    """A request for a connection or cursor that is closed (or never was)."""


# --- values and messages ------------------------------------------------------------------------------


def _tagged(value: Any) -> Any:
    """json's default hook: a value JSON has no type for, as a one-key object."""
    if isinstance(value, Decimal):
        return {"$d": str(value)}
    if isinstance(value, datetime):  # before date: a datetime is a date
        return {"$T": value.isoformat()}
    if isinstance(value, date):
        return {"$D": value.isoformat()}
    if isinstance(value, time):
        return {"$t": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, uuid.UUID):
        return {"$u": str(value)}
    if isinstance(value, type):  # a type code in cursor.description
        return {"$y": value.__name__}
    raise TypeError(f"no wire form for a value of type {type(value).__name__}")


def _type_named(name: str) -> type:
    """The Python type a description names; a stand-in with that name for one this side lacks."""
    found = _TYPES.get(name) or _OTHER_TYPES.get(name)
    if found is None:
        found = type(name, (), {})
        if len(_OTHER_TYPES) < 256:
            _OTHER_TYPES[name] = found
    return found


def _untagged(obj: dict[str, Any]) -> Any:
    """json's object hook: a tagged value back to its type; any other object as it is."""
    if len(obj) == 1:
        key, value = next(iter(obj.items()))
        if key == "$d":
            return Decimal(value)
        if key == "$T":
            return datetime.fromisoformat(value)
        if key == "$D":
            return date.fromisoformat(value)
        if key == "$t":
            return time.fromisoformat(value)
        if key == "$b":
            return base64.b64decode(value, validate=True)
        if key == "$u":
            return uuid.UUID(value)
        if key == "$y":
            return _type_named(value)
    return obj


def encode(message: dict[str, Any]) -> bytes:
    """A message as its length and JSON body."""
    body = json.dumps(message, default=_tagged, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(body) > MAX_MESSAGE:
        raise ProtocolError(f"a message of {len(body)} bytes is over the {MAX_MESSAGE}-byte limit")
    return _LENGTH.pack(len(body)) + body


def decode(body: bytes) -> dict[str, Any]:
    try:
        message = json.loads(body.decode("ascii"), object_hook=_untagged)
    except (UnicodeDecodeError, ValueError, ArithmeticError, TypeError) as exc:
        raise ProtocolError(f"not a message: {exc}") from None
    if not isinstance(message, dict):
        raise ProtocolError("not a message: expected a JSON object")
    return message


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """The next message, or None when the input ends between messages."""
    header = _read(stream, _LENGTH.size, between_messages=True)
    if header is None:
        return None
    (size,) = _LENGTH.unpack(header)
    if size > MAX_MESSAGE:
        raise ProtocolError(f"a message of {size} bytes is over the {MAX_MESSAGE}-byte limit")
    return decode(_read(stream, size, between_messages=False) or b"")


def _read(stream: IO[bytes], size: int, *, between_messages: bool) -> bytes | None:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            if between_messages and not data:
                return None
            raise ProtocolError("the input ended inside a message")
        data += chunk
    return data


def _plain(value: Any) -> Any:
    """An exception argument JSON can carry."""
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def _description(cursor: Any) -> Any:
    return [list(column) for column in cursor.description] if cursor.description else None


# --- the helper ---------------------------------------------------------------------------------------


class Helper:
    """Answers one server's requests: read from ``reader``, replies written to ``writer``."""

    def __init__(self, odbc: Any, reader: IO[bytes], writer: IO[bytes]) -> None:
        self.odbc = odbc
        self.reader = reader
        self.writer = writer
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._handles = 0
        self._connections: dict[int, _Connection] = {}
        self._cursors: dict[int, tuple[_Connection, Any]] = {}

    def serve(self) -> None:
        """Answer requests until the input ends; then close each connection once its last call returns."""
        try:
            while True:
                message = read_message(self.reader)
                if message is None:
                    return
                self._dispatch(message)
        finally:
            with self._lock:
                connections = list(self._connections.values())
            for connection in connections:
                connection.jobs.put(None)

    def _dispatch(self, message: dict[str, Any]) -> None:
        ident, op = message.get("id"), message.get("op")
        if op == "hello":
            self.run(ident, self._hello, message)
        elif op == "cancel":
            # Not queued behind the call it cancels, and not on this thread, which must keep reading.
            threading.Thread(target=self.run, args=(ident, self._cancel, message), daemon=True).start()
        elif op == "connect":
            connection = _Connection(self, self._new_handle())
            with self._lock:
                self._connections[connection.handle] = connection
            connection.start()
            connection.jobs.put((ident, message))
        else:
            owner = self._owner(message)
            if owner is None:
                self._send({"id": ident, "error": {"driver": "ProgrammingError", "args": ["08003", CLOSED]}})
            else:
                owner.jobs.put((ident, message))

    def run(self, ident: Any, work: Callable[[dict[str, Any]], Any], message: dict[str, Any]) -> bool:
        """Do ``work`` and reply with its result or its error; True when it succeeded."""
        try:
            result = work(message)
        except _Closed:
            self._send({"id": ident, "error": {"driver": "ProgrammingError", "args": ["08003", CLOSED]}})
            return False
        except self.odbc.Error as exc:
            error = {"driver": type(exc).__name__, "args": [_plain(arg) for arg in exc.args]}
            self._send({"id": ident, "error": error})
            return False
        except Exception as exc:
            self._send({"id": ident, "error": self._failure(exc, traceback.format_exc())})
            return False
        self._send({"id": ident, "result": result})
        return True

    @staticmethod
    def _failure(exc: BaseException, trace: str) -> dict[str, Any]:
        """Anything but a driver error: a fault in the helper, which the server reports as INTERNAL."""
        return {"failure": type(exc).__name__, "message": str(exc), "traceback": trace}

    def _send(self, message: dict[str, Any]) -> None:
        try:
            data = encode(message)
        except (TypeError, ValueError, ProtocolError) as exc:  # a value with no wire form, or too much at once
            data = encode({"id": message.get("id"), "error": self._failure(exc, "")})
        with self._write_lock:
            try:
                self.writer.write(data)
                self.writer.flush()
            except (OSError, ValueError):
                pass  # the server has gone; serve() returns once its input ends

    def _hello(self, message: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "python": platform.python_version(),
            "bits": struct.calcsize("P") * 8,
            "odbc": str(getattr(self.odbc, "version", "")) or None,  # pyodbc's version
            "pid": os.getpid(),
            "executable": sys.executable,
        }

    def _cancel(self, message: dict[str, Any]) -> None:
        self.cursor(message.get("cursor"))[1].cancel()

    # --- handles ------------------------------------------------------------------------------------------

    def _new_handle(self) -> int:
        with self._lock:
            self._handles += 1
            return self._handles

    def _owner(self, message: dict[str, Any]) -> _Connection | None:
        with self._lock:
            if "cursor" in message:
                kept = self._cursors.get(message["cursor"])
                return kept[0] if kept is not None else None
            return self._connections.get(message.get("connection"))  # type: ignore[arg-type]

    def keep(self, connection: _Connection, cursor: Any) -> int:
        handle = self._new_handle()
        with self._lock:
            self._cursors[handle] = (connection, cursor)
        return handle

    def cursor(self, handle: Any) -> tuple[_Connection, Any]:
        with self._lock:
            kept = self._cursors.get(handle)
        if kept is None:
            raise _Closed()
        return kept

    def drop_cursor(self, handle: Any) -> None:
        with self._lock:
            self._cursors.pop(handle, None)

    def forget(self, connection: _Connection) -> list[Any]:
        """Unregister ``connection``; its cursors, which the caller closes."""
        with self._lock:
            self._connections.pop(connection.handle, None)
            mine = [handle for handle, kept in self._cursors.items() if kept[0] is connection]
            return [self._cursors.pop(handle)[1] for handle in mine]


class _Connection:
    """One driver connection and the thread that makes its calls, in the order they came."""

    def __init__(self, helper: Helper, handle: int) -> None:
        self.helper = helper
        self.handle = handle
        self.jobs: queue.Queue[tuple[Any, dict[str, Any]] | None] = queue.Queue()
        self.connection: Any = None
        self._thread = threading.Thread(target=self._work, name=f"sage100-odbc-{handle}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:  # the server has gone
                self.helper.forget(self)
                if self.connection is not None:
                    with contextlib.suppress(Exception):
                        self.connection.close()
                return
            ident, message = job
            op = message.get("op")
            succeeded = self.helper.run(ident, self._perform, message)
            if op == "connect" and not succeeded:
                self.helper.forget(self)
                return
            if op == "close":
                return

    def _perform(self, message: dict[str, Any]) -> Any:
        op = message.get("op")
        if op == "connect":
            return self._connect(message)
        if op == "getinfo":
            return self.connection.getinfo(message["code"])
        if op == "timeout":  # pyodbc sets SQL_ATTR_CONNECTION_TIMEOUT now, and the query timeout per new cursor
            self.connection.timeout = int(message["seconds"])
            return None
        if op == "cursor":
            return {"cursor": self.helper.keep(self, self.connection.cursor())}
        if op == "close":
            for cursor in self.helper.forget(self):
                with contextlib.suppress(Exception):  # closing anyway
                    cursor.close()
            self.connection.close()
            return None
        handle = message.get("cursor")
        cursor = self.helper.cursor(handle)[1]
        if op == "execute":
            cursor.execute(message["sql"], *message.get("params", ()))
            return {"description": _description(cursor)}
        if op == "fetch":
            return {"rows": [list(row) for row in cursor.fetchmany(int(message["size"]))]}
        if op in ("tables", "columns", "primaryKeys"):
            getattr(cursor, op)(**message.get("arguments", {}))
            return {"description": _description(cursor)}
        if op == "close_cursor":
            self.helper.drop_cursor(handle)
            cursor.close()
            return None
        raise ValueError(f"unknown request {op!r}")

    def _connect(self, message: dict[str, Any]) -> dict[str, Any]:
        odbc = self.helper.odbc
        autocommit = bool(message.get("autocommit"))
        timeout = int(message.get("timeout") or 0)
        if timeout:  # the login timeout; without it the call is as the server makes it on a driver that refuses one
            self.connection = odbc.connect(message["text"], autocommit=autocommit, timeout=timeout)
        else:
            self.connection = odbc.connect(message["text"], autocommit=autocommit)
        return {"connection": self.handle}


# --- the program ----------------------------------------------------------------------------------------


def _binary(stream: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(stream.fileno(), os.O_BINARY)


def main(odbc: Any = None) -> int:
    """Serve the server that started this process, over stdin and stdout, until stdin ends.

    ``odbc`` is pyodbc unless a test passes the driver emulator.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is the server's to handle; the helper ends with its input
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())  # a stray print goes to stderr, not into the replies
    sys.stdout = sys.stderr
    requests = sys.stdin.buffer
    _binary(requests)
    _binary(replies)
    if odbc is None:
        try:
            import pyodbc
        except ImportError as exc:
            print(f"pyodbc can't be imported by {sys.executable}: {exc}", file=sys.stderr)
            return 3
        odbc = pyodbc
    try:
        Helper(odbc, requests, replies).serve()
    except ProtocolError as exc:
        print(f"stopped reading requests: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    code = main()
    sys.stderr.flush()
    os._exit(code)  # now: a driver call the server gave up on must not keep this process alive
