"""Process-persistent keyring backend used only by loopback GreenMail E2E tests."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyring(KeyringBackend):
    priority = 1

    @property
    def path(self) -> Path:
        raw = os.environ["MCP_EMAIL_SERVER_E2E_KEYRING_PATH"]
        return Path(raw)

    def _connect(self) -> sqlite3.Connection:
        path = self.path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            path.parent.chmod(0o700)
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        connection = sqlite3.connect(path, timeout=5)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS credential (
                   service TEXT NOT NULL,
                   username TEXT NOT NULL,
                   password TEXT NOT NULL,
                   PRIMARY KEY(service, username)
               )"""
        )
        connection.commit()
        return connection

    def get_password(self, service: str, username: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password FROM credential WHERE service = ? AND username = ?", (service, username)
            ).fetchone()
        return row[0] if row is not None else None

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO credential(service, username, password) VALUES (?, ?, ?)
                   ON CONFLICT(service, username) DO UPDATE SET password = excluded.password""",
                (service, username, password),
            )
            connection.commit()

    def delete_password(self, service: str, username: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM credential WHERE service = ? AND username = ?", (service, username)
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise PasswordDeleteError("Credential does not exist")
