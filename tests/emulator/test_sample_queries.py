"""The M2 gate on the emulator (SPEC.md §18): 50 sample queries right under each driver profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.emulator.conftest import install_at, providex_runtime
from tests.providex_fixture import PROFILE_NAMES
from tests.sample_queries import run_samples


@pytest.mark.parametrize("name", PROFILE_NAMES)
def test_fifty_samples_match_the_oracle(name: str, tmp_path: Path) -> None:
    runtime = providex_runtime(tmp_path / "home", install_at(tmp_path, name))
    try:
        outcomes = run_samples(runtime, runtime.resolve_company("EMP"))
    finally:
        runtime.close()
    failures = [f"{o.sample.name} ({o.mode}): {o.detail}" for o in outcomes if not o.passed]
    assert not failures, "\n".join(failures)
    hybrid = [o.sample.name for o in outcomes if o.mode == "hybrid"]
    assert len(outcomes) == 50
    assert len(hybrid) >= 15
    assert {o.sample.name for o in outcomes if o.sample.always_hybrid} <= set(hybrid)
    if name == "all_pass":  # a driver that runs everything still leaves only the always-hybrid ones local
        assert set(hybrid) == {o.sample.name for o in outcomes if o.sample.always_hybrid}
