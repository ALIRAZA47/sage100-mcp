"""The 32-bit driver helper as a child process, serving the driver emulator (docs/decisions.md D67).

    python tests/bridge_host.py <MAS90 directory> [<capability profile>] [<table>=<seconds> ...]

It registers a standard emulated install (ABC, EMP, PCC) at the directory, with the given tables made slow,
then runs helper.main with the emulator where pyodbc would be. So the stdin and stdout handling, exit at
end of input, and stderr are the helper's own.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sage100_mcp.bridge import helper
from tests import emulator
from tests.providex_fixture import profile


def main(argv: list[str]) -> int:
    directory, *rest = argv
    name = rest.pop(0) if rest and "=" not in rest[0] else "pxplus_full"
    install = emulator.standard_install(directory, profile(name))
    for setting in rest:
        table, seconds = setting.split("=", 1)
        install.slow_tables[table] = float(seconds)
    print("emulated install ready", file=sys.stderr)  # the server logs the helper's stderr
    return helper.main(emulator)


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stderr.flush()
    os._exit(code)
