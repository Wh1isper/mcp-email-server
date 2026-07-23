from __future__ import annotations

from copy import deepcopy

import pytest

from mcp_email_server import keyring_store
from mcp_email_server.adapters.management import LocalManagementBackend
from mcp_email_server.application.management import ManagementError


def _legacy_raw() -> dict[str, object]:
    return {
        "emails": [
            {
                "account_name": "alice",
                "full_name": "Alice",
                "email_address": "alice@example.test",
                "incoming": {
                    "host": "imap.example.test",
                    "port": 993,
                    "user_name": "alice@example.test",
                    "password": "legacy-secret",
                    "use_ssl": True,
                },
            }
        ]
    }


def test_legacy_secret_resolution_rejects_changed_source_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalManagementBackend()
    original = _legacy_raw()
    account = backend._parse_legacy_accounts(original)[0]
    expected = backend._legacy_account_snapshot(account)
    changed = deepcopy(original)
    changed_accounts = changed["emails"]
    assert isinstance(changed_accounts, list)
    changed_account = changed_accounts[0]
    assert isinstance(changed_account, dict)
    changed_incoming = changed_account["incoming"]
    assert isinstance(changed_incoming, dict)
    changed_incoming["host"] = "imap.changed.example.test"
    monkeypatch.setattr(backend, "_read_legacy_raw", lambda: changed)

    with pytest.raises(ManagementError, match=r"source changed.*preview and retry"):
        backend.resolve_legacy_secret("alice", "incoming", expected)


def test_legacy_snapshot_exposes_only_credential_source_class() -> None:
    backend = LocalManagementBackend()
    raw = _legacy_raw()
    account = backend._parse_legacy_accounts(raw)[0]
    plaintext = backend._legacy_account_snapshot(account)
    incoming = raw["emails"]
    assert isinstance(incoming, list)
    account_raw = incoming[0]
    assert isinstance(account_raw, dict)
    endpoint = account_raw["incoming"]
    assert isinstance(endpoint, dict)
    endpoint["password"] = keyring_store.SENTINEL
    keyring_account = backend._parse_legacy_accounts(raw)[0]
    keyring = backend._legacy_account_snapshot(keyring_account)

    assert plaintext.incoming_secret_source == "plaintext"  # noqa: S105 - source class
    assert keyring.incoming_secret_source == "keyring"  # noqa: S105 - source class
    assert "legacy-secret" not in repr(plaintext)
