"""scripts/premium_readonly.sql (SPEC.md §16.4) stays in step with the policy it is generated from."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from typing import Any

import pytest

from sage100_mcp.policy import load_policy
from tests.paths import REPO

SCRIPT = runpy.run_path(str(REPO / "scripts" / "premium_readonly.py"), run_name="premium_readonly")
render: Callable[..., str] = SCRIPT["render"]
like: Callable[[str], str] = SCRIPT["like"]
main: Callable[[list[str]], int] = SCRIPT["main"]


def test_the_committed_script_is_current() -> None:
    assert main(["--check"]) == 0, "run: uv run python scripts/premium_readonly.py"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("PR_*", "pr[_]%"), ("SY_User*", "sy[_]user%"), ("*CreditCard*", "%creditcard%"), ("A?_x%", "a_[_]x[%]")],
)
def test_like(pattern: str, expected: str) -> None:
    assert like(pattern) == expected


def test_denies_follow_the_policy() -> None:
    policy = load_policy()
    sql = render(policy, "policy.yaml")
    assert "LOWER(t.name) LIKE N''pr[_]%''" in sql
    assert "DENY SELECT ON dbo." in sql
    assert "column" not in sql.split("SET NOCOUNT ON;")[1].lower()  # no column-level denies (decisions D20)
    payroll = render(policy.model_copy(update={"allow_modules": ["PR"]}), "policy.yaml")
    assert "pr[_]%" not in payroll  # opted-in payroll is not denied
    assert "%creditcard%" in payroll


def test_sqlcmd_variables_are_the_only_inputs() -> None:
    sql = render(load_policy(), "policy.yaml")
    variables: Any = {part.split(")")[0] for part in sql.split("$(")[1:]}
    assert variables == {"Login", "Password", "Companies"}
