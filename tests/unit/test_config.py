from __future__ import annotations

import re
import string
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from sage100_mcp.config import (
    Config,
    ConfigError,
    ProvideXBackend,
    SqlServerBackend,
    default_config_path,
    default_home,
    load_config,
    load_config_or_defaults,
    parse_company_code,
)
from tests.paths import EXAMPLE_CONFIG, SPEC

WriteConfig = Callable[..., Path]

PROVIDEX = """
backends:
  prod:
    type: providex
    directory: \\\\SAGESRV\\Sage\\Sage 100\\MAS90
    credential: sage100-mcp/prod
"""


def load(write_config: WriteConfig, text: str, env: dict[str, str] | None = None) -> Config:
    return load_config(write_config(text), env=env or {}, must_exist=True).config


def rejected(write_config: WriteConfig, text: str, env: dict[str, str] | None = None) -> str:
    with pytest.raises(ConfigError) as caught:
        load(write_config, text, env)
    return str(caught.value)


# --- files and defaults ---------------------------------------------------------------


def test_missing_default_file_gives_defaults(isolated_home: Path) -> None:
    loaded = load_config(env={"SAGE100_MCP_HOME": str(isolated_home)})
    assert loaded.found is False
    assert loaded.path == isolated_home / "config.yaml"
    assert loaded.config == Config()
    assert loaded.config.backends == {}


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.yaml", env={}, must_exist=True)


def test_example_config_loads() -> None:
    config = load_config(EXAMPLE_CONFIG, env={}, must_exist=True).config
    prod, premium = config.backends["sage_prod"], config.backends["sage_premium"]
    assert isinstance(prod, ProvideXBackend)
    assert prod.directory == r"\\SAGESRV\Sage\Sage 100\MAS90"
    assert prod.driver_options == {"DirtyReads": 0, "BurstMode": 0, "CacheSize": 4}
    assert prod.cs is not None
    assert prod.cs.port == 20222
    assert isinstance(premium, SqlServerBackend)
    assert premium.server == r"SQLSRV\SAGE"
    assert config.companies.backend_map == {"ABC": "sage_prod", "EMP": "sage_premium"}
    assert config.limits.query_timeout_s.providex == 60
    assert (config.http.host, config.http.port) == ("127.0.0.1", 8765)


def test_example_config_matches_the_spec(write_config: WriteConfig) -> None:
    """docs/config.example.yaml is the §16.1 example plus comments and the deviations docs/decisions.md
    records; both must load to the same model."""
    blocks = re.findall(r"^```yaml\n(.*?)^```", SPEC.read_text(encoding="utf-8"), flags=re.S | re.M)
    spec_example = next(block for block in blocks if block.startswith("server:"))
    expected = load(write_config, spec_example)
    assert expected.views.inventory_available_formula == "onhand - so - bo - wo_required"
    views = expected.views.model_copy(update={"inventory_available_formula": "sage"})  # D40
    assert expected.model_copy(update={"views": views}) == load_config(EXAMPLE_CONFIG, env={}, must_exist=True).config


@pytest.mark.parametrize("text", ["", "# only a comment\n", "~\n"])
def test_empty_file_gives_defaults(write_config: WriteConfig, text: str) -> None:
    assert load(write_config, text) == Config()


def test_byte_order_mark_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes("\ufefflimits:\n  max_rows: 10\n".encode())
    assert load_config(path, env={}).config.limits.max_rows == 10


def test_invalid_yaml(write_config: WriteConfig) -> None:
    assert "not valid YAML" in rejected(write_config, "limits: [unclosed\n")


def test_top_level_must_be_a_mapping(write_config: WriteConfig) -> None:
    assert "top level must be a mapping" in rejected(write_config, "- a\n- b\n")


def test_duplicate_keys_are_rejected(write_config: WriteConfig) -> None:
    """A repeated section would otherwise silently replace the first, e.g. dropping an allowlist."""
    message = rejected(write_config, "companies:\n  allow: [ABC]\ncompanies:\n  hide: [DEF]\n")
    assert "duplicate key 'companies'" in message


def test_unknown_keys_are_rejected(write_config: WriteConfig) -> None:
    message = rejected(write_config, "limits:\n  max_row: 5\n")
    assert "limits.max_row" in message


def test_errors_name_the_location(write_config: WriteConfig) -> None:
    message = rejected(write_config, "limits:\n  max_rows: lots\n")
    assert "limits.max_rows" in message


def test_config_is_immutable() -> None:
    config = Config()
    with pytest.raises(ValidationError):
        config.limits.max_rows = 5  # type: ignore[misc]


