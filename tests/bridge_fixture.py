"""The 32-bit driver helper in tests (docs/decisions.md D67).

ThreadChannel runs the helper on a thread of the test process, over two OS pipes, with the driver emulator
where pyodbc would be. The emulated installs a test registers stay visible to it, so every executor test
can run through the helper. Process-level behaviour (exit, kill, stderr) uses tests/bridge_host.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

from sage100_mcp.bridge.client import Bridge, ProcessChannel
from sage100_mcp.bridge.helper import Helper
from tests import emulator
from tests.paths import REPO

HOST = REPO / "tests" / "bridge_host.py"


class ThreadChannel:
    """The helper on a thread, serving ``odbc`` over two pipes."""

    def __init__(self, odbc: Any = emulator) -> None:
        requests_in, requests_out = os.pipe()
        replies_in, replies_out = os.pipe()
        self.writer: IO[bytes] = os.fdopen(requests_out, "wb")
        self.reader: IO[bytes] = os.fdopen(replies_in, "rb")
        self._helper_in = os.fdopen(requests_in, "rb")
        self._helper_out = os.fdopen(replies_out, "wb")
        self.helper = Helper(odbc, self._helper_in, self._helper_out)
        self._thread = threading.Thread(target=self._serve, name="sage100-test-helper", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self.helper.serve()
        finally:
            self._helper_in.close()
            with suppress(OSError, ValueError):
                self._helper_out.close()

    def close(self) -> None:
        with suppress(OSError, ValueError):
            self.writer.close()  # the helper reads the end of its input and returns
        self._thread.join(5)

    def wait(self, timeout: float) -> int | None:
        self._thread.join(timeout)
        return None if self._thread.is_alive() else 0

    def tail(self) -> list[str]:
        return []


def helper_process(bridge: Bridge) -> subprocess.Popen[bytes]:
    """The running helper's process, for a test that kills it or waits for it to exit."""
    session = bridge._session
    assert session is not None
    assert isinstance(session.channel, ProcessChannel)
    return session.channel.process


def thread_bridge(odbc: Any = emulator) -> Bridge:
    return Bridge(("in-process helper",), opener=lambda: ThreadChannel(odbc))


def process_bridge(directory: Path, *settings: str, **options: Any) -> Bridge:
    """The helper as a child process (tests/bridge_host.py) serving a standard install at ``directory``."""
    return Bridge((sys.executable, str(HOST), str(directory), *settings), **options)
