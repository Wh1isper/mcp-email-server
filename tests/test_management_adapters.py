from __future__ import annotations

from copy import deepcopy

import pytest

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
