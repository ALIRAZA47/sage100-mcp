"""Configuration model and loader (SPEC.md §16.1).

One YAML file, ``<home>/config.yaml``, where ``<home>`` is ``%ProgramData%\\sage100-mcp`` on
Windows. Any key can also be set with a ``SAGE100_MCP__<SECTION>__<KEY>`` environment variable.
Secrets never live here: a backend names a keyring entry in ``credential`` (§9.4).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

ENV_PREFIX = "SAGE100_MCP__"
HOME_ENV = "SAGE100_MCP_HOME"
CONFIG_ENV = "SAGE100_MCP_CONFIG"

_COMPANY_CODE = re.compile(r"[A-Z0-9]{3}")
_BACKEND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_SQL_NAME = re.compile(r"[A-Za-z0-9_]{1,128}")
_CREDENTIAL = re.compile(r"[^/\s][^/]*/[^/]*[^/\s]")
_ORIGIN = re.compile(r"https?://[^/\s?#]+")
_TIME_RANGE = re.compile(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d")

# Connection-string keywords a deployer may tune (§4.3), with their default and allowed range.
_TUNABLE_DRIVER_OPTIONS: dict[str, tuple[str, int, int, int | None]] = {
    "dirtyreads": ("DirtyReads", 0, 0, 1),
    "burstmode": ("BurstMode", 0, 0, 1),
    "cachesize": ("CacheSize", 4, 1, None),
}
_CREDENTIAL_KEYWORDS = {"uid", "pwd"}
_SERVER_MANAGED_KEYWORDS = {
    "driver", "dsn", "company", "directory", "prefix", "viewdll", "silent", "server",
    "striptrailingspaces", "remotepvkiohost", "remotepvkioport", "logfile", "debug",
    "readonly", "compression", "ssl", "enforcenulldate",
}  # fmt: skip
_NEVER_SENT_KEYWORDS = {
    "sid", "catalog", "inifile", "filedsn", "installdir", "keyrestrict", "enforcedouble", "legacynull",
}  # fmt: skip

# Terms the inventory "available" formula may use (§12.5). The default, "sage", follows the company's own
# IM_Options.QuantityAvailableToInclude, as Sage's Item Inquiry does (docs/decisions.md D40).
_AVAILABLE_TERMS = {"onhand", "so", "bo", "wo_required", "in_shipping", "po", "wo"}


class ConfigError(Exception):
    """The config file or an environment override is invalid. The message is safe to show."""


def default_home(env: Mapping[str, str] | None = None) -> Path:
    """Directory for config, caches and audit logs (§16.1)."""
    env = os.environ if env is None else env
    if override := env.get(HOME_ENV):
        return Path(override)
    if sys.platform == "win32":
        return Path(env.get("PROGRAMDATA", r"C:\ProgramData")) / "sage100-mcp"
    data_home = env.get("XDG_DATA_HOME")
    return (Path(data_home) if data_home else Path.home() / ".local" / "share") / "sage100-mcp"


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if override := env.get(CONFIG_ENV):
        return Path(override)
    return default_home(env) / "config.yaml"


def parse_company_code(value: object) -> str:
    """Normalise a company code to upper case and check it is three letters or digits (§8.3)."""
    if not isinstance(value, str):
        raise ValueError(f"company code must be text, not {type(value).__name__}")
    code = value.strip().upper()
    if value.isascii() and _COMPANY_CODE.fullmatch(code):  # isascii: upper() maps some non-ASCII letters to A-Z
        return code
    hint = f"; use the code {code[4:]!r}, not the data folder or database name" if code.startswith("MAS_") else ""
    raise ValueError(f"invalid company code {value!r}: expected 3 letters or digits, such as ABC{hint}")


CompanyCode = Annotated[str, BeforeValidator(parse_company_code)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSection(_Section):
    name: str = "sage100-mcp"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    audit_dir: Path | None = None  # None means <home>/audit
    audit_redact_literals: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value


class OAuthSection(_Section):
    issuer: str
    audience: str

    @field_validator("issuer")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("issuer must be an https:// URL")
        return value


class HttpSection(_Section):
    bind: str = "127.0.0.1:8765"
    allowed_origins: list[str] = Field(default_factory=list)
    auth: Literal["bearer", "oauth"] = "bearer"
    oauth: OAuthSection | None = None
    # Not in §16.1 (docs/decisions.md D51): the endpoint URL clients use, such as
    # https://sage-mcp.corp.example/mcp behind a proxy; and a certificate to serve TLS directly.
    public_url: str | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = value.strip().rstrip("/")
        if not re.fullmatch(r"https?://[^/\s?#]+(/[^\s?#]*)?", url):
            raise ValueError(f"public_url must be an http:// or https:// URL without query or fragment, not {value!r}")
        return url

    @field_validator("bind")
    @classmethod
    def _bind(cls, value: str) -> str:
        host, sep, port = value.strip().rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"bind must be host:port, such as 127.0.0.1:8765, not {value!r}")
        return value.strip()

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        origins = []
        for origin in value:
            normalised = origin.strip().rstrip("/").lower()
            if not _ORIGIN.fullmatch(normalised):
                raise ValueError(f"origin {origin!r} must be scheme://host[:port] with no path")
            origins.append(normalised)
        return origins

    @model_validator(mode="after")
    def _oauth_settings(self) -> HttpSection:
        if self.auth == "oauth" and self.oauth is None:
            raise ValueError("auth: oauth needs an oauth: {issuer, audience} section")
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("tls_cert and tls_key go together")
        return self

    @property
    def endpoint(self) -> str:
        """The MCP endpoint URL as clients see it: public_url, else this server's own address."""
        if self.public_url:
            return self.public_url
        scheme = "https" if self.tls_cert else "http"
        return f"{scheme}://{self.bind}/mcp"

    @property
    def host(self) -> str:
        return self.bind.rpartition(":")[0].strip("[]")

    @property
    def port(self) -> int:
        return int(self.bind.rpartition(":")[2])