# --- scalars stay text -----------------------------------------------------------------


def test_scalars_are_not_retyped_by_yaml(write_config: WriteConfig) -> None:
    """YAML 1.1 would read 001 as 1, YES as true and 19:00 as 1140."""
    config = load(
        write_config,
        PROVIDEX + "    performance_profile: {after_hours: 19:00-06:00}\ncompanies:\n  allow: [abc, 001, YES]\n",
    )
    assert config.companies.allow == ["ABC", "001", "YES"]
    backend = config.backends["prod"]
    assert isinstance(backend, ProvideXBackend)
    assert backend.performance_profile is not None
    assert backend.performance_profile.after_hours == "19:00-06:00"


def test_numbers_and_booleans_are_still_typed(write_config: WriteConfig) -> None:
    config = load(write_config, "limits:\n  max_rows: 25\nserver:\n  audit_redact_literals: no\n")
    assert config.limits.max_rows == 25
    assert config.server.audit_redact_literals is False


# --- company codes -----------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "code"), [("abc", "ABC"), (" emp ", "EMP"), ("001", "001"), ("A1b", "A1B")])
def test_company_codes_are_normalised(raw: str, code: str) -> None:
    assert parse_company_code(raw) == code


@pytest.mark.parametrize("raw", ["AB", "ABCD", "A-C", "", "ÄBC", "A C", "\u0131ab", "\u2003ABC", "\uff21\uff22\uff23"])
def test_bad_company_codes(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid company code"):
        parse_company_code(raw)


def test_database_name_gets_a_hint() -> None:
    with pytest.raises(ValueError, match="use the code 'EMP'"):
        parse_company_code("MAS_EMP")


def test_company_code_must_be_text() -> None:
    with pytest.raises(ValueError, match="must be text"):
        parse_company_code(1)


@given(st.text(max_size=6))
def test_company_code_property(raw: str) -> None:
    candidate = raw.strip().upper()
    valid = (
        raw.isascii() and len(candidate) == 3 and all(c in string.ascii_uppercase + string.digits for c in candidate)
    )
    if valid:
        assert parse_company_code(raw) == candidate
    else:
        with pytest.raises(ValueError, match="invalid company code"):
            parse_company_code(raw)


def test_company_rules(write_config: WriteConfig) -> None:
    assert "both allow and hide: ABC" in rejected(write_config, "companies: {allow: [ABC], hide: [abc]}\n")
    assert "default company DEF is hidden" in rejected(write_config, "companies: {default: DEF, hide: [DEF]}\n")
    assert "not in allow" in rejected(write_config, "companies: {default: XYZ, allow: [ABC]}\n")
    message = rejected(write_config, "companies: {backend_map: {ABC: nowhere}}\n")
    assert "undefined backend 'nowhere'" in message


# --- backends -----------------------------------------------------------------------------


def test_driver_options_are_canonical_and_defaulted(write_config: WriteConfig) -> None:
    backend = load(write_config, PROVIDEX + "    driver_options: {dirtyreads: 1}\n").backends["prod"]
    assert isinstance(backend, ProvideXBackend)
    assert backend.driver_options == {"DirtyReads": 1, "BurstMode": 0, "CacheSize": 4}
    assert backend.transport == "local"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ("{PWD: x}", "comes from the keyring entry"),
        ("{uid: 1}", "comes from the keyring entry"),
        ("{Silent: 0}", "is set by the server"),
        ("{RemotePVKIOHost: x}", "is set by the server"),
        ("{SID: 1}", "never sent"),
        ("{LegacyNULL: 1}", "never sent"),
        ("{Turbo: 1}", "unknown driver option"),
        ("{BurstMode: 2}", "BurstMode must be 0 or 1"),
        ("{CacheSize: 0}", "CacheSize must be at least 1"),
        ("{DirtyReads: 1, dirtyreads: 0}", "given twice"),
    ],
)
def test_driver_option_rules(write_config: WriteConfig, options: str, message: str) -> None:
    assert message in rejected(write_config, PROVIDEX + f"    driver_options: {options}\n")


def test_secrets_cannot_be_written_into_the_config(write_config: WriteConfig) -> None:
    message = rejected(write_config, PROVIDEX + "    password: hunter2\n")
    assert "prod.providex.password" in message
    assert "hunter2" not in message
    message = rejected(write_config, PROVIDEX + "    driver_options: {PWD: hunter2}\n")
    assert "keyring" in message
    assert "hunter2" not in message


def test_cs_transport_needs_an_endpoint(write_config: WriteConfig) -> None:
    assert "transport: cs needs a cs:" in rejected(write_config, PROVIDEX + "    transport: cs\n")


