"""The emulator itself (SPEC.md §15.2): it refuses what its profile lacks, with the driver's own words."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from tests import emulator
from tests.emulator.conftest import Installer
from tests.providex_fixture import custom

STRING = r"Driver={MAS 90 4.0 ODBC Driver};UID=mcp_reader;PWD=reader-pass;Company=EMP;Directory={directory};Silent=1"


def connect(install: emulator.Install, **extra: str) -> Any:
    text = STRING.replace("{directory}", install.directory) + "".join(f";{k}={v}" for k, v in extra.items())
    return emulator.connect(text, autocommit=True)


def refused(install: emulator.Install, sql: str) -> str:
    cursor = connect(install).cursor()
    with pytest.raises(emulator.Error) as caught:
        cursor.execute(sql)
    return str(caught.value.args[1])


REFUSALS = [
    (
        "SELECT c.CustomerNo FROM AR_Customer c INNER JOIN AR_OpenInvoice i ON i.CustomerNo = c.CustomerNo",
        "Unexpected extra token: INNER",
    ),
    ("SELECT CustomerNo FROM AR_Customer;", "Unexpected extra token: ;"),
    ("SELECT [CustomerNo] FROM AR_Customer", "Expected lexical element not found: <identifier>"),
    (
        "SELECT CustomerNo FROM AR_Customer WHERE CustomerNo IN (SELECT CustomerNo FROM AR_OpenInvoice)",
        "Expected lexical element not found: <identifier>",
    ),
    ("SELECT UCASE(CustomerNo) FROM AR_Customer", "Expected lexical element not found: <identifier>"),
    (
        "SELECT CustomerNo FROM AR_Customer WHERE CustomerNo = 'A' OR CustomerName = 'B'",
        "Unexpected extra token: OR",
    ),
    ("SELECT TOP 5 CustomerNo FROM AR_Customer", "Unexpected extra token: 5"),
    ("SELECT CustomerNo FROM AR_Nope", "[FILEIO]Table is not accessible"),
]


@pytest.mark.parametrize(("sql", "message"), REFUSALS)
def test_refusals_use_the_drivers_words(make_install: Installer, sql: str, message: str) -> None:
    assert message in refused(make_install("calvin_graham"), sql)


def test_limit_where_the_profile_lacks_it(make_install: Installer) -> None:
    assert "Unexpected extra token: 5" in refused(
        make_install(custom("calvin_graham", limit=False)), "SELECT CustomerNo FROM AR_Customer LIMIT 5"
    )


def test_order_by_with_limit_is_never_emulated(make_install: Installer) -> None:
    assert "(emulator)" in refused(make_install("all_pass"), "SELECT CustomerNo FROM AR_Customer ORDER BY 1 LIMIT 5")


def test_escapes_are_accepted(make_install: Installer) -> None:
    cursor = connect(make_install("all_pass")).cursor()
    cursor.execute(
        "SELECT c.CustomerNo, {fn UCASE(c.CustomerName)} FROM {IJ AR_Customer c INNER JOIN AR_OpenInvoice i "
        "ON i.ARDivisionNo = c.ARDivisionNo AND i.CustomerNo = c.CustomerNo} WHERE i.InvoiceDate >= {d '2024-01-01'}"
    )
    assert cursor.fetchone() is not None


def test_connection_rules(make_install: Installer) -> None:
    install = make_install()
    text = STRING.replace("{directory}", install.directory)
    with pytest.raises(emulator.Error, match="HYC00"):
        emulator.connect(text)  # pyodbc's default autocommit=False sets an attribute the driver refuses
    with pytest.raises(emulator.Error, match="Silent=1"):
        emulator.connect(text.replace(";Silent=1", ""), autocommit=True)
    with pytest.raises(emulator.Error, match="Logon failed"):
        emulator.connect(text.replace("reader-pass", "nope"), autocommit=True)
    with pytest.raises(emulator.Error, match="Invalid connection attribute logfile"):
        emulator.connect(text + ";LogFile=x.log", autocommit=True)
    assert connect(install, ReadOnly="1")  # pxplus_full accepts ReadOnly


def test_providex_has_blanks_not_nulls(make_install: Installer) -> None:
    install = make_install()
    install.companies["EMP"].rows["AR_Customer"] = [{"ARDivisionNo": "00", "CustomerNo": "X1"}]
    cursor = connect(install).cursor()
    cursor.execute("SELECT CustomerName, CreditLimit, DateLastActivity FROM AR_Customer WHERE CustomerNo = 'X1'")
    assert tuple(cursor.fetchone()) == ("", Decimal("0.00"), None)


def test_a_translation_bug_fails_loudly(make_install: Installer, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DuckDB error is never dressed up as a driver refusal, which auto mode would quietly re-plan."""
    monkeypatch.setitem(emulator._DUCK_FUNCTIONS, "UCASE", "NO_SUCH_FUNCTION")
    message = refused(make_install("all_pass"), "SELECT {fn UCASE(CustomerName)} FROM AR_Customer")
    assert message.startswith("[HY000] emulator: DuckDB rejected")