class CsSection(_Section):
    """Client/Server ODBC endpoint (§4.4)."""

    host: str = Field(min_length=1)
    port: int = Field(default=20222, gt=0, lt=65536)


class PerformanceProfile(_Section):
    after_hours: str | None = None  # "HH:MM-HH:MM" in server local time; may wrap midnight

    @field_validator("after_hours")
    @classmethod
    def _range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _TIME_RANGE.fullmatch(value) or value[:5] == value[6:]:
            raise ValueError(f"after_hours must be HH:MM-HH:MM with different ends, not {value!r}")
        return value


def _check_credential(value: str) -> str:
    value = value.strip()
    if not _CREDENTIAL.fullmatch(value):
        raise ValueError(
            f"credential must name a keyring entry as service/name, such as sage100-mcp/prod, not {value!r}"
        )
    return value


KeyringRef = Annotated[str, AfterValidator(_check_credential)]


class ProvideXBackend(_Section):
    """Sage 100 Standard/Advanced through the ProvideX ODBC driver (Backend A, §4)."""

    type: Literal["providex"]
    transport: Literal["local", "cs"] = "local"
    directory: str = Field(min_length=1)  # the MAS90 folder; a UNC path when running as a service
    prefix: str = "auto"
    viewdll: str = "auto"
    cs: CsSection | None = None
    credential: KeyringRef
    driver_options: dict[str, int] = Field(default_factory=dict, validate_default=True)
    performance_profile: PerformanceProfile | None = None
    # Which build of Sage's driver to load. 32 loads it through the 32-bit helper. auto uses the helper only
    # when the driver is registered for 32-bit alone (docs/decisions.md D67).
    driver_bitness: Literal["auto", "32", "64"] = "auto"
    # The 32-bit helper: sage100-mcp-odbc32.exe, or a 32-bit python.exe that has pyodbc. The MSI's is found
    # without this setting.
    helper: str | None = None

    @field_validator("driver_bitness", mode="before")
    @classmethod
    def _bitness_as_text(cls, value: object) -> object:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("driver_options", mode="before")
    @classmethod
    def _driver_option_keywords(cls, value: object) -> object:
        """Check keywords before values are typed, so a misplaced secret is refused for what it is."""
        if not isinstance(value, Mapping):
            return value  # pydantic reports the type error
        options: dict[str, object] = {}
        for keyword, setting in value.items():
            folded = str(keyword).strip().lower()
            if folded in _CREDENTIAL_KEYWORDS:
                raise ValueError(f"{keyword} comes from the keyring entry named in credential, not driver_options")
            if folded in _SERVER_MANAGED_KEYWORDS:
                raise ValueError(f"{keyword} is set by the server and cannot be configured (SPEC.md §4.3)")
            if folded in _NEVER_SENT_KEYWORDS:
                raise ValueError(f"{keyword} is never sent to the Sage driver (SPEC.md §4.3)")
            if folded not in _TUNABLE_DRIVER_OPTIONS:
                allowed = ", ".join(spec[0] for spec in _TUNABLE_DRIVER_OPTIONS.values())
                raise ValueError(f"unknown driver option {keyword!r}; allowed: {allowed}")
            canonical = _TUNABLE_DRIVER_OPTIONS[folded][0]
            if canonical in options:
                raise ValueError(f"driver option {canonical} is given twice")
            options[canonical] = setting
        return options

    @field_validator("driver_options")
    @classmethod
    def _driver_option_values(cls, value: dict[str, int]) -> dict[str, int]:
        options = {canonical: default for canonical, default, _, _ in _TUNABLE_DRIVER_OPTIONS.values()}
        for canonical, setting in value.items():
            _, _, low, high = _TUNABLE_DRIVER_OPTIONS[canonical.lower()]
            if setting < low or (high is not None and setting > high):
                bounds = f"{low} or {high}" if high is not None else f"at least {low}"
                raise ValueError(f"{canonical} must be {bounds}, not {setting}")
            options[canonical] = setting
        return options

    @model_validator(mode="after")
    def _cs_endpoint(self) -> ProvideXBackend:
        if self.transport == "cs" and self.cs is None:
            raise ValueError("transport: cs needs a cs: {host, port} section (SPEC.md §4.4)")
        return self


