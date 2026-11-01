"""Backend logins (SPEC.md §9.4): the keyring first, then environment variables, never echoed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sage100_mcp.credentials import Credential, env_names, resolve_credential
from sage100_mcp.errors import ErrorCode, SageError


def test_env_names() -> None:
    assert env_names("sage100-mcp/sage_premium") == (
        "SAGE100_MCP_CRED_SAGE_PREMIUM_USER",
        "SAGE100_MCP_CRED_SAGE_PREMIUM_PASSWORD",
    )
    assert env_names("plain") == ("SAGE100_MCP_CRED_PLAIN_USER", "SAGE100_MCP_CRED_PLAIN_PASSWORD")


def test_the_keyring_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    seen: list[tuple[str, object]] = []

    def get_credential(service: str, username: object) -> SimpleNamespace:
        seen.append((service, username))
        return SimpleNamespace(username="mcp_reader", password="from-keyring")

    monkeypatch.setattr(keyring, "get_credential", get_credential)
    env = {"SAGE100_MCP_CRED_PREMIUM_USER": "x", "SAGE100_MCP_CRED_PREMIUM_PASSWORD": "y"}
    assert resolve_credential("sage100-mcp/premium", env) == Credential("mcp_reader", "from-keyring")
    assert seen == [("sage100-mcp/premium", None)]


def test_the_environment_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    def broken(service: str, username: object) -> None:
        raise RuntimeError("no keyring backend")

    monkeypatch.setattr(keyring, "get_credential", broken)
    env = {"SAGE100_MCP_CRED_PREMIUM_USER": "reader", "SAGE100_MCP_CRED_PREMIUM_PASSWORD": "s3cret"}
    credential = resolve_credential("sage100-mcp/premium", env)
    assert credential == Credential("reader", "s3cret")
    assert "s3cret" not in repr(credential)


def test_a_missing_login_is_auth_failed_with_a_hint() -> None:
    with pytest.raises(SageError) as raised:
        resolve_credential("sage100-mcp/premium", {})
    assert raised.value.code == ErrorCode.AUTH_FAILED
    assert 'keyring set "sage100-mcp/premium"' in (raised.value.hint or "")
    assert "SAGE100_MCP_CRED_PREMIUM_PASSWORD" in (raised.value.hint or "")