@pytest.mark.parametrize("credential", ["prod", "/prod", "sage100-mcp/", "a/b/c"])
def test_credential_must_be_a_keyring_reference(write_config: WriteConfig, credential: str) -> None:
    text = PROVIDEX.replace("sage100-mcp/prod", credential)
    assert "keyring entry" in rejected(write_config, text)


def test_backend_names(write_config: WriteConfig) -> None:
    assert "backend name 'sage-prod'" in rejected(write_config, PROVIDEX.replace("  prod:", "  sage-prod:"))


SQLSERVER = """
backends:
  premium:
    type: sqlserver
    server: SQLSRV\\SAGE
    credential: sage100-mcp/premium
"""


def test_sqlserver_defaults(write_config: WriteConfig) -> None:
    backend = load(write_config, SQLSERVER).backends["premium"]
    assert isinstance(backend, SqlServerBackend)
    assert (backend.database_pattern, backend.system_database, backend.encrypt) == ("MAS_{company}", "MAS_SYSTEM", True)


@pytest.mark.parametrize("pattern", ["MAS_ABC", "{company}_{company}", "MAS-{company}", "MAS_{company};DROP"])
def test_database_pattern(write_config: WriteConfig, pattern: str) -> None:
    assert "database_pattern" in rejected(write_config, SQLSERVER + f"    database_pattern: '{pattern}'\n")


def test_unknown_backend_type(write_config: WriteConfig) -> None:
    assert "backends.prod" in rejected(write_config, PROVIDEX.replace("type: providex", "type: odata"))


# --- http, limits, views -------------------------------------------------------------------


def test_http_rules(write_config: WriteConfig) -> None:
    assert "needs an oauth:" in rejected(write_config, "http: {auth: oauth}\n")
    assert "bind must be host:port" in rejected(write_config, "http: {bind: localhost}\n")
    assert "no path" in rejected(write_config, "http: {allowed_origins: ['https://claude.ai/app']}\n")
    assert "https:// URL" in rejected(write_config, "http: {auth: oauth, oauth: {issuer: 'http://x', audience: a}}\n")
    config = load(write_config, "http: {bind: '[::1]:9000', allowed_origins: ['HTTPS://Claude.ai/']}\n")
    assert (config.http.host, config.http.port) == ("::1", 9000)
    assert config.http.allowed_origins == ["https://claude.ai"]


def test_limit_rules(write_config: WriteConfig) -> None:
    assert "exceeds max_rows_hard" in rejected(write_config, "limits: {max_rows: 60000}\n")
    assert "limits.rate_per_min" in rejected(write_config, "limits: {rate_per_min: 0}\n")
    config = load(write_config, "limits: {query_timeout_s: {providex: 90}}\n")
    assert (config.limits.query_timeout_s.providex, config.limits.query_timeout_s.sqlserver) == (90, 30)


@pytest.mark.parametrize(
    ("formula", "normalised"),
    [
        ("onhand - so - bo - wo_required", "onhand - so - bo - wo_required"),
        ("OnHand-SO-BO-WO_Required-In_Shipping", "onhand - so - bo - wo_required - in_shipping"),
        ("onhand", "onhand"),
        ("onhand + po - so - bo", "onhand + po - so - bo"),
        ("Sage", "sage"),
    ],
)
def test_available_formula(write_config: WriteConfig, formula: str, normalised: str) -> None:
    config = load(write_config, f"views: {{inventory_available_formula: '{formula}'}}\n")
    assert config.views.inventory_available_formula == normalised


def test_available_formula_defaults_to_sages_option(write_config: WriteConfig) -> None:
    assert load(write_config, "views: {}\n").views.inventory_available_formula == "sage"


@pytest.mark.parametrize("formula", ["", "onhand -", "onhand * so", "onhand so", "onhand - cost", "- onhand"])
def test_bad_available_formula(write_config: WriteConfig, formula: str) -> None:
    assert "inventory_available_formula" in rejected(
        write_config, f"views: {{inventory_available_formula: '{formula}'}}\n"
    )


# --- environment overrides ------------------------------------------------------------------


def test_env_overrides_merge_into_the_file(write_config: WriteConfig) -> None:
    env = {
        "SAGE100_MCP__LIMITS__MAX_ROWS": "500",
        "SAGE100_MCP__BACKENDS__PROD__TRANSPORT": "cs",
        "SAGE100_MCP__BACKENDS__PROD__CS__HOST": "SAGESRV",
        "SAGE100_MCP__COMPANIES__ALLOW": "[abc, 001]",
        "UNRELATED": "x",
    }
    loaded = load_config(write_config(PROVIDEX), env=env, must_exist=True)
    backend = loaded.config.backends["prod"]
    assert isinstance(backend, ProvideXBackend)
    assert (backend.transport, backend.cs and backend.cs.host) == ("cs", "SAGESRV")
    assert loaded.config.limits.max_rows == 500
    assert loaded.config.companies.allow == ["ABC", "001"]
    assert "UNRELATED" not in loaded.env_overrides
    assert len(loaded.env_overrides) == 4


