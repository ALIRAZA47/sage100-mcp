"""Tier 3 seed (SPEC.md §15.3): only on the self-hosted Windows runner that has Sage 100 installed."""

from __future__ import annotations

import sys

import pytest

from sage100_mcp.backends.drivers import PROVIDEX_DRIVER, check_driver, registered_drivers
from sage100_mcp.status import PROVIDEX_REMEDY

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")]


def test_sage_driver_is_loadable_by_this_process() -> None:
    check = check_driver(PROVIDEX_DRIVER, registered_drivers(), remedy=PROVIDEX_REMEDY)
    assert check.loadable, check.message
