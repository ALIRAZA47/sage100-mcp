"""Per-principal state (SPEC.md §9.3, §10.3): the active company, company scopes and the rate limit."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from sage100_mcp import principals
from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.principals import ACTIVE_FOR_S, LOCAL, Principal, Principals
from tests.fake_backend import fake_runtime

ALICE = Principal("token:a1", "alice (a1)", transport="http")
BOB = Principal("token:b2", "bob (b2)", transport="http")
EMP_ONLY = Principal("token:e3", "emp-only (e3)", companies=frozenset({"EMP"}), transport="http")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_each_principal_keeps_its_own_active_company() -> None:
    state = Principals(30)
    state.set_active(ALICE, "EMP")
    state.set_active(BOB, "PCC")
    assert (state.active(ALICE), state.active(BOB), state.active(LOCAL)) == ("EMP", "PCC", None)
    state.set_active(ALICE, None)
    assert state.active(ALICE) is None
    assert state.active(BOB) == "PCC"


def test_an_http_active_company_lapses_after_eight_hours() -> None:
    clock = Clock()
    state = Principals(30, clock=clock)
    state.set_active(ALICE, "EMP")
    state.set_active(LOCAL, "EMP")
    clock.now += ACTIVE_FOR_S
    assert state.active(ALICE) == "EMP"
    clock.now += 1
    assert state.active(ALICE) is None
    assert state.active(LOCAL) == "EMP", "over stdio it lasts as long as the process"


def test_an_active_company_outside_the_principals_scope_is_ignored() -> None:
    state = Principals(30)
    state.set_active(EMP_ONLY, "PCC")
    assert state.active(EMP_ONLY) is None
    state.set_active(EMP_ONLY, "EMP")
    assert state.active(EMP_ONLY) == "EMP"


def test_the_rate_limit_counts_calls_per_principal_over_a_sliding_minute() -> None:
    clock = Clock()
    state = Principals(3, clock=clock)
    for _ in range(3):
        state.admit(ALICE)  # at 0, 10 and 20 s
        clock.now += 10
    with pytest.raises(SageError) as refused:
        state.admit(ALICE)
    assert refused.value.code == ErrorCode.RATE_LIMITED
    assert refused.value.retry_after_s == 30  # the first call was 30 s ago; it leaves the window in 30 s
    assert "30 s" in (refused.value.hint or "")
    state.admit(BOB)  # another principal has its own budget
    clock.now += 30
    state.admit(ALICE)  # the first call has left the window
    with pytest.raises(SageError):
        state.admit(ALICE)


def test_a_refused_call_does_not_use_up_the_budget() -> None:
    clock = Clock()
    state = Principals(1, clock=clock)
    state.admit(ALICE)
    for _ in range(5):
        with pytest.raises(SageError):
            state.admit(ALICE)
    clock.now += 60
    state.admit(ALICE)


def test_retry_after_is_at_least_one_second() -> None:
    clock = Clock()
    state = Principals(1, clock=clock)
    state.admit(ALICE)
    clock.now += 59.9
    with pytest.raises(SageError) as refused:
        state.admit(ALICE)
    assert refused.value.retry_after_s == 1
    assert refused.value.envelope().retry_after_s == 1


@pytest.mark.anyio
async def test_worker_threads_act_as_the_caller() -> None:
    with principals.acting_as(ALICE):
        assert await anyio.to_thread.run_sync(principals.current) == ALICE
    assert principals.current() == LOCAL


def test_a_scoped_principal_sees_only_its_companies(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path, companies={"default": "PCC"})
    assert sorted(runtime.visible_companies()) == ["EMP", "PCC"]
    with principals.acting_as(EMP_ONLY):
        assert sorted(runtime.visible_companies()) == ["EMP"]
        # The configured default is out of scope, so the one company in scope is used.
        assert runtime.resolve_company(None).code == "EMP"
        with pytest.raises(SageError) as refused:
            runtime.resolve_company("PCC")
        assert refused.value.code == ErrorCode.COMPANY_NOT_FOUND
        assert refused.value.choices == ["EMP"]
    assert runtime.resolve_company(None).code == "PCC"


def test_set_company_is_per_principal(tmp_path: Path) -> None:
    runtime, _ = fake_runtime(tmp_path)
    with principals.acting_as(ALICE):
        runtime.set_active("EMP")
        assert runtime.active_company == "EMP"
    with principals.acting_as(BOB):
        assert runtime.active_company is None
        with pytest.raises(SageError) as required:
            runtime.resolve_company(None)
        assert required.value.code == ErrorCode.COMPANY_REQUIRED
        runtime.set_active("PCC")
    with principals.acting_as(ALICE):
        assert runtime.resolve_company(None).code == "EMP"
    assert runtime.active_company is None, "stdio is a principal of its own"
