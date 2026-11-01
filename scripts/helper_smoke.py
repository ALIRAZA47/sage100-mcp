"""The 32-bit driver helper on Windows, end to end, without Sage (docs/decisions.md D67).

    python scripts/helper_smoke.py --helper C:\\hostedtoolcache\\...\\x86\\python.exe   # a 32-bit Python with pyodbc
    python scripts/helper_smoke.py --helper build\\dist\\sage100-mcp\\odbc32\\sage100-mcp-odbc32.exe

Windows still ships 32-bit-only ODBC drivers: the Jet text driver is registered under WOW6432Node alone.
So this script runs the case the helper exists for, a driver this 64-bit process can't load:
1. It checks that the text driver is registered for 32-bit only, and that pyodbc in this process can't
   connect to it.
2. It starts the helper, which must report 32 bits.
3. Through the helper it reads a CSV file of typed columns, and runs a catalog call.
4. It checks that the helper exits once its input ends.

Exit status 0 means every step passed. CI runs it on windows-latest under 64-bit Python (.github/workflows).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sage100_mcp.backends.drivers import process_bitness, registered_drivers
from sage100_mcp.bridge import helper_command
from sage100_mcp.bridge.client import Bridge, ProcessChannel

SCHEMA = """[smoke.csv]
ColNameHeader=True
Format=CSVDelimited
DateTimeFormat=yyyy-mm-dd
Col1=Code Text Width 10
Col2=Amount Currency
Col3=Day DateTime
"""
CSV = "Code,Amount,Day\nC0001,1234.50,2026-10-31\nC0002,-0.25,2024-02-29\n"


def text_driver() -> str | None:
    """The name of a text driver registered for 32-bit and not for 64-bit."""
    drivers = registered_drivers() or []
    names = {d.name for d in drivers if d.bitness == 32 and "text" in d.name.lower()}
    names -= {d.name for d in drivers if d.bitness == 64}
    return min(names) if names else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--helper", required=True, help="the helper .exe, or a 32-bit python.exe with pyodbc")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or process_bitness() != 64:
        print("helper_smoke.py needs 64-bit Python on Windows", file=sys.stderr)
        return 2
    driver = text_driver()
    if driver is None:
        print("no 32-bit-only text driver is registered on this host", file=sys.stderr)
        return 1
    print(f"32-bit-only driver: {driver}")
    with tempfile.TemporaryDirectory(prefix="sage100-helper-") as folder:
        (Path(folder) / "schema.ini").write_text(SCHEMA, encoding="ascii")
        (Path(folder) / "smoke.csv").write_text(CSV, encoding="ascii")
        text = f"Driver={{{driver}}};DBQ={folder};"
        import pyodbc

        try:
            pyodbc.connect(text, autocommit=True).close()
            print("pyodbc in this 64-bit process loaded the 32-bit driver, so this host can't show the helper's use")
            return 1
        except pyodbc.Error as exc:
            print(f"this 64-bit process can't load it, as expected: {exc.args[0]}")

        bridge = Bridge(helper_command(args.helper))
        try:
            connection = bridge.connect(text, autocommit=True)
            print(f"helper: {bridge.describe()}")
            if bridge.bits != 32:
                print(f"the helper is {bridge.bits}-bit, not 32-bit", file=sys.stderr)
                return 1
            cursor = connection.cursor()
            cursor.execute("SELECT Code, Amount, Day FROM [smoke.csv] ORDER BY Code")
            rows = [tuple(row) for row in cursor.fetchall()]
            print(f"rows: {rows}")
            print(f"description: {[(d[0], d[1].__name__) for d in cursor.description or ()]}")
            expected = [("C0001", Decimal("1234.5"), datetime(2026, 10, 31)),
                        ("C0002", Decimal("-0.25"), datetime(2024, 2, 29))]  # fmt: skip
            if [(code, Decimal(amount).normalize(), day) for code, amount, day in rows] != [
                (code, amount.normalize(), day) for code, amount, day in expected
            ]:
                print(f"expected {expected}", file=sys.stderr)
                return 1
            tables = [row.table_name for row in cursor.tables().fetchall()]
            print(f"tables: {tables}")
            if not any("smoke" in name.lower() for name in tables):
                print("the catalog call did not list smoke.csv", file=sys.stderr)
                return 1
            connection.close()
            session = bridge._session
            assert session is not None
            assert isinstance(session.channel, ProcessChannel)
            process = session.channel.process
        finally:
            bridge.shutdown()
        code = process.wait(10)
        print(f"helper exited with {code} once its input ended")
        return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
