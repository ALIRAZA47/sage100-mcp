"""The 32-bit driver helper (docs/decisions.md D67) against the driver emulator.

What the executor gets through the helper must be what it gets from the driver itself: the same rows,
types, descriptions, catalog rows and errors. A slow read on one connection must not hold up another,
and a cancel must reach a running statement. If the helper stops, the next call must start a new one.
The executor and probe suites run through the helper as well, because conftest.py parametrizes
make_executor over both routes.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from sage100_mcp.bridge.client import Bridge, HelperFailure, InterfaceError, ProgrammingError
from sage100_mcp.capabilities import cache_path
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.runtime import Runtime
from sage100_mcp.server import build_server
from sage100_mcp.views.api import run_named
from tests import emulator
from tests.bridge_fixture import ThreadChannel, helper_process, process_bridge, thread_bridge
from tests.emulator.conftest import Installer, Maker, install_at, providex_runtime
from tests.paths import REPO

pytestmark = [pytest.mark.anyio]

PASSWORD = "reader-pass"
STATEMENTS = [
    ("SELECT CustomerNo, CustomerName, CreditLimit, DateEstablished FROM AR_Customer WHERE ARDivisionNo = ?", ["00"]),
    ("SELECT InvoiceNo, InvoiceDate, InvoiceDueDate, Balance FROM AR_OpenInvoice ORDER BY InvoiceNo", []),
    ("SELECT ItemCode, StandardUnitCost, TotalQuantityOnHand FROM CI_Item WHERE StandardUnitCost > ?", [1.5]),
    (
        "SELECT h.SalesOrderNo, d.LineKey, d.QuantityOrdered FROM {oj SO_SalesOrderHeader h LEFT OUTER JOIN "
        "SO_SalesOrderDetail d ON h.SalesOrderNo = d.SalesOrderNo} WHERE h.OrderDate >= {d '2024-01-01'}",
        [],
    ),
    ("SELECT ModuleCode FROM AR_Options", []),
]


@pytest.fixture
def driver() -> Callable[[], Any]:
    """Here each test picks its own route, so make_executor isn't run once per route as elsewhere."""
    return lambda: emulator


@pytest.fixture
def bridge() -> Iterator[Bridge]:
    bridge = thread_bridge()
    yield bridge
    bridge.shutdown()


def connect(odbc: Any, install: emulator.Install, company: str = "EMP", **keywords: str) -> Any:
    text = (f"Driver={{{emulator.DRIVER}}};UID=mcp_reader;PWD={keywords.pop('PWD', PASSWORD)};Company={company};"
            f"Directory={install.directory};Silent=1")  # fmt: skip
    return odbc.connect(text, autocommit=True, timeout=15)


