"""Discovery and admin tools (SPEC.md §7.4, §8, §11.1): companies, tables, columns, catalog state.

Each function returns the tool's structured result; ``*_text`` renders the compact text that goes
with it for clients that read only text.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from sage100_mcp.catalog.model import Catalog, Semantic, Table
from sage100_mcp.catalog.search import search
from sage100_mcp.companies import Company
from sage100_mcp.errors import ErrorCode, ErrorEnvelope, SageError
from sage100_mcp.formatting import text_fields, text_table
from sage100_mcp.runtime import CatalogStatus, Runtime

# --- list_companies / set_company ----------------------------------------------------------------


class CompanyInfo(BaseModel):
    code: str
    name: str | None
    backend: str = Field(description="The backend's name in config.yaml")
    backend_type: Literal["providex", "sqlserver"]
    active: bool
    modules: list[str] | None = Field(description="Installed modules; null until the profile is read")
    data_versions: dict[str, str] = Field(description="DataVersion per module, from <MOD>_Options")
    current_periods: dict[str, str] = Field(description="Current fiscal year-period per module, such as 2024-12")
    divisions: dict[str, bool] = Field(description="Whether AR/AP divisions are enabled")
    catalog_status: CatalogStatus
    catalog_error: ErrorEnvelope | None = None
    tables: int | None = Field(description="Tables visible in the catalog; null until it is built")


class CompanyList(BaseModel):
    companies: list[CompanyInfo]
    active: str | None
    default: str | None
    unavailable_backends: list[str] = Field(description="Backends that could not be reached; see sage_status")


class ActiveCompany(BaseModel):
    active: str
    name: str | None
    previous: str | None


def _company_info(runtime: Runtime, company: Company) -> CompanyInfo:
    state = runtime.catalogs.get(company.code)
    profile = state.profile if state else None
    catalog = state.catalog if state else None
    return CompanyInfo(
        code=company.code,
        name=company.name,
        backend=company.backend,
        backend_type="providex" if runtime.backends[company.backend].kind == "providex" else "sqlserver",
        active=company.code == runtime.active_company,
        modules=list(profile.modules) if profile else None,
        data_versions=dict(profile.data_versions) if profile else {},
        current_periods=dict(profile.current_periods) if profile else {},
        divisions=dict(profile.divisions) if profile else {},
        catalog_status=runtime.status_of(company.code),
        catalog_error=state.error.envelope() if state and state.error else None,
        tables=len(catalog.tables) if catalog else None,
    )


def list_companies(runtime: Runtime) -> CompanyList:
    runtime.wait_for_discovery()
    return CompanyList(
        companies=[_company_info(runtime, company) for company in runtime.visible_companies().values()],
        active=runtime.active_company,
        default=runtime.config.companies.default,
        unavailable_backends=sorted(name for name, state in runtime.backends.items() if state.error is not None),
    )


def companies_text(result: CompanyList) -> str:
    rows = [
        [
            c.code + (" *" if c.active else ""),
            c.name or "",
            c.backend_type,
            ", ".join(c.modules or []),
            ", ".join(f"{m} {p}" for m, p in c.current_periods.items()),
            c.catalog_status,
        ]
        for c in result.companies
    ]
    parts = [text_table(["company", "name", "backend", "modules", "current periods", "catalog"], rows)]
    if not result.companies:
        parts = ["No companies are available. sage_status shows backend and configuration problems."]
    notes = []
    if result.active:
        notes.append(f"* active company: {result.active}")
    if result.unavailable_backends:
        notes.append(f"Unreachable backends: {', '.join(result.unavailable_backends)} (see sage_status).")
    return "\n".join([*parts, *notes])


def set_company(runtime: Runtime, requested: str) -> ActiveCompany:
    previous = runtime.active_company
    company = runtime.set_active(requested)
    return ActiveCompany(active=company.code, name=company.name, previous=previous)


# --- list_tables -----------------------------------------------------------------------------------


class TableInfo(BaseModel):
    table: str
    module: str
    purpose: str | None
    columns: int
    approx_rows: int | None = Field(description="Row count when the catalog was built")
    key: list[str]
    work: bool
    third_party: bool


class TableList(BaseModel):
    company: str
    module: str | None
    tables: list[TableInfo]
    total: int = Field(description="Tables that matched, before the limit")
    truncated: bool
    modules: dict[str, int] = Field(description="Matching tables per module, to narrow with module=")


def _table_info(table: Table) -> TableInfo:
    return TableInfo(
        table=table.name,
        module=table.module,
        purpose=table.purpose,
        columns=len(table.columns),
        approx_rows=table.row_hint,
        key=list(table.key),
        work=table.work,
        third_party=table.third_party,
    )


def list_tables(
    catalog: Catalog,
    *,
    module: str | None = None,
    include_work: bool = False,
    include_third_party: bool = True,
    limit: int = 200,
) -> TableList:
    wanted = module.strip().upper() if module else None
    candidates = [
        table
        for table in catalog.tables.values()
        if (include_work or not table.work) and (include_third_party or not table.third_party)
    ]
    modules = dict(sorted(Counter(table.module for table in candidates).items()))
    if wanted is not None and wanted not in modules:
        raise SageError(
            ErrorCode.MODULE_NOT_INSTALLED,
            f"Company {catalog.company} has no {wanted} tables.",
            hint=f"Modules here: {', '.join(modules) or 'none'}.",
            company=catalog.company,
            choices=list(modules) or None,
        )
    matching = [table for table in candidates if wanted is None or table.module == wanted]
    matching.sort(key=lambda t: (not t.curated, t.module, t.name.lower()))  # curated tables first
    return TableList(
        company=catalog.company,
        module=wanted,
        tables=[_table_info(table) for table in matching[:limit]],
        total=len(matching),
        truncated=len(matching) > limit,
        modules=modules,
    )


def tables_text(result: TableList) -> str:
    rows = [[t.table, t.module, t.purpose or "", t.columns, t.approx_rows] for t in result.tables]
    parts = [text_table(["table", "module", "purpose", "columns", "rows"], rows, limit=len(rows))]
    if result.truncated:
        parts.append(
            f"Showing {len(result.tables)} of {result.total} tables. Narrow with module= "
            f"({', '.join(f'{m} {n}' for m, n in result.modules.items())}), or use search_catalog."
        )
    return "\n".join(parts)


# --- describe_table ----------------------------------------------------------------------------------


class ColumnDetail(BaseModel):
    name: str
    type: str
    nullable: bool
    semantic: Semantic
    masked: bool
    tags: list[str]
    description: str | None = None
    values: dict[str, str] | None = Field(default=None, description="Code meanings, such as InvoiceType IN = Invoice")
    observed: list[str] | None = Field(default=None, description="Codes seen in real data, where values is unknown")


class JoinInfo(BaseModel):
    name: str
    table: str
    on: str = Field(description="A join condition ready to use in SQL")


class TableDetail(BaseModel):
    company: str
    table: str
    module: str
    purpose: str | None
    key: list[str]
    key_source: Literal["live", "overlay", "none"]
    approx_rows: int | None = Field(description="Row count when the catalog was built")
    work: bool
    third_party: bool
    requires_filter: bool = Field(description="A big history table: filter on a date range or a key")
    default_filters: list[str] = Field(description="Conditions most questions need, such as Balance <> 0")
    notes: list[str]
    columns: list[ColumnDetail]
    columns_shown: int
    columns_total: int
    joins: list[JoinInfo]


def find_table(catalog: Catalog, name: str, runtime: Runtime) -> Table:
    table = catalog.table(name.strip())
    if table is not None:
        return table
    rule = runtime.require_policy().denied_by(name.strip())
    if rule is not None:
        raise SageError(
            ErrorCode.TABLE_DENIED,
            f"{name} is not available: {rule.reason}",
            hint=f"Policy rule {rule.pattern} hides it (sage://policy).",
            company=catalog.company,
        )
    suggestions = catalog.suggest(name.strip())
    raise SageError(
        ErrorCode.UNKNOWN_TABLE,
        f"There is no table {name} in company {catalog.company}.",
        hint=f"Did you mean {', '.join(suggestions)}?" if suggestions else "Use search_catalog or list_tables.",
        company=catalog.company,
        choices=suggestions or None,
    )


def describe_table(catalog: Catalog, table: Table, *, all_columns: bool = False) -> TableDetail:
    shown = table.columns if all_columns else table.default_columns()
    return TableDetail(
        company=catalog.company,
        table=table.name,
        module=table.module,
        purpose=table.purpose,
        key=list(table.key),
        key_source=table.key_source,
        approx_rows=table.row_hint,
        work=table.work,
        third_party=table.third_party,
        requires_filter=table.requires_filter,
        default_filters=list(table.default_filters),
        notes=list(table.notes),
        columns=[
            ColumnDetail(
                name=c.name,
                type=c.type,
                nullable=c.nullable,
                semantic=c.semantic,
                masked=c.masked,
                tags=list(c.tags),
                description=c.description,
                values=dict(c.enum) if c.enum else None,
                observed=list(c.observed) if c.observed else None,
            )
            for c in shown
        ],
        columns_shown=len(shown),
        columns_total=len(table.columns),
        joins=[
            JoinInfo(
                name=j.name,
                table=j.table,
                on=" AND ".join(f"{table.name}.{here} = {j.table}.{there}" for here, there in j.on),
            )
            for j in table.joins
        ],
    )


def table_text(result: TableDetail) -> str:
    header = text_fields(
        [
            ("table", f"{result.table} ({result.module}, company {result.company})"),
            ("purpose", result.purpose),
            ("key", ", ".join(result.key) + (" (from the overlay)" if result.key_source == "overlay" else "")),
            ("rows", result.approx_rows),
            ("filter", "required: a date range or a key (big history table)" if result.requires_filter else None),
            ("usually", " AND ".join(result.default_filters)),
            ("notes", "\n".join(result.notes)),
        ]
    )
    rows = []
    for c in result.columns:
        meaning = c.description or ""
        if c.values:
            meaning = (meaning + " " if meaning else "") + "; ".join(f"{k}={v}" for k, v in c.values.items())
        elif c.observed:
            meaning = (meaning + " " if meaning else "") + "seen: " + ", ".join(c.observed)
        rows.append([c.name, c.type, c.semantic + (", masked" if c.masked else ""), meaning])
    parts = [header, text_table(["column", "type", "semantic", "meaning"], rows, limit=len(rows))]
    if result.columns_shown < result.columns_total:
        parts.append(f"{result.columns_shown} of {result.columns_total} columns; all_columns=true lists the rest.")
    if result.joins:
        parts.append("Joins:\n" + "\n".join(f"- {j.name}: {j.table} ON {j.on}" for j in result.joins))
    return "\n\n".join(parts)


# --- search_catalog ----------------------------------------------------------------------------------


class SearchHit(BaseModel):
    kind: Literal["table", "column"]
    table: str
    column: str | None
    module: str
    score: float
    purpose: str | None = Field(description="The table's purpose, from the overlay")
    type: str | None = None
    semantic: Semantic | None = None


class SearchResult(BaseModel):
    company: str
    text: str
    hits: list[SearchHit]


def search_catalog(catalog: Catalog, text: str, *, limit: int = 50) -> SearchResult:
    hits = []
    for hit in search(catalog, text, limit=limit):
        column = None
        if hit.column is not None:
            table = catalog.table(hit.table)
            column = table.column(hit.column) if table else None
        hits.append(
            SearchHit(
                kind=hit.kind,
                table=hit.table,
                column=hit.column,
                module=hit.module,
                score=hit.score,
                purpose=hit.purpose,
                type=column.type if column else None,
                semantic=column.semantic if column else None,
            )
        )
    return SearchResult(company=catalog.company, text=text, hits=hits)


def search_text(result: SearchResult) -> str:
    if not result.hits:
        return f"Nothing in company {result.company} matches {result.text!r}. Try other words, or list_tables."
    rows = [[h.table, h.column or "(table)", h.type or "", h.purpose or "", h.score] for h in result.hits]
    return text_table(["table", "column", "type", "table purpose", "score"], rows)


# --- refresh_catalog / test_connection ----------------------------------------------------------------


class CatalogRefresh(BaseModel):
    company: str
    status: CatalogStatus
    step: str
    message: str


def refresh_catalog(runtime: Runtime, company: Company) -> CatalogRefresh:
    state = runtime.refresh(company.code)
    status = runtime.status_of(company.code)
    message = (
        "Rebuilding from the database; the previous catalog answers until it finishes."
        if state.catalog is not None
        else "Building from the database; discovery tools answer meanwhile."
    )
    return CatalogRefresh(company=company.code, status=status, step=state.step, message=message)


class ConnectionTest(BaseModel):
    company: str
    backend: str
    database: str
    connect_ms: int
    query_ms: int
    driver_version: str | None
    server_version: str | None


def check_connection(runtime: Runtime, company: Company) -> ConnectionTest:
    """A fresh connection plus a one-row read of a Sage table (the test_connection tool)."""
    executor = runtime.executor(company)
    state = runtime.catalogs.get(company.code)
    modules = [
        m for m in (state.profile.modules if state and state.profile else ()) if re.fullmatch(r"[A-Z0-9]{2,3}", m)
    ]
    timeouts = runtime.config.limits.query_timeout_s
    timeout = timeouts.providex if executor.kind == "providex" else timeouts.sqlserver
    connect_ms, query_ms = executor.ping(company.database, executor.health_query(modules), timeout)
    return ConnectionTest(
        company=company.code,
        backend=company.backend,
        database=company.database,
        connect_ms=connect_ms,
        query_ms=query_ms,
        driver_version=executor.driver_version,
        server_version=executor.server_version,
    )