def test_env_overrides_match_existing_keys_case_insensitively(write_config: WriteConfig) -> None:
    text = PROVIDEX.replace("  prod:", "  SageProd:")
    config = load(write_config, text, env={"SAGE100_MCP__BACKENDS__SAGEPROD__DRIVER_OPTIONS__BURSTMODE": "1"})
    assert list(config.backends) == ["SageProd"]
    backend = config.backends["SageProd"]
    assert isinstance(backend, ProvideXBackend)
    assert backend.driver_options["BurstMode"] == 1


def test_env_overrides_without_a_file(isolated_home: Path) -> None:
    loaded = load_config(env={"SAGE100_MCP_HOME": str(isolated_home), "SAGE100_MCP__LIMITS__MAX_ROWS": "10"})
    assert loaded.found is False
    assert loaded.config.limits.max_rows == 10


def test_bad_env_override_is_attributed_to_the_environment(isolated_home: Path) -> None:
    with pytest.raises(ConfigError, match="invalid configuration in environment overrides"):
        load_config(env={"SAGE100_MCP_HOME": str(isolated_home), "SAGE100_MCP__LIMITS__MAX_ROWS": "many"})


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("SAGE100_MCP__SERVER__NAME__X", "server.name is a value, not a section"),
        ("SAGE100_MCP__LIMITS____MAX_ROWS", "empty key"),
    ],
)
def test_env_override_shape_errors(write_config: WriteConfig, name: str, message: str) -> None:
    assert message in rejected(write_config, "server: {name: x}\n", env={name: "1"})


def test_env_override_with_bad_collection(write_config: WriteConfig) -> None:
    assert "not a valid YAML list" in rejected(write_config, "", env={"SAGE100_MCP__COMPANIES__ALLOW": "[ABC"})


# --- paths and fail-closed loading ------------------------------------------------------------


def test_home_and_config_path_overrides(tmp_path: Path) -> None:
    assert default_home({"SAGE100_MCP_HOME": str(tmp_path)}) == tmp_path
    assert default_config_path({"SAGE100_MCP_CONFIG": str(tmp_path / "x.yaml")}) == tmp_path / "x.yaml"


def test_home_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert default_home({"PROGRAMDATA": r"D:\Data"}) == Path(r"D:\Data") / "sage100-mcp"


def test_home_on_posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert default_home({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "sage100-mcp"


def test_rejected_config_fails_closed(write_config: WriteConfig) -> None:
    path = write_config(PROVIDEX + "    driver_options: {PWD: x}\n")
    loaded = load_config_or_defaults(path, env={}, must_exist=True)
    assert loaded.error is not None
    assert "keyring" in loaded.error
    assert loaded.found is True
    assert loaded.config.backends == {}


# --- http (§16.1 plus D51) ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "endpoint"),
    [
        ("http: {}\n", "http://127.0.0.1:8765/mcp"),
        ("http: {bind: '0.0.0.0:9000', tls_cert: c.pem, tls_key: k.pem}\n", "https://0.0.0.0:9000/mcp"),
        ("http: {public_url: 'https://Sage-MCP.example.com/mcp/'}\n", "https://Sage-MCP.example.com/mcp"),
    ],
)
def test_the_endpoint_clients_use(write_config: WriteConfig, text: str, endpoint: str) -> None:
    assert load(write_config, text).http.endpoint == endpoint


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("http: {public_url: 'sage-mcp.example.com/mcp'}\n", "public_url must be an http:// or https:// URL"),
        ("http: {public_url: 'https://sage-mcp.example.com/mcp?x=1'}\n", "without query or fragment"),
        ("http: {tls_cert: c.pem}\n", "tls_cert and tls_key go together"),
        ("http: {tls_key: k.pem}\n", "tls_cert and tls_key go together"),
        ("http: {auth: oauth}\n", "auth: oauth needs an oauth"),
        ("http: {auth: oauth, oauth: {issuer: 'http://login.example.com', audience: x}}\n", "https://"),
    ],
)
def test_http_settings_are_checked(write_config: WriteConfig, text: str, fragment: str) -> None:
    assert fragment in rejected(write_config, text)
