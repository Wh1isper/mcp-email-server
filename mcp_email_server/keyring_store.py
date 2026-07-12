from __future__ import annotations

import contextlib
import os
import re
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


# macOS Keychain OSStatus errSecInvalidOwnerEdit: the item exists but is owned by
# a *different* application, so the current process may read it but not overwrite
# it. The macOS keyring backend embeds this raw status in the PasswordSetError
# message (e.g. "Can't store password on keychain: (-25244, 'Unknown Error')").
_OWNER_EDIT_STATUS = "-25244"
# Match the status as a standalone integer token, not a bare substring: guards
# against false positives from a longer number (e.g. -252440), an account name or
# byte count that happens to contain the digits, etc. Any such misclassification
# would trigger the destructive delete-and-recreate path on an unrelated failure.
_OWNER_EDIT_RE = re.compile(rf"(?<!\d){_OWNER_EDIT_STATUS}(?!\d)")


def _is_owner_edit_conflict(error: Exception) -> bool:
    """True only for the macOS foreign-owner conflict (errSecInvalidOwnerEdit).

    Only this specific status justifies the destructive delete-and-recreate
    recovery below. Any other ``PasswordSetError`` (locked keychain, quota, disk
    error, an arbitrary third-party backend failure) must NOT trigger it: deleting
    on a transient or unrelated failure can destroy a still-valid credential.
    """
    return _OWNER_EDIT_RE.search(str(error)) is not None


def set_secret(account_name: str, role: str, value: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError, PasswordSetError

    key = _entry_key(account_name, role)
    try:
        keyring.set_password(SERVICE, key, value)
        return
    except PasswordSetError as exc:
        if not _is_owner_edit_conflict(exc):
            # Not the recoverable foreign-owner case. Propagate untouched — the
            # existing entry (if any) is left intact rather than deleted on a
            # failure we don't understand.
            raise

    # errSecInvalidOwnerEdit (-25244): the entry was created by a *different*
    # install (`uvx` vs `uv tool install`, or a differently-signed interpreter).
    # macOS refuses to modify a foreign-owned item, so delete it and recreate one
    # owned by the current process. Back the current value up first so a failed
    # recreate can be rolled back instead of losing the credential.
    logger.warning(
        f"Keyring set for '{key}' hit errSecInvalidOwnerEdit ({_OWNER_EDIT_STATUS}); the entry is "
        "owned by a different install of this tool. Deleting and recreating it under the current process."
    )
    try:
        previous = keyring.get_password(SERVICE, key)
    except Exception:
        # Can't read the old value (locked/denied) — proceed without a rollback
        # copy; the delete below may still be blocked, leaving the entry intact.
        previous = None

    # nothing to delete, or delete itself blocked — let the retry surface any error
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(SERVICE, key)

    try:
        keyring.set_password(SERVICE, key, value)
    except PasswordSetError:
        # Recreate still failed. If we managed to read the old value, restore it so
        # the caller is not left with a missing credential, then re-raise so the
        # caller records this as a store failure. (On genuine macOS foreign
        # ownership the delete above is itself blocked, so the old item is still
        # present and this restore is belt-and-suspenders; it matters only if the
        # delete succeeded and the recreate then failed transiently.)
        if previous is not None:
            try:
                keyring.set_password(SERVICE, key, previous)
            except Exception:
                logger.error(
                    f"Keyring recreate for '{key}' failed AND restoring the previous value also "
                    "failed; this credential may no longer be stored. Re-add the account."
                )
        raise


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
