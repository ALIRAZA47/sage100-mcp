"""Backend logins (SPEC.md §9.4): a keyring entry named by the backend's ``credential``.

The entry's service name is the whole reference (``sage100-mcp/sage_premium``), its user name is
the login and its password the password, so ``keyring set "sage100-mcp/sage_premium" mcp_reader``
stores one. Containers without a keyring use SAGE100_MCP_CRED_<NAME>_USER and _PASSWORD, where
NAME is the part after the slash, upper-cased.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from sage100_mcp.errors import ErrorCode, SageError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credential:
    user: str
    password: str = field(repr=False)


def env_names(reference: str) -> tuple[str, str]:
    name = re.sub(r"[^A-Za-z0-9]+", "_", reference.rsplit("/", 1)[-1]).strip("_").upper()
    return f"SAGE100_MCP_CRED_{name}_USER", f"SAGE100_MCP_CRED_{name}_PASSWORD"


def resolve_credential(reference: str, env: Mapping[str, str] | None = None) -> Credential:
    """The keyring entry first, then the environment; never logs the password."""
    env = os.environ if env is None else env
    try:
        import keyring

        found = keyring.get_credential(reference, None)
        if found is not None and found.username and found.password:
            return Credential(found.username, found.password)
    except Exception as exc:  # no backend, locked store, ...: fall through to the environment
        logger.debug("keyring lookup for %s failed: %s", reference, type(exc).__name__)
    user_var, password_var = env_names(reference)
    user, password = env.get(user_var), env.get(password_var)
    if user and password:
        return Credential(user, password)
    raise SageError(
        ErrorCode.AUTH_FAILED,
        f"No login is stored for {reference}.",
        hint=f'Store one with: keyring set "{reference}" <login>, or set {user_var} and {password_var}.',
    )
