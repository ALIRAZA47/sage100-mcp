"""§15.8 on a real Sage install: layer 2 is checked with Sage's own driver, outside the server."""

from __future__ import annotations

import pytest

from sage100_mcp.backends.providex import ProvideXExecutor
from sage100_mcp.runtime import Runtime
from tests.windows.conftest import SAGE_RUNNER

pytestmark = SAGE_RUNNER


def test_the_sage_driver_itself_refuses_writes(runtime: Runtime) -> None:
    """The UPDATE matches no row, so even a driver that took writes would change nothing."""
    company = runtime.resolve_company(None)
    executor = runtime.executor(company)
    assert isinstance(executor, ProvideXExecutor)
    connection = executor.odbc.connect(executor.connection_string(company.code), autocommit=True, timeout=15)
    try:
        with pytest.raises(executor.odbc.Error):
            connection.cursor().execute(
                "UPDATE AR_Customer SET CreditLimit = CreditLimit WHERE CustomerNo = 'NO-SUCH-CUSTOMER'"
            )
    finally:
        connection.close()
