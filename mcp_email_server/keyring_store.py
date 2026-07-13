from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterable
from functools import lru_cache
from typing import Literal

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
# a different application, so the current process may read it but not overwrite it.
_OWNER_EDIT_STATUS = -25244


def _is_owner_edit_conflict(error: Exception) -> bool:
    """Recognize errSecInvalidOwnerEdit from the chained macOS backend error.

    Matching an arbitrary error message is unsafe because a third-party backend
    can include ``-25244`` for an unrelated reason. keyring's macOS backend chains
    its low-level ``api.Error(status, message)`` into ``PasswordSetError``, so use
    that integer status and only enable this recovery on Darwin.
    """
    if sys.platform != "darwin":
        return False
    cause = error.__cause__
    return (
        isinstance(cause, Exception)
        and bool(cause.args)
        and isinstance(cause.args[0], int)
        and cause.args[0] == _OWNER_EDIT_STATUS
    )


def _restore_previous_secret(keyring, key: str, previous: str | None, *, force: bool) -> None:
    """Best-effort rollback after a failed keyring write.

    Always avoid another backend write when the old value is confirmed intact.
    ``force`` is used after this module explicitly attempted deletion: if read-back
    itself fails, still attempt restoration because the entry may already be gone.
    """
    if previous is None:
        return
    try:
        if keyring.get_password(SERVICE, key) == previous:
            return
    except Exception:
        if not force:
            logger.error(
                f"Keyring write for '{key}' failed and the previous value could not be verified; "
                "the credential may need to be re-added."
            )
            return
    try:
        keyring.set_password(SERVICE, key, previous)
    except Exception:
        logger.error(
            f"Keyring write for '{key}' failed AND restoring the previous value also failed; "
            "this credential may no longer be stored. Re-add the account."
        )


def set_secret(account_name: str, role: str, value: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError, PasswordSetError

    key = _entry_key(account_name, role)

    # keyring's macOS backend implements set as delete-then-add. Capture the old
    # value before the first write so an add failure cannot erase the only rollback
    # copy. If the snapshot itself is denied, fail closed before changing anything.
    previous = keyring.get_password(SERVICE, key)
    if previous == value:
        return
    try:
        keyring.set_password(SERVICE, key, value)
        return
    except PasswordSetError as exc:
        if not _is_owner_edit_conflict(exc):
            _restore_previous_secret(keyring, key, previous, force=False)
            raise
    except Exception:
        _restore_previous_secret(keyring, key, previous, force=False)
        raise

    # A verified macOS foreign-owner conflict is the only case where destructive
    # recovery is allowed. The snapshot above predates keyring's own delete-first
    # write, so every recreate failure can attempt to restore the original value.
    logger.warning(
        f"Keyring set for '{key}' hit errSecInvalidOwnerEdit ({_OWNER_EDIT_STATUS}); the entry is "
        "owned by a different install of this tool. Deleting and recreating it under the current process."
    )
    try:
        keyring.delete_password(SERVICE, key)
    except PasswordDeleteError:
        pass
    except Exception:
        _restore_previous_secret(keyring, key, previous, force=False)
        raise

    try:
        keyring.set_password(SERVICE, key, value)
    except Exception:
        _restore_previous_secret(keyring, key, previous, force=True)
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


def delete_secret_checked(account_name: str, role: str) -> Literal["deleted", "present", "unverifiable"]:
    """Delete a secret and verify whether the keyring entry remains."""
    import keyring
    from keyring.errors import KeyringError

    key = _entry_key(account_name, role)
    # A missing entry and a backend failure use the same broad keyring error
    # hierarchy. Resolve the ambiguity with a read-back below.
    with contextlib.suppress(KeyringError):
        keyring.delete_password(SERVICE, key)

    try:
        value = keyring.get_password(SERVICE, key)
    except Exception:
        return "unverifiable"
    return "deleted" if value is None else "present"


def delete_account_credentials(account_name: str, roles: Iterable[str]) -> None:
    """Best-effort cleanup of every keyring entry for an account being removed."""
    for role in roles:
        delete_secret(account_name, role)