class SqlServerBackend(_Section):
    """Sage 100 Premium, or a SQL mirror of any edition (Backend B, §3)."""

    type: Literal["sqlserver"]
    server: str = Field(min_length=1)
    database_pattern: str = "MAS_{company}"
    system_database: str = "MAS_SYSTEM"
    driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: bool = True
    trust_server_certificate: bool = False  # only for servers with a self-signed certificate
    connect_timeout_s: int = Field(default=15, gt=0)
    credential: KeyringRef

    @field_validator("database_pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        if value.count("{company}") != 1 or not _SQL_NAME.fullmatch(value.replace("{company}", "ABC")):
            raise ValueError("database_pattern must contain {company} once and otherwise only letters, digits and _")
        return value

    @field_validator("system_database")
    @classmethod
    def _system_database(cls, value: str) -> str:
        if not _SQL_NAME.fullmatch(value):
            raise ValueError("system_database may contain only letters, digits and _")
        return value


Backend = Annotated[ProvideXBackend | SqlServerBackend, Field(discriminator="type")]


class CompaniesSection(_Section):
    default: CompanyCode | None = None
    allow: list[CompanyCode] | None = None  # None exposes every discovered company
    hide: list[CompanyCode] = Field(default_factory=list)
    backend_map: dict[CompanyCode, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> CompaniesSection:
        if both := sorted(set(self.allow or ()) & set(self.hide)):
            raise ValueError(f"companies listed in both allow and hide: {', '.join(both)}")
        if self.default is not None:
            if self.default in self.hide:
                raise ValueError(f"default company {self.default} is hidden")
            if self.allow is not None and self.default not in self.allow:
                raise ValueError(f"default company {self.default} is not in allow")
        return self


class QueryTimeouts(_Section):
    providex: int = Field(default=60, gt=0)
    sqlserver: int = Field(default=30, gt=0)


class Concurrency(_Section):
    providex: int = Field(default=2, gt=0)
    sqlserver: int = Field(default=4, gt=0)


class LimitsSection(_Section):
    max_rows: int = Field(default=1000, gt=0)
    max_rows_hard: int = Field(default=50000, gt=0)
    max_pushdown_rows: int = Field(default=250000, gt=0)
    query_timeout_s: QueryTimeouts = Field(default_factory=QueryTimeouts)
    per_company_concurrency: Concurrency = Field(default_factory=Concurrency)
    rate_per_min: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _row_caps(self) -> LimitsSection:
        if self.max_rows > self.max_rows_hard:
            raise ValueError(f"max_rows ({self.max_rows}) exceeds max_rows_hard ({self.max_rows_hard})")
        return self


class ViewsSection(_Section):
    inventory_available_formula: str = "sage"

    @field_validator("inventory_available_formula")
    @classmethod
    def _formula(cls, value: str) -> str:
        if value.strip().lower() == "sage":
            return "sage"
        tokens = re.findall(r"[a-z_]+|[+-]|\S", value.lower())
        terms, signs = tokens[0::2], tokens[1::2]
        if (
            not tokens
            or len(tokens) % 2 == 0
            or any(t not in _AVAILABLE_TERMS for t in terms)
            or any(s not in "+-" for s in signs)
        ):
            raise ValueError(
                f"inventory_available_formula must be sage, or terms joined by + or -, using {sorted(_AVAILABLE_TERMS)}"
            )
        return " ".join(tokens)


class Config(_Section):
    server: ServerSection = Field(default_factory=ServerSection)
    http: HttpSection = Field(default_factory=HttpSection)
    backends: dict[str, Backend] = Field(default_factory=dict)
    companies: CompaniesSection = Field(default_factory=CompaniesSection)
    limits: LimitsSection = Field(default_factory=LimitsSection)
    policy_file: Path | None = None
    views: ViewsSection = Field(default_factory=ViewsSection)

    @field_validator("backends")
    @classmethod
    def _backend_names(
        cls, value: dict[str, ProvideXBackend | SqlServerBackend]
    ) -> dict[str, ProvideXBackend | SqlServerBackend]:
        for name in value:
            if not _BACKEND_NAME.fullmatch(name):
                raise ValueError(f"backend name {name!r} must start with a letter and use only letters, digits and _")
        return value

    @model_validator(mode="after")
    def _backend_map(self) -> Config:
        for company, backend in self.companies.backend_map.items():
            if backend not in self.backends:
                raise ValueError(f"companies.backend_map sends {company} to undefined backend {backend!r}")
        return self


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    path: Path
    found: bool  # False when no file existed and defaults (plus env overrides) were used
    env_overrides: tuple[str, ...] = ()  # variable names only; values may be sensitive
    error: str | None = None  # set by load_config_or_defaults when the config was rejected


class _ConfigLoader(yaml.SafeLoader):
    """Keeps scalars as text so pydantic does the typing, and rejects duplicate keys.

    YAML 1.1 would turn company code 001 into 1, YES into True and 19:00 into 1140.
    A repeated key would silently replace the first, for example dropping companies.allow.
    """


_KEEP_IMPLICIT = {"tag:yaml.org,2002:null", "tag:yaml.org,2002:merge"}
_ConfigLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag in _KEEP_IMPLICIT]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader: _ConfigLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            continue
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def parse_yaml(text: str) -> Any:
    """Parse YAML keeping scalars as text and refusing duplicate keys (see _ConfigLoader)."""
    return yaml.load(text, Loader=_ConfigLoader)  # a SafeLoader subclass


def apply_env_overrides(data: MutableMapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """Overlay ``SAGE100_MCP__A__B=value`` onto the parsed file, matching existing keys case-insensitively.

    Values stay text (pydantic converts them) unless they start with ``[`` or ``{``, which are read as
    YAML flow collections, for example ``SAGE100_MCP__COMPANIES__ALLOW=[ABC, EMP]``.
    """
    applied = []
    for name in sorted(env):
        if not name.upper().startswith(ENV_PREFIX):
            continue
        path = name[len(ENV_PREFIX) :].split("__")
        if not all(path):
            raise ConfigError(f"{name}: empty key between double underscores")
        raw = env[name]
        value: Any = raw
        if raw.lstrip().startswith(("[", "{")):
            try:
                value = parse_yaml(raw)
            except yaml.YAMLError as exc:
                raise ConfigError(f"{name}: not a valid YAML list or mapping ({exc.__class__.__name__})") from None
        node = data
        for depth, part in enumerate(path):
            key = next((k for k in node if isinstance(k, str) and k.lower() == part.lower()), part.lower())
            if depth == len(path) - 1:
                node[key] = value
                break
            child = node.get(key)
            if child is None:
                child = node[key] = {}
            elif not isinstance(child, MutableMapping):
                raise ConfigError(f"{name}: {'.'.join(path[: depth + 1]).lower()} is a value, not a section")
            node = child
        applied.append(name)
    return applied


def _describe(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def load_config(
    path: Path | None = None, *, env: Mapping[str, str] | None = None, must_exist: bool = False
) -> LoadedConfig:
    """Read the YAML file (if any), apply environment overrides and validate.

    With no explicit path a missing file is not an error: the server starts with defaults and
    ``sage_status`` says how to create one. ``must_exist`` is for a path given on the command line.
    """
    env = os.environ if env is None else env
    path = path or default_config_path(env)
    found = path.is_file()
    if must_exist and not found:
        raise ConfigError(f"config file not found: {path}")
    data: Any = {}
    if found:
        try:
            text = path.read_text(encoding="utf-8-sig")  # tolerate the BOM Notepad writes
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
        try:
            data = parse_yaml(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML:\n{exc}") from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: the top level must be a mapping of sections")
    overrides = apply_env_overrides(data, env)
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        source = str(path) if found else "environment overrides"
        raise ConfigError(f"invalid configuration in {source}:\n{_describe(exc)}") from None
    return LoadedConfig(config=config, path=path, found=found, env_overrides=tuple(overrides))


def load_config_or_defaults(
    path: Path | None = None, *, env: Mapping[str, str] | None = None, must_exist: bool = False
) -> LoadedConfig:
    """Like ``load_config``, but a rejected config yields defaults plus the error instead of raising.

    Defaults have no backends, so nothing is reachable: the server fails closed while still
    answering ``sage_status``, which is how a stdio client gets to see the problem.
    """
    try:
        return load_config(path, env=env, must_exist=must_exist)
    except ConfigError as exc:
        resolved = path or default_config_path(os.environ if env is None else env)
        return LoadedConfig(config=Config(), path=resolved, found=resolved.is_file(), error=str(exc))
