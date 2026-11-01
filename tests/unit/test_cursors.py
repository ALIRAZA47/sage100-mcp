"""run_query paging (SPEC.md §13.3): signed cursors, page snapshots, and paging over the fake backend."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sage100_mcp import principals
from sage100_mcp.backends.base import RawResult
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.http.cursors import MAX_SNAPSHOTS, TTL_S, Cursors, Position, Snapshot, query_key, rows_digest
from sage100_mcp.principals import Principal
from sage100_mcp.query import next_page, run_query
from sage100_mcp.runtime import Runtime
from tests.fake_backend import FakeExecutor, fake_runtime, result

SQL = "SELECT CustomerNo, CreditLimit FROM AR_Customer"
KEY = query_key(SQL, None, "auto", "ansi")
ALICE = Principal("token:a1", "alice (a1)", transport="http")


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def refused(error: pytest.ExceptionInfo[SageError], fragment: str) -> None:
    assert error.value.code == ErrorCode.CURSOR_INVALID
    assert fragment in error.value.message
    assert error.value.hint


@pytest.fixture
def cursors(tmp_path: Path) -> Cursors:
    return Cursors(tmp_path / "tmp", clock=Clock())


def test_a_cursor_reads_back_what_it_was_issued_with(cursors: Cursors) -> None:
    token = cursors.issue("token:a1", "EMP", KEY, 1000, shown="abc")
    position = cursors.read(token, principal="token:a1", query=KEY)
    assert (position.company, position.offset, position.snapshot, position.shown) == ("EMP", 1000, None, "abc")


def test_an_altered_cursor_is_refused(cursors: Cursors) -> None:
    token = cursors.issue("token:a1", "EMP", KEY, 1000, shown="abc")
    body, _, signature = token.partition(".")
    other = cursors.issue("token:a1", "EMP", KEY, 5, shown="abc").partition(".")[0]
    for forged in (f"{other}.{signature}", f"{body}.{signature[:-2]}AA", body, "", "not a cursor", f"{body}.x.y"):
        with pytest.raises(SageError) as error:
            cursors.read(forged, principal="token:a1", query=KEY)
        refused(error, "not valid")


def test_a_cursor_from_another_run_of_the_server_is_refused(cursors: Cursors, tmp_path: Path) -> None:
    token = cursors.issue("token:a1", "EMP", KEY, 1000, shown="abc")
    with pytest.raises(SageError) as error:
        Cursors(tmp_path / "other").read(token, principal="token:a1", query=KEY)
    refused(error, "before the server restarted")


def test_a_cursor_is_bound_to_its_principal_query_and_lifetime(tmp_path: Path) -> None:
    clock = Clock()
    cursors = Cursors(tmp_path, clock=clock)
    token = cursors.issue("token:a1", "EMP", KEY, 1000, shown="abc")
    with pytest.raises(SageError) as other_caller:
        cursors.read(token, principal="token:b2", query=KEY)
    refused(other_caller, "another caller")
    with pytest.raises(SageError) as other_query:
        cursors.read(token, principal="token:a1", query=query_key(SQL + " WHERE 1 = 1", None, "auto", "ansi"))
    refused(other_query, "different query")
    clock.now += TTL_S + 1
    with pytest.raises(SageError) as expired:
        cursors.read(token, principal="token:a1", query=KEY)
    refused(expired, "expired")


def test_the_query_key_covers_sql_params_mode_and_dialect() -> None:
    keys = {
        query_key(SQL, None, "auto", "ansi"),
        query_key(SQL, ["x"], "auto", "ansi"),
        query_key(SQL, None, "hybrid", "ansi"),
        query_key(SQL, None, "auto", "tsql"),
        query_key(SQL + " ", None, "auto", "ansi"),
    }
    assert len(keys) == 5
    assert query_key(SQL, [], "auto", "ansi") == query_key(SQL, None, "auto", "ansi")


def test_the_rows_digest_ignores_order_but_not_content() -> None:
    rows = [["C1", "10.00"], ["C2", "20.00"], ["C2", "20.00"]]
    assert rows_digest(rows) == rows_digest(list(reversed(rows)))
    assert rows_digest(rows) != rows_digest(rows[:2])
    assert rows_digest(rows) != rows_digest([["C1", "10.00"], ["C2", "20.01"], ["C2", "20.00"]])


def snapshot_of(cursors: Cursors, rows: list[list[object]], token: str = "t") -> tuple[Position, Snapshot]:
    position = Position(token, "token:a1", "EMP", KEY, 2, None, rows_digest(rows[:2]), 0)
    return position, cursors.save(position, rows, capped=False, head={"columns": []})


def test_a_snapshot_pages_and_expires(tmp_path: Path) -> None:
    clock = Clock()
    cursors = Cursors(tmp_path / "tmp", clock=clock)
    rows: list[list[object]] = [[f"C{n:04}", f"{n}.00", n % 2 == 0, None] for n in range(45_000)]
    position, snapshot = snapshot_of(cursors, rows)
    assert cursors.opened(position) is snapshot, "a retried first use reuses the snapshot it took"
    assert cursors.page(snapshot, 0, 3) == rows[:3]
    assert cursors.page(snapshot, 44_998, 10) == rows[44_998:]
    token = cursors.issue("token:a1", "EMP", KEY, 5, snapshot=snapshot)
    clock.now += TTL_S - 1
    assert cursors.opened(cursors.read(token, principal="token:a1", query=KEY)) is snapshot
    clock.now += 2
    with pytest.raises(SageError) as expired:
        cursors.read(token, principal="token:a1", query=KEY)
    refused(expired, "expired")
    assert cursors.opened(position) is None, "an expired snapshot is dropped"


def test_only_the_newest_snapshots_are_kept(tmp_path: Path) -> None:
    clock = Clock()
    cursors = Cursors(tmp_path / "tmp", clock=clock)
    tokens = []
    for n in range(MAX_SNAPSHOTS + 1):
        _, snapshot = snapshot_of(cursors, [["x"], ["y"], [str(n)]], token=f"t{n}")
        tokens.append(cursors.issue("token:a1", "EMP", KEY, 2, snapshot=snapshot))
        clock.now += 1
    with pytest.raises(SageError) as dropped:
        cursors.opened(cursors.read(tokens[0], principal="token:a1", query=KEY))
    refused(dropped, "no longer kept")
    newest = cursors.opened(cursors.read(tokens[-1], principal="token:a1", query=KEY))
    assert newest is not None
    assert cursors.page(newest, 2, 5) == [[str(MAX_SNAPSHOTS)]]
    cursors.close()


# --- paging through run_query, over the fake backend ---------------------------------------------------


def customers(count: int, credit: str = "100.00") -> RawResult:
    return result(
        [("CustomerNo", "varchar(20)"), ("CreditLimit", "decimal(13,2)")],
        [(f"C{n:04}", Decimal(credit)) for n in range(count)],
    )


@pytest.fixture
def emp(tmp_path: Path) -> tuple[Runtime, FakeExecutor]:
    return fake_runtime(tmp_path)


def test_pages_cover_the_result_once_and_query_the_database_twice(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = emp
    fake.results += [customers(25), customers(25)]
    first = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10)
    assert (first.row_count, first.truncated) == (10, True)
    assert first.next_cursor
    assert "Pass next_cursor for the next page" in first.warnings[-1]
    assert "SELECT TOP 11 " in fake.sent[-1][0]

    second = next_page(runtime, first.next_cursor, SQL, max_rows=10)
    assert "SELECT TOP 250001 " in fake.sent[-1][0], "the first use keeps up to max_pushdown_rows"
    assert second.sent_sql == [fake.sent[-1][0]]
    assert (second.row_count, second.truncated, second.as_of) == (10, True, second.as_of)
    assert second.warnings == ["RESULT_TRUNCATED: Rows 11-20 of 25 are shown. Pass next_cursor for the next page."]
    assert second.next_cursor

    sent = len(fake.sent)
    third = next_page(runtime, second.next_cursor, SQL, max_rows=10)
    assert len(fake.sent) == sent, "later pages read the snapshot"
    assert (third.row_count, third.truncated, third.next_cursor, third.sent_sql, third.warnings) == (
        5, False, None, [], []
    )  # fmt: skip
    assert third.as_of == second.as_of, "every page shows when the snapshot was read"
    assert [row[0] for row in first.rows + second.rows + third.rows] == [f"C{n:04}" for n in range(25)]
    assert next_page(runtime, second.next_cursor, SQL, max_rows=10).rows == third.rows, "pages can be read again"


def test_a_first_use_that_finds_other_rows_is_refused(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = emp
    fake.results += [customers(25), customers(25, credit="150.00")]
    first = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10)
    assert first.next_cursor
    with pytest.raises(SageError) as changed:
        next_page(runtime, first.next_cursor, SQL, max_rows=10)
    refused(changed, "rows changed since the first page")
    assert "Order by a unique key" in (changed.value.hint or "")


def test_a_first_use_that_finds_the_same_rows_reordered_pages_on(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = emp
    shuffled = customers(25)
    shuffled.rows[:10] = list(reversed(shuffled.rows[:10]))
    fake.results += [customers(25), shuffled]
    first = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10)
    assert first.next_cursor
    assert next_page(runtime, first.next_cursor, SQL, max_rows=10).rows[0][0] == "C0010"


def test_a_cursor_is_for_its_query_company_and_caller(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = emp
    fake.results += [customers(25)]
    token = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10).next_cursor
    assert token
    sent = len(fake.sent)
    with pytest.raises(SageError) as other_query:
        next_page(runtime, token, SQL + " WHERE CreditLimit > 0", max_rows=10)
    refused(other_query, "different query")
    with pytest.raises(SageError) as other_company:
        next_page(runtime, token, SQL, company="PCC")
    refused(other_company, "for company EMP, not PCC")
    with principals.acting_as(ALICE), pytest.raises(SageError) as other_caller:
        next_page(runtime, token, SQL)
    refused(other_caller, "another caller")
    assert len(fake.sent) == sent, "a refused cursor reaches no database"


def test_a_cursor_pages_its_own_company_whatever_is_active(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = emp
    fake.results += [customers(12), customers(12)]
    token = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10).next_cursor
    assert token
    runtime.set_active("PCC")
    page = next_page(runtime, token, SQL, company="emp")
    assert (page.company, page.row_count, page.truncated) == ("EMP", 2, False)


def test_a_complete_first_page_has_no_cursor(emp: tuple[Runtime, FakeExecutor]) -> None:
    runtime, fake = emp
    fake.results += [customers(10)]
    page = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10)
    assert (page.truncated, page.next_cursor) == (False, None)


def test_paging_stops_at_the_snapshot_limit(tmp_path: Path) -> None:
    runtime, fake = fake_runtime(tmp_path, limits={"max_pushdown_rows": 15})
    fake.results += [customers(40), customers(40)]
    first = run_query(runtime, runtime.resolve_company("EMP"), SQL, max_rows=10)
    assert first.next_cursor
    second = next_page(runtime, first.next_cursor, SQL, max_rows=10)
    assert "SELECT TOP 21 " in fake.sent[-1][0], "at least one more page is kept"
    assert (second.row_count, second.truncated, second.next_cursor) == (10, True, None)
    assert second.warnings[-1].startswith("RESULT_TRUNCATED: Paging stops after 20 rows")
