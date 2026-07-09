from __future__ import annotations

import os
from collections.abc import Iterable
from functools import lru_cache

from mcp_email_server.log import logger

SERVICE = "mcp-email-server"
SENTINEL = "__KEYRING__"


def _entry_key(account_name: str, role: str) -> str:
    return f"{account_name}:{role}"


@lru_cache(maxsize=1)
def keyring_usable() -> bool:
    """Probe the active keyring backend with a real set/get round-trip.

    A locked collection or denied prompt can make ``get_keyring()`` look
    usable while every operation actually fails, so only a live round-trip is
    trustworthy. Cached for the process lifetime: a keychain locked at first
    probe keeps the process on plaintext until restart.
    """
    import keyring

    probe_key = f"__probe__{os.getpid()}"
    try:
        keyring.set_password(SERVICE, probe_key, "ok")
        ok = keyring.get_password(SERVICE, probe_key) == "ok"
    except Exception:
        return False

    try:
        keyring.delete_password(SERVICE, probe_key)
    except Exception:
        logger.debug("Keyring probe cleanup failed; leftover probe entry is harmless")

    if ok:
        logger.info(f"Keyring backend usable: {keyring.get_keyring()}")
    return ok


def set_secret(account_name: str, role: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, _entry_key(account_name, role), value)


def get_secret(account_name: str, role: str) -> str | None:
    import keyring

    return keyring.get_password(SERVICE, _entry_key(account_name, role))


def delete_secret(account_name: str, role: str) -> None:
    """Best-effort delete: swallows missing-entry and missing-backend errors alike."""
    import keyring
    from keyring.errors import KeyringError

    try:
        keyring.delete_password(SERVICE, _entry_key(account_name, role))
    except KeyringError:
        logger.debug(f"Keyring delete for '{account_name}:{role}' failed (already absent or no backend)")


def delete_account_credentials(account_name: str, roles: Iterable[str]) -> None:
    """Best-effort cleanup of every keyring entry for an account being removed."""
    for role in roles:
        delete_secret(account_name, role)
