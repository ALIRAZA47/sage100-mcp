"""What a ProvideX driver build accepts (SPEC.md §5.2, §5.3): one profile per driver version.

The probe fills a profile on the first connect for each SQL_DRIVER_VER and caches it as
``<home>/capabilities/<driver_version>.json``; the planner sends a feature to the driver only when
the profile says it passed. Until a driver has been probed, BASELINE applies: the features the
spec's field evidence says work everywhere (§5.2), and nothing else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CAPABILITY_FORMAT = 1

# Feature name -> what it covers. The probe has one check for each (backends/providex_probe.py).
FEATURES: dict[str, str] = {
    "limit": "LIMIT n",
    "limit_offset": "LIMIT n OFFSET m",
    "top": "SELECT TOP n",
    "order_by_column": "ORDER BY a column",
    "order_by_position": "ORDER BY a select-list position",
    "order_by_desc": "ORDER BY ... DESC",
    "or": "OR between conditions",
    "compound_and": "several conditions joined by AND",
    "union": "UNION",
    "union_all": "UNION ALL",
    "comments": "comments in the statement",
    "parameters": "? parameter markers",
    "distinct": "SELECT DISTINCT",
    "group_by": "GROUP BY columns",
    "group_by_expression": "GROUP BY an expression",
    "having": "HAVING",
    "in_list": "IN (value, ...)",
    "between": "BETWEEN",
    "like": "LIKE",
    "is_null": "IS [NOT] NULL",
    "arithmetic": "+ - * / in expressions",
    "column_alias": "SELECT expression AS name",
    "table_alias": "FROM table alias",
    "comma_join": "FROM a, b WHERE a.x = b.x",
    "oj_left": "{oj a LEFT OUTER JOIN b ON (...)}",
    "oj_left_chain": "{oj a LEFT OUTER JOIN b ON (...) LEFT OUTER JOIN c ON (...)}",
    "oj_inner": "{oj a INNER JOIN b ON (...)}",
    "ij": "{IJ a INNER JOIN b ON ...}",
    "ij_nested": "{IJ (a INNER JOIN b ON ...) INNER JOIN c ON ...}",
    "case": "CASE WHEN ... END",
    "cast": "CAST(x AS type)",
    "count_distinct": "COUNT(DISTINCT x)",
    "subquery_in": "x IN (SELECT ...)",
    "exists": "EXISTS (SELECT ...)",
    "derived_table": "FROM (SELECT ...)",
    "date_literal": "{d 'yyyy-mm-dd'}",
    "iso_date_string": "a date column compared with 'yyyy-mm-dd'",
    "negative_literal": "a negative number literal",
    "aggregate_exact": "SUM of a decimal column returns an exact decimal",
    "primary_keys": "SQLPrimaryKeys metadata",
    "query_timeout": "SQL_ATTR_QUERY_TIMEOUT accepted",
}

# ODBC scalar functions (§5.1), probed escaped ({fn NAME(...)}) and bare.
FUNCTIONS: tuple[str, ...] = (
    "UCASE", "LCASE", "LEFT", "RIGHT", "LENGTH", "LOCATE", "LTRIM", "RTRIM", "SUBSTRING", "CONCAT", "REPLACE",
    "ABS", "ROUND", "FLOOR", "CEILING", "MOD", "TRUNCATE",
    "YEAR", "MONTH", "QUARTER", "WEEK", "DAYOFWEEK", "DAYOFMONTH", "CURDATE", "NOW", "TIMESTAMPADD", "TIMESTAMPDIFF",
    "IFNULL",
)  # fmt: skip

# Optional connection keywords (§4.3); each is sent only after the probe connected with it.
KEYWORDS: tuple[str, ...] = ("ReadOnly", "EnforceNullDate", "Compression", "SSL")

# §5.2's "Work" row plus {oj} LEFT OUTER, the only syntax the field evidence agrees on.
_BASELINE_FEATURES = (
    "distinct", "group_by", "having", "in_list", "between", "like", "is_null", "arithmetic", "compound_and",
    "comma_join", "table_alias", "oj_left", "date_literal", "aggregate_exact",
)  # fmt: skip
_VERSION = re.compile(r"[^A-Za-z0-9._-]")


class FunctionSupport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    escaped: bool = False  # {fn NAME(...)}
    bare: bool = False  # NAME(...)


class Capabilities(BaseModel):
    """One driver build's accepted SQL. Unknown features count as unsupported."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: int = CAPABILITY_FORMAT
    driver_version: str
    source: Literal["probe", "baseline", "profile"]
    probed_at: str | None = None
    table: str | None = Field(default=None, description="The table the probe read")
    keywords: dict[str, bool] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)
    functions: dict[str, FunctionSupport] = Field(default_factory=dict)
    notes: dict[str, str] = Field(default_factory=dict, description="The driver's message for each failed check")

    def supports(self, feature: str) -> bool:
        return self.features.get(feature, False)

    def function_form(self, name: str) -> Literal["escaped", "bare"] | None:
        """How to send scalar function ``name``: {fn ...} when that works, else bare, else not at all."""
        support = self.functions.get(name.upper())
        if support is None:
            return None
        if support.escaped:
            return "escaped"
        return "bare" if support.bare else None

    def accepts(self, keyword: str) -> bool:
        return self.keywords.get(keyword, False)

    def summary(self) -> dict[str, list[str]]:
        """Passed and failed checks, for sage_status and sage://capabilities."""
        passed = sorted(name for name, ok in self.features.items() if ok)
        failed = sorted(name for name, ok in self.features.items() if not ok)
        passed += sorted(f"fn:{name}" for name in self.functions if self.function_form(name))
        failed += sorted(f"fn:{name}" for name in self.functions if not self.function_form(name))
        return {"passed": passed, "failed": failed}


def baseline(driver_version: str = "unknown") -> Capabilities:
    return Capabilities(
        driver_version=driver_version, source="baseline", features=dict.fromkeys(_BASELINE_FEATURES, True)
    )


def cache_path(home: Path, driver_version: str, suffix: str = "") -> Path:
    """``suffix`` "-x86" keeps the 32-bit build's profile apart: the two builds of a version differ (§4.1, D67)."""
    return home / "capabilities" / f"{_VERSION.sub('_', driver_version) or 'unknown'}{suffix}.json"


def load(path: Path) -> Capabilities | None:
    """A cached or committed profile, or None if it is missing, unreadable or from another format."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != CAPABILITY_FORMAT:
            return None
        return Capabilities.model_validate(data)
    except (OSError, ValueError, ValidationError):
        return None


def save(path: Path, capabilities: Capabilities) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    text = json.dumps(capabilities.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class CapabilitySummary(BaseModel):
    driver_version: str
    source: Literal["probe", "baseline", "profile"] = Field(
        description="probe: tested on this driver; baseline: not probed yet, only what works everywhere"
    )
    probed_at: str | None
    table: str | None = Field(description="The table the probe read")
    passed: list[str] = Field(description="Features and fn:FUNCTIONS sent to the driver natively")
    failed: list[str] = Field(description="Features and functions run locally (hybrid) instead")
    accepted_keywords: list[str] = Field(description="Optional connection keywords the driver accepted")


def summarize(caps: Capabilities) -> CapabilitySummary:
    summary = caps.summary()
    return CapabilitySummary(
        driver_version=caps.driver_version,
        source=caps.source,
        probed_at=caps.probed_at,
        table=caps.table,
        passed=summary["passed"],
        failed=summary["failed"],
        accepted_keywords=sorted(k for k, ok in caps.keywords.items() if ok),
    )