def read(odbc: Any, install: emulator.Install, sql: str, params: list[Any]) -> tuple[list[Any], list[tuple[Any, ...]]]:
    connection = connect(odbc, install)
    try:
        cursor = connection.cursor()
        cursor.execute(sql, *params)
        return list(cursor.description), [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


# --- what crosses -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("sql", "params"), STATEMENTS, ids=[s[0][7:40] for s in STATEMENTS])
def test_rows_and_descriptions_cross_unchanged(
    make_install: Installer, bridge: Bridge, sql: str, params: list[Any]
) -> None:
    install = make_install()
    direct = read(emulator, install, sql, params)
    helped = read(bridge, install, sql, params)
    assert helped == direct
    assert direct[1]  # there were rows to compare
    for row in helped[1]:
        assert [type(value) for value in row] == [type(value) for value in direct[1][0]] or None in row


def test_catalog_rows_answer_by_name(make_install: Installer, bridge: Bridge) -> None:
    install = make_install()
    connection = connect(bridge, install)
    cursor = connection.cursor()
    tables = cursor.tables(table="AR_%").fetchall()
    assert "AR_Customer" in [row.table_name for row in tables]
    assert {row.table_type for row in tables} == {"TABLE"}
    columns = cursor.columns(table="AR_Customer").fetchall()
    assert columns[0].column_name == "ARDivisionNo"
    assert [row.ordinal_position for row in columns] == list(range(1, len(columns) + 1))
    keys = cursor.primaryKeys("AR_Customer").fetchall()
    assert [(row.column_name, row.key_seq) for row in keys] == [("ARDivisionNo", 1), ("CustomerNo", 2)]
    assert connection.getinfo(bridge.SQL_DRIVER_VER) == install.driver_version
    connection.close()
    assert install.open_connections == 0


def test_driver_errors_keep_their_class_and_arguments(make_install: Installer, bridge: Bridge) -> None:
    install = make_install()
    with pytest.raises(emulator.Error) as direct:
        connect(emulator, install, PWD="wrong")
    with pytest.raises(InterfaceError) as helped:
        connect(bridge, install, PWD="wrong")
    assert helped.value.args == direct.value.args
    sql = "SELECT c.CustomerNo FROM AR_Customer c INNER JOIN AR_OpenInvoice i ON c.CustomerNo = i.CustomerNo"
    with pytest.raises(emulator.Error) as direct_refusal:
        connect(emulator, install).cursor().execute(sql)
    with pytest.raises(ProgrammingError) as refusal:
        connect(bridge, install).cursor().execute(sql)  # a bare JOIN, which the driver's grammar lacks
    assert refusal.value.args == direct_refusal.value.args
    assert refusal.value.args[0] == "37000"


def test_a_value_without_a_wire_form_is_refused_here(make_install: Installer, bridge: Bridge) -> None:
    cursor = connect(bridge, make_install()).cursor()
    with pytest.raises(ProgrammingError, match="HY105"):
        cursor.execute("SELECT CustomerNo FROM AR_Customer WHERE CustomerNo = ?", object())


def test_a_closed_cursor_is_refused(make_install: Installer, bridge: Bridge) -> None:
    cursor = connect(bridge, make_install()).cursor()
    cursor.close()
    cursor._closed = False  # as if the caller kept using it
    with pytest.raises(ProgrammingError, match="closed"):
        cursor.execute("SELECT CustomerNo FROM AR_Customer")


# --- concurrency and cancel ------------------------------------------------------------------------------


def test_a_slow_read_does_not_hold_up_another_connection(make_install: Installer, bridge: Bridge) -> None:
    install = make_install(slow_tables={"AR_Customer": 1.5})
    slow = connect(bridge, install).cursor()
    worker = threading.Thread(target=slow.execute, args=("SELECT CustomerNo FROM AR_Customer",), daemon=True)
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    fast = connect(bridge, install, company="ABC").cursor()
    fast.execute("SELECT ModuleCode FROM AR_Options")
    assert fast.fetchall()
    assert time.monotonic() - started < 1.0
    worker.join(5)
    assert slow.fetchall()  # the slow one finished too


def test_cancel_reaches_a_running_statement(make_install: Installer, bridge: Bridge) -> None:
    install = make_install(slow_tables={"AR_Customer": 10.0})
    cursor = connect(bridge, install).cursor()
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            cursor.execute("SELECT CustomerNo FROM AR_Customer")
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    time.sleep(0.3)
    cursor.cancel()
    worker.join(5)
    assert time.monotonic() - started < 2.0
    assert outcome["error"].args[0] == "HY008"
    cursor.execute("SELECT ModuleCode FROM AR_Options")  # the connection still serves
    assert cursor.fetchone() is not None


def test_many_threads_share_one_helper(make_install: Installer, make_executor: Maker) -> None:
    install = make_install()
    bridge = thread_bridge()
    executor = make_executor(install, odbc=bridge, limits={"per_company_concurrency": {"providex": 3}})
    sql = "SELECT CustomerNo, CreditLimit FROM AR_Customer ORDER BY CustomerNo"
    expected = executor.query("EMP", sql, max_rows=500, timeout_s=10).rows
    found: list[Any] = []

    def run(company: str) -> None:
        found.append((company, executor.query(company, sql, max_rows=500, timeout_s=10).rows))

    threads = [threading.Thread(target=run, args=(code,)) for code in ["EMP", "ABC", "EMP"] * 6]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(found) == len(threads)
    assert all(rows == expected for company, rows in found if company == "EMP")
    assert bridge.starts == 1
    assert install.peak_open <= 2 * 3  # two companies, three connections each
    executor.close()


# --- faults ---------------------------------------------------------------------------------------------------


def test_a_fault_in_the_helper_is_not_a_driver_error(
    make_install: Installer, make_executor: Maker, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    executor = make_executor(make_install(), odbc=thread_bridge())
    executor.query("EMP", "SELECT ModuleCode FROM AR_Options", max_rows=1, timeout_s=10)

    def broken(self: Any, sql: str, *params: Any) -> Any:
        raise RuntimeError("the helper tripped over itself")

    monkeypatch.setattr(emulator.Cursor, "execute", broken)
    with pytest.raises(HelperFailure, match="RuntimeError: the helper tripped over itself"):
        executor.query("EMP", "SELECT ModuleCode FROM AR_Options", max_rows=1, timeout_s=10)
    assert "Traceback" in caplog.text
    assert "the helper tripped over itself" in caplog.text
    executor.close()


def test_closing_the_executor_stops_its_helper(make_install: Installer, make_executor: Maker) -> None:
    bridge = thread_bridge()
    executor = make_executor(make_install(), odbc=bridge)
    executor.query("EMP", "SELECT ModuleCode FROM AR_Options", max_rows=1, timeout_s=10)
    assert "(pid " in (executor.driver_host() or "")
    executor.close()
    assert executor.driver_host() == "in-process helper (not running)"


def test_the_32_bit_build_has_its_own_capability_cache(
    make_install: Installer, make_executor: Maker, isolated_home: Path
) -> None:
    class Bridge32(Bridge):
        bits = 32

    bridge = Bridge32(("helper",), opener=lambda: ThreadChannel())
    executor = make_executor(make_install("calvin_graham"), odbc=bridge)
    executor.query("EMP", "SELECT ModuleCode FROM AR_Options", max_rows=1, timeout_s=10)
    assert executor.capabilities_origin == "probed"
    assert cache_path(isolated_home, "4.40.0004", "-x86").is_file()
    assert not cache_path(isolated_home, "4.40.0004").exists()
    executor.close()


# --- the whole server through the helper ------------------------------------------------------------------------


def normal(value: Any) -> Any:
    """Tool output without what differs between two runs: timings, clock times and signed cursors."""
    if isinstance(value, dict):
        return {k: normal(v) for k, v in value.items()
                if "elapsed" not in k and k not in ("as_of", "next_cursor", "pid")}  # fmt: skip
    if isinstance(value, list):
        return [normal(v) for v in value]
    return value


async def answers(runtime: Runtime) -> list[Any]:
    grouped = "SELECT CustomerType, COUNT(*) AS n FROM AR_Customer GROUP BY CustomerType ORDER BY 1"
    calls: list[tuple[str, dict[str, Any]]] = [
        ("run_query", {"company": "EMP", "sql": "SELECT CustomerNo, CustomerName, CreditLimit FROM AR_Customer"}),
        ("run_query", {"company": "EMP", "sql": "SELECT CustomerNo FROM AR_Customer ORDER BY 1", "max_rows": 3}),
        ("run_query", {"company": "EMP", "mode": "hybrid", "sql": grouped}),
        ("describe_table", {"company": "EMP", "table": "AR_OpenInvoice"}),
        ("customer_lookup", {"company": "EMP", "query": "C000"}),
    ]
    found = []
    async with Client(build_server(runtime.loaded, runtime)) as client:
        for tool, arguments in calls:
            result = await client.call_tool(tool, arguments)
            assert result.is_error is False, result.content
            found.append(normal(result.structured_content))
    company = runtime.resolve_company("EMP")
    view = run_named(runtime, company, "ar_aging", {"as_of": "2024-12-31"})
    found.append((view.columns, view.rows))
    return found


async def test_the_server_answers_the_same_through_the_helper(tmp_path: Path) -> None:
    install = install_at(tmp_path, "calvin_graham")
    direct = providex_runtime(tmp_path / "direct", install)
    bridges: list[Bridge] = []

    def helper() -> Bridge:
        bridges.append(thread_bridge())
        return bridges[-1]

    helped = providex_runtime(tmp_path / "helped", install, odbc=helper)
    try:
        assert await answers(helped) == await answers(direct)
        async with Client(build_server(helped.loaded, helped)) as client:
            status = (await client.call_tool("sage_status", {})).structured_content
        assert status is not None
        assert status["backends"][0]["driver"]["helper"].startswith("in-process helper (pid ")
    finally:
        direct.close()
        helped.close()
    assert [bridge.describe() for bridge in bridges] == ["in-process helper (not running)"]


# --- the helper as a process ---------------------------------------------------------------------------------


@pytest.fixture
def mas90(tmp_path: Path) -> Path:
    install_at(tmp_path, "pxplus_full")  # the same layout the child process emulates
    return tmp_path / "MAS90"


def wait_for_exit(process: subprocess.Popen[bytes], seconds: float = 10.0) -> int | None:
    try:
        return process.wait(seconds)
    except subprocess.TimeoutExpired:
        return None


def test_the_helper_process_serves_and_exits_with_its_input(
    mas90: Path, make_executor: Maker, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = process_bridge(mas90)
    executor = make_executor(emulator.INSTALLS[str(mas90).lower()], odbc=bridge)
    with caplog.at_level(logging.WARNING, "sage100_mcp.bridge.client"):
        assert [c.code for c in executor.discover()] == ["ABC", "EMP", "PCC"]
        result = executor.query("EMP", "SELECT CustomerNo, CreditLimit FROM AR_Customer", max_rows=5, timeout_s=10)
    assert len(result.rows) == 5
    assert result.columns[1].type == "decimal(13,2)"
    assert bridge.bits == (64 if sys.maxsize > 2**32 else 32)
    assert f"Python {sys.version.split()[0]}" in bridge.describe()
    assert "emulated install ready" in caplog.text  # the helper's stderr reaches the server log
    process = helper_process(bridge)
    executor.close()
    assert wait_for_exit(process) == 0


def test_a_statement_still_running_does_not_keep_the_helper_alive(mas90: Path) -> None:
    bridge = process_bridge(mas90, "AR_Customer=30")
    cursor = connect(bridge, emulator.INSTALLS[str(mas90).lower()]).cursor()
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            cursor.execute("SELECT CustomerNo FROM AR_Customer")
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.5)
    process = helper_process(bridge)
    started = time.monotonic()
    bridge.shutdown()
    worker.join(10)
    assert wait_for_exit(process) == 0
    assert time.monotonic() - started < 3.0
    assert "stopped" in str(outcome["error"])


def test_a_helper_that_dies_is_replaced(mas90: Path, make_executor: Maker) -> None:
    bridge = process_bridge(mas90, "AR_Customer=30")
    executor = make_executor(emulator.INSTALLS[str(mas90).lower()], odbc=bridge)
    executor.query("EMP", "SELECT ModuleCode FROM AR_Options", max_rows=1, timeout_s=10)  # a pooled connection
    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            executor.query("EMP", "SELECT CustomerNo FROM AR_Customer", max_rows=1, timeout_s=60)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(1.0)
    helper_process(bridge).kill()  # as a driver crash would
    worker.join(10)
    error = outcome["error"]
    assert isinstance(error, SageError)
    assert error.code == ErrorCode.BACKEND_UNREACHABLE
    assert "helper stopped (exit code" in error.message
    # The pooled connection belonged to the dead helper: it is dropped, and a new helper serves the next call.
    assert executor.query("EMP", "SELECT ModuleCode FROM AR_Options", max_rows=1, timeout_s=10).rows
    assert bridge.starts == 2
    executor.close()


@pytest.mark.parametrize(
    ("command", "says"),
    [
        (("/nonexistent/sage100-mcp-odbc32.exe",), "could not start: /nonexistent/sage100-mcp-odbc32.exe: "),
        ((), "could not start: no 32-bit helper is installed"),
        ((sys.executable, "-c", "import sys; print('no pyodbc here', file=sys.stderr); sys.exit(3)"),
         "could not start: it exited with code 3: no pyodbc here"),
    ],
)  # fmt: skip
def test_a_helper_that_cannot_start(
    make_install: Installer, make_executor: Maker, command: tuple[str, ...], says: str
) -> None:
    executor = make_executor(make_install(), odbc=Bridge(command))
    with pytest.raises(SageError) as failed:
        executor.discover()
    assert failed.value.code == ErrorCode.DRIVER_NOT_INSTALLED
    assert says in failed.value.message
    assert "32-bit helper" in (failed.value.hint or "")


def test_a_helper_that_never_answers_is_stopped(make_install: Installer, make_executor: Maker) -> None:
    bridge = Bridge((sys.executable, "-c", "import time; time.sleep(60)"), start_timeout_s=1)
    executor = make_executor(make_install(), odbc=bridge)
    started = time.monotonic()
    with pytest.raises(SageError, match="no answer within 1 s"):
        executor.discover()
    assert time.monotonic() - started < 10


def test_a_helper_of_another_protocol_is_refused(make_install: Installer, make_executor: Maker) -> None:
    script = (
        "import sys; sys.path.insert(0, {src!r}); from sage100_mcp.bridge.helper import encode, read_message; "
        "m = read_message(sys.stdin.buffer); sys.stdout.buffer.write(encode({{'id': m['id'], 'result': "
        "{{'protocol': 99}}}})); sys.stdout.buffer.flush(); read_message(sys.stdin.buffer)"
    ).format(src=str(REPO / "src"))
    executor = make_executor(make_install(), odbc=Bridge((sys.executable, "-c", script)))
    with pytest.raises(SageError, match="speaks protocol 99 and this server speaks 1"):
        executor.discover()


def test_helper_stderr_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    script = "import sys; print('connect failed: UID=x;PWD=hunter2;Company=EMP', file=sys.stderr); sys.exit(3)"
    with caplog.at_level(logging.WARNING, "sage100_mcp.bridge.client"), pytest.raises(InterfaceError):
        Bridge((sys.executable, "-c", script)).connect("Driver={x}")
    assert "PWD=***" in caplog.text
    assert "hunter2" not in caplog.text


def test_the_real_helper_script_with_pyodbc() -> None:
    """helper.py as a 32-bit Python would run it: isolated, with the real pyodbc (no database needed)."""
    pyodbc = pytest.importorskip("pyodbc")
    from sage100_mcp.bridge import SCRIPT

    bridge = Bridge((sys.executable, "-I", str(SCRIPT)))
    try:
        with pytest.raises(bridge.Error) as failed:
            bridge.connect("Driver={No Such Driver 32};Server=x", autocommit=True, timeout=5)
        with pytest.raises(pyodbc.Error) as direct:
            pyodbc.connect("Driver={No Such Driver 32};Server=x", autocommit=True, timeout=5)
        assert type(failed.value).__name__ == type(direct.value).__name__
        assert failed.value.args == direct.value.args
        assert f"pyodbc {pyodbc.version}" in bridge.describe()
    finally:
        bridge.shutdown()
