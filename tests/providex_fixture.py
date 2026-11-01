"""ProvideX test helpers: the capability fixtures, a ProvideX-typed catalog, and parsed statements."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any

from sqlglot import exp

from sage100_mcp.capabilities import Capabilities, FunctionSupport, load
from sage100_mcp.catalog.builder import LiveLayer, RawColumn, RawTable, assemble
from sage100_mcp.catalog.model import Catalog
from sage100_mcp.catalog.overlay import load_overlay
from sage100_mcp.policy import Policy, load_policy
from sage100_mcp.sql.bind import Bound, bind, number_placeholders
from sage100_mcp.sql.guard import check
from sage100_mcp.sql.resolve import resolve
from tests.paths import REPO
from tests.premium_fixture import live_layer

PROFILE_DIR = REPO / "tests" / "fixtures" / "capabilities"
PROFILE_NAMES = ("all_pass", "pxplus_full", "calvin_graham")


@cache
def profile(name: str) -> Capabilities:
    found = load(PROFILE_DIR / f"{name}.json")
    assert found is not None, f"capability fixture {name} does not load"
    return found


def custom(base: str = "all_pass", **changes: bool) -> Capabilities:
    """A fixture profile with some features switched; ``fn_NAME=False`` drops a function."""
    caps = profile(base)
    features = dict(caps.features)
    functions = dict(caps.functions)
    for name, value in changes.items():
        if name.startswith("fn_"):
            functions[name[3:]] = FunctionSupport(escaped=value, bare=False)
        else:
            features[name] = value
    return caps.model_copy(update={"features": features, "functions": functions})


@cache
def policy() -> Policy:
    return load_policy(None)


@cache
def catalog(database: str = "MAS_EMP") -> Catalog:
    """The pinned schema as the ProvideX driver reports it: dates are DATE, with no time part."""
    live = live_layer(database)
    tables = tuple(
        RawTable(
            t.name,
            tuple(
                RawColumn(c.name, "date" if c.type.startswith("datetime") else c.type, c.nullable) for c in t.columns
            ),
            t.key,
            None,
        )
        for t in live.tables
    )
    return assemble(
        LiveLayer(live.company, "providex", live.data_versions, live.built_at, tables), load_overlay(), policy()
    )


def parsed(sql: str, params: Sequence[Any] = ()) -> tuple[exp.Query, list[Bound]]:
    """``sql`` guarded, resolved against MAS_EMP and with its ? markers numbered, as run_query does."""
    checked = check(sql, dialect="tsql")
    resolved = resolve(checked, catalog(), policy())
    values = bind(list(params), checked.placeholders)
    number_placeholders(resolved.tree)
    return resolved.tree, values
