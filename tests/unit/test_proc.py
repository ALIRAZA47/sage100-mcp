"""The process figures sage_status and the soak driver report (SPEC.md §13.5, §15.7), read on every OS."""

from __future__ import annotations

from sage100_mcp import proc


def test_resident_memory_is_read() -> None:
    """The working set on Windows, RSS elsewhere: a Python process with the server loaded holds megabytes."""
    found = proc.resident_mb()
    assert found is not None
    assert 5 < found < 64 * 1024


def test_threads_and_uptime() -> None:
    assert proc.threads() >= 1
    assert proc.uptime_s() >= 0
