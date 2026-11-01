"""SQL guard (SPEC.md §9.1) against the rejection corpus (§15.1), the accept corpus and fuzzing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlglot
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlglot import exp

from sage100_mcp.errors import ErrorCode, SageError
from sage100_mcp.sql import guard
from sage100_mcp.sql.guard import check, to_sql

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
REJECT: list[dict[str, Any]] = yaml.safe_load((FIXTURES / "reject.yaml").read_text(encoding="utf-8"))
ACCEPT: list[dict[str, Any]] = yaml.safe_load((FIXTURES / "accept.yaml").read_text(encoding="utf-8"))
CORPUS_MINIMUM = 300


def variants(sql: str) -> list[str]:
    """Spellings that must not change the verdict: case, leading comments, whitespace, a final semicolon."""
    return [
        sql,
        sql.lower(),
        sql.upper(),
        "/* note */ " + sql,
        "-- note\n" + sql,
        f"\n\t  {sql}  \n",
        sql.rstrip().rstrip(";") + ";",
    ]


CASES = [(case["code"], text) for case in REJECT for text in variants(case["sql"])]


def rejection(sql: str) -> SageError:
    with pytest.raises(SageError) as caught:
        check(sql)
    return caught.value


def assert_safe(sql: str) -> None:
    """Whatever the guard accepts must regenerate as exactly one read-only SELECT."""
    try:
        checked = check(sql)
    except SageError:
        return
    sent = to_sql(checked.tree)
    statements = [s for s in sqlglot.parse(sent, read="tsql") if s is not None]
    assert len(statements) == 1, sent
    assert isinstance(statements[0], (exp.Select, exp.SetOperation)), sent
    for node in statements[0].walk():
        assert not isinstance(node, guard._WRITES + guard._HINTS), sent
        assert not node.comments, sent  # comments are dropped; -- or /* inside a name or string is just text


# --- the exit-gate corpus ------------------------------------------------------------------


def test_guard_corpus_is_fully_rejected() -> None:
    """M1 exit gate: every case in the corpus, in every variant, is rejected."""
    assert len(CASES) >= CORPUS_MINIMUM, f"corpus has {len(CASES)} cases; §15.1 asks for {CORPUS_MINIMUM}"
    accepted = []
    for _, sql in CASES:
        try:
            check(sql)
        except SageError:
            continue
        accepted.append(sql)
    assert not accepted, f"{len(accepted)} of {len(CASES)} rejected cases were accepted: {accepted[:5]}"


@pytest.mark.parametrize("case", REJECT, ids=lambda case: case["sql"][:60])
def test_each_case_gets_its_code(case: dict[str, Any]) -> None:
    assert rejection(case["sql"]).code == ErrorCode(case["code"])


@pytest.mark.parametrize("case", [c for c in ACCEPT if "rejected" not in c], ids=lambda c: c["sql"][:60])
def test_read_only_statements_are_accepted(case: dict[str, Any]) -> None:
    checked = check(case["sql"])
    assert sorted(t.name for t in checked.tables()) == sorted(case["tables"])
    assert checked.placeholders == case.get("placeholders", 0)
    assert_safe(case["sql"])


def test_procedure_escape_is_refused_even_inside_a_string() -> None:
    """The spec checks {call on the raw text, before parsing, so a literal containing it is refused too."""
    case = next(c for c in ACCEPT if "rejected" in c)
    assert rejection(case["sql"]).code == ErrorCode(case["rejected"])


# --- behaviour the rest of the pipeline relies on ------------------------------------------


def test_comments_never_reach_the_driver() -> None:
    sent = to_sql(check("SELECT a /* hidden */ FROM t -- also hidden\nWHERE b = 1").tree)
    assert sent == "SELECT a FROM t WHERE b = 1"


def test_dangerous_text_inside_a_literal_stays_a_literal() -> None:
    sent = to_sql(check("SELECT 'x''; DROP TABLE t; --' AS s FROM t").tree)
    reparsed = sqlglot.parse_one(sent, read="tsql")
    assert [lit.this for lit in reparsed.find_all(exp.Literal)] == ["x'; DROP TABLE t; --"]


def test_t_sql_and_ansi_are_both_accepted() -> None:
    assert check("SELECT TOP 5 [a] FROM [t]").dialect == "tsql"
    assert check("SELECT TOP 5 [a] FROM [t]", dialect="ansi").dialect == "tsql"  # fallback reader
    assert to_sql(check("SELECT a FROM t LIMIT 5", dialect="ansi").tree) == "SELECT TOP 5 a FROM t"


def test_cte_names_are_not_catalog_tables() -> None:
    checked = check("WITH open_ar AS (SELECT * FROM AR_OpenInvoice) SELECT * FROM open_ar JOIN AR_Customer c ON 1 = 1")
    assert checked.cte_names == frozenset({"open_ar"})
    assert sorted(t.name for t in checked.tables()) == ["AR_Customer", "AR_OpenInvoice"]


def test_parse_errors_say_where() -> None:
    error = rejection("SELECT a FROM t WHERE (b = 1")
    assert error.code == ErrorCode.PARSE_ERROR
    assert "line 1" in error.message


def test_oversized_sql_is_refused() -> None:
    error = rejection("SELECT a FROM t WHERE a IN (" + ",".join(["1"] * 60000) + ")")
    assert error.code == ErrorCode.PARSE_ERROR
    assert "longer than" in error.message


def test_deep_nesting_is_refused_cleanly() -> None:
    error = rejection("SELECT " + "(" * 5000 + "1" + ")" * 5000 + " AS x FROM t")
    assert error.code == ErrorCode.PARSE_ERROR


# --- fuzzing (§15.1: "hypothesis fuzzing that generates SQL around known-bad nodes") ----------

FUZZ = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
TOKENS = [
    "SELECT", "FROM", "WHERE", "INTO", "DELETE", "INSERT", "UPDATE", "DROP", "EXEC", "EXECUTE", "MERGE", "SET",
    "USE", "WITH", "AS", "UNION", "ALL", "TOP", "1", "a", "b", "AR_Customer", "#t", "MAS_EMP.dbo.t", "sys.objects",
    ";", ",", "(", ")", "'", '"', "[", "]", "--", "/*", "*/", "\n", "{call p}", "@@VERSION", "@x", "?", ":p",
    "OPENROWSET('a','b','c')", "xp_cmdshell('x')", "NEXT VALUE FOR s", "OPTION (RECOMPILE)", "WITH (NOLOCK)",
    "FOR UPDATE", "WAITFOR DELAY '0:0:1'", "GO", "*", "=", "<>", "AND", "OR", "NOT", "NULL", "CASE", "WHEN", "THEN",
    "END", "JOIN", "ON", "GROUP BY", "ORDER BY",
]  # fmt: skip
BASES = [
    "SELECT a FROM AR_Customer",
    "SELECT a FROM AR_Customer WHERE b = 'x'",
    "WITH c AS (SELECT a FROM t) SELECT a FROM c",
]
SEPARATORS = [";", " ", "\n", "/**/", "--", "'", "*/", "; --", " UNION ", " WHERE 1 = 1 "]


@FUZZ
@given(st.text(max_size=200))
def test_arbitrary_text_is_safe(text: str) -> None:
    assert_safe(text)


@FUZZ
@given(st.lists(st.sampled_from(TOKENS), max_size=30).map(" ".join))
def test_token_soup_is_safe(sql: str) -> None:
    assert_safe(sql)


@FUZZ
@given(st.sampled_from(BASES), st.sampled_from(SEPARATORS), st.sampled_from([c["sql"] for c in REJECT]))
def test_bad_statements_spliced_onto_a_select_are_safe(base: str, separator: str, bad: str) -> None:
    assert_safe(base + separator + bad)


@FUZZ
@given(st.text(min_size=1, max_size=24), st.text(max_size=40))
def test_regenerated_sql_quotes_identifiers_and_strings(identifier: str, literal: str) -> None:
    """We send SQL regenerated from the tree, so quoting must round-trip whatever a name or value holds."""
    tree = (
        exp.select(exp.column(identifier, quoted=True))
        .from_("AR_Customer")
        .where(exp.column("CustomerNo").eq(exp.Literal.string(literal)))
    )
    sent = to_sql(tree)
    statements = [s for s in sqlglot.parse(sent, read="tsql") if s is not None]
    assert len(statements) == 1, sent
    reparsed = statements[0]
    assert isinstance(reparsed, exp.Select)
    assert reparsed.expressions[0].name == identifier
    assert [lit.this for lit in reparsed.find_all(exp.Literal)] == [literal]
