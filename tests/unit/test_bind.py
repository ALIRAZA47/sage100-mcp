"""? markers keep their values when clauses move (SPEC.md §5.5 step 5)."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from sage100_mcp.sql.bind import number_placeholders, ordered_params


def numbered(sql: str) -> exp.Expr:
    tree = sqlglot.parse_one(sql, read="tsql")
    number_placeholders(tree)
    return tree


def test_markers_are_numbered_in_text_order_not_walk_order() -> None:
    """sqlglot attaches WITH after parsing its SELECT, so a tree walk visits the WITH's marker last."""
    tree = numbered("WITH c AS (SELECT a FROM t WHERE b = ?) SELECT a FROM c WHERE d = ?")
    walk = [node.meta["param"] for node in tree.walk(bfs=False) if isinstance(node, exp.Placeholder)]
    assert walk == [1, 0]
    assert ordered_params(tree, ["first", "second"], "duckdb") == ["first", "second"]


def test_numbering_counts_markers() -> None:
    tree = sqlglot.parse_one("SELECT a FROM t WHERE x = ? AND y IN (?, ?) GROUP BY a HAVING COUNT(*) > ?")
    assert number_placeholders(tree) == 4


def test_values_follow_a_rewritten_tree() -> None:
    tree = numbered("SELECT a FROM t WHERE x = ? AND y = ?")
    where = tree.args["where"].this
    swapped = exp.and_(where.expression.copy(), where.this.copy())  # y = ? now comes first
    tree.set("where", exp.Where(this=swapped))
    assert ordered_params(tree, ["x", "y"], "duckdb") == ["y", "x"]


def test_markers_survive_copies() -> None:
    tree = numbered("SELECT a FROM t WHERE x = ? AND y = ?").copy()
    assert ordered_params(tree, [1, 2], "tsql") == [1, 2]
    assert all(node.this is None for node in tree.find_all(exp.Placeholder))  # still written as ?
