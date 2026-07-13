from __future__ import annotations

import contextlib
import datetime
import fnmatch
import os
import sys
import tempfile
from collections.abc import Iterable
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import tomli_w
from pydantic import BaseModel, Field, PrivateAttr, SecretStr, SerializationInfo, field_serializer, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from mcp_email_server import keyring_store
from mcp_email_server.log import logger

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # pragma: no cover (coverage is uploaded from the 3.12 job)

DEFAULT_CONFIG_PATH = "~/.config/zerolib/mcp_email_server/config.toml"
_VALID_CREDENTIAL_STORAGE_MODES = ("auto", "keyring", "plaintext")

# Set by Settings.load_for_migration() around construction so __init__ can skip
# env-composited state (override pickup, env-account injection, allowlist/bool env
# reads) and always attempt keyring resolution regardless of credential_storage.
_MIGRATION_LOAD = False


def _parse_bool_env(value: str | None, default: bool = False) -> bool:
    """Parse boolean value from environment variable."""
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


def normalize_address(raw: str) -> str:
    """Extract and normalize a bare email address for case-insensitive comparison.

    "Alice <Alice@Example.com>" -> "alice@example.com"; "" -> "". ``parseaddr`` is lenient,
    so non-address input yields a token that will not equal a real configured address.
    """
    _, addr = parseaddr(raw)
    return addr.strip().lower()


def sender_allowed(sender: str, patterns: list[str]) -> bool:
    """Return True if exactly one sender address matches any allowlist pattern.

    An empty allowlist allows everyone. When an allowlist is configured, malformed, empty, or
    multi-address From headers fail closed rather than relying on parser leniency.
    """
    if not patterns:
        return True

    addrs = [addr.strip().lower() for _name, addr in getaddresses([sender]) if addr.strip()]
    if len(addrs) != 1:
        return False

    return any(fnmatch.fnmatchcase(addrs[0], pattern.lower()) for pattern in patterns)


def _normalize_address_list(raw: Iterable[str]) -> list[str]:
    """Normalize each address, drop empties, de-duplicate (order-preserving)."""
    return list(dict.fromkeys(a for a in (normalize_address(x) for x in raw) if a))


def _normalize_pattern_list(raw: Iterable[str]) -> list[str]:
    """Lowercase, strip, de-duplicate (order-preserving). Glob characters are preserved."""
    return list(dict.fromkeys(p.strip().lower() for p in raw if p.strip()))


def _reject_sentinel_secret(secret: SecretStr, label: str) -> None:
    """Reject the reserved keyring sentinel as a literal secret value.

    Cannot be enforced with a field validator on EmailServer/ProviderSettings:
    those run during the TOML load itself, before any Settings-level code, and
    would reject every legitimately keyring-stored config. Enforced instead at
    creation entry points (EmailSettings.init, Settings.add_email/add_provider)
    and as a defense-in-depth pre-write check in Settings.store().
    """
    if secret.get_secret_value() == keyring_store.SENTINEL:
        raise ValueError(
            f"{label} cannot be the reserved value {keyring_store.SENTINEL!r} "
            "(used internally to mark keyring-stored credentials)"
        )


CONFIG_PATH = Path(os.getenv("MCP_EMAIL_SERVER_CONFIG_PATH", DEFAULT_CONFIG_PATH)).expanduser().resolve()


class EmailServer(BaseModel):
    user_name: str
    password: SecretStr
    host: str
    port: int
    use_ssl: bool = True  # Usually port 465
    start_ssl: bool = False  # Usually port 587
    verify_ssl: bool = True  # Set to False for self-signed certificates (e.g., ProtonMail Bridge)

    @field_serializer("password")
    def serialize_password(self, v: SecretStr, info: SerializationInfo) -> str:
        if info.context and info.context.get("secrets") == "keyring":
            return keyring_store.SENTINEL
        return v.get_secret_value()

    def masked(self) -> EmailServer:
        return self.model_copy(update={"password": SecretStr("********")})


class AccountAttributes(BaseModel):
    account_name: str
    description: str = ""
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(ZoneInfo("UTC")))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(ZoneInfo("UTC")))

    @model_validator(mode="after")
    def update_updated_at(self) -> AccountAttributes:
        """Update updated_at field."""
        self.updated_at = datetime.datetime.now(ZoneInfo("UTC"))
        return self

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AccountAttributes):
            return NotImplemented
        return self.model_dump(exclude={"created_at", "updated_at"}) == other.model_dump(
            exclude={"created_at", "updated_at"}
        )

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, v: datetime.datetime) -> str:
        return v.isoformat()

    def masked(self) -> AccountAttributes:
        return self.model_copy()


class EmailSettings(AccountAttributes):
    full_name: str
    email_address: str
    incoming: EmailServer
    outgoing: EmailServer | None = None
    save_to_sent: bool = True  # Save sent emails to IMAP Sent folder
    sent_folder_name: str | None = None  # Override Sent folder name (auto-detect if None)

    @property
    def can_send(self) -> bool:
        """Return whether this account has SMTP configuration."""
        return self.outgoing is not None

    @classmethod
    def init(
        cls,
        *,
        account_name: str,
        full_name: str,
        email_address: str,
        user_name: str,
        password: str,
        imap_host: str,
        smtp_host: str | None = None,
        imap_user_name: str | None = None,
        imap_password: str | None = None,
        imap_port: int = 993,
        imap_ssl: bool = True,
        imap_start_ssl: bool = False,
        imap_verify_ssl: bool = True,
        smtp_port: int = 465,
        smtp_ssl: bool = True,
        smtp_start_ssl: bool = False,
        smtp_verify_ssl: bool = True,
        smtp_user_name: str | None = None,
        smtp_password: str | None = None,
        save_to_sent: bool = True,
        sent_folder_name: str | None = None,
    ) -> EmailSettings:
        for candidate in (password, imap_password, smtp_password):
            if candidate == keyring_store.SENTINEL:
                raise ValueError(
                    f"Password value {keyring_store.SENTINEL!r} is reserved for keyring-stored "
                    "credentials and cannot be used as an account password"
                )
        return cls(
            account_name=account_name,
            full_name=full_name,
            email_address=email_address,
            incoming=EmailServer(
                user_name=imap_user_name or user_name,
                password=imap_password or password,
                host=imap_host,
                port=imap_port,
                use_ssl=imap_ssl,
                start_ssl=imap_start_ssl,
                verify_ssl=imap_verify_ssl,
            ),
            outgoing=(
                EmailServer(
                    user_name=smtp_user_name or user_name,
                    password=smtp_password or password,
                    host=smtp_host,
                    port=smtp_port,
                    use_ssl=smtp_ssl,
                    start_ssl=smtp_start_ssl,
                    verify_ssl=smtp_verify_ssl,
                )
                if smtp_host
                else None
            ),
            save_to_sent=save_to_sent,
            sent_folder_name=sent_folder_name,
        )

    @classmethod
    def from_env(cls) -> EmailSettings | None:
        """Create EmailSettings from environment variables.

        Expected environment variables:
        - MCP_EMAIL_SERVER_ACCOUNT_NAME (default: "default")
        - MCP_EMAIL_SERVER_FULL_NAME
        - MCP_EMAIL_SERVER_EMAIL_ADDRESS
        - MCP_EMAIL_SERVER_USER_NAME
        - MCP_EMAIL_SERVER_PASSWORD
        - MCP_EMAIL_SERVER_IMAP_HOST
        - MCP_EMAIL_SERVER_IMAP_PORT (default: 993)
        - MCP_EMAIL_SERVER_IMAP_SSL (default: true)
        - MCP_EMAIL_SERVER_IMAP_START_SSL (default: false)
        - MCP_EMAIL_SERVER_IMAP_VERIFY_SSL (default: true)
        - MCP_EMAIL_SERVER_SMTP_HOST (optional; enables send_email)
        - MCP_EMAIL_SERVER_SMTP_PORT (default: 465)
        - MCP_EMAIL_SERVER_SMTP_SSL (default: true)
        - MCP_EMAIL_SERVER_SMTP_START_SSL (default: false)
        - MCP_EMAIL_SERVER_SMTP_VERIFY_SSL (default: true)
        - MCP_EMAIL_SERVER_SAVE_TO_SENT (default: true)
        - MCP_EMAIL_SERVER_SENT_FOLDER_NAME (default: auto-detect)
        """
        # Check if minimum required environment variables are set
        email_address = os.getenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS")
        password = os.getenv("MCP_EMAIL_SERVER_PASSWORD")

        if not email_address or not password:
            return None

        # Get all environment variables with defaults
        account_name = os.getenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "default")
        full_name = os.getenv("MCP_EMAIL_SERVER_FULL_NAME", email_address.split("@")[0])
        user_name = os.getenv("MCP_EMAIL_SERVER_USER_NAME", email_address)
        imap_host = os.getenv("MCP_EMAIL_SERVER_IMAP_HOST")
        smtp_host = os.getenv("MCP_EMAIL_SERVER_SMTP_HOST")

        # Required fields check
        if not imap_host:
            logger.warning("Missing required email configuration environment variable: IMAP_HOST")
            return None

        try:
            return cls.init(
                account_name=account_name,
                full_name=full_name,
                email_address=email_address,
                user_name=user_name,
                password=password,
                imap_host=imap_host,
                imap_port=int(os.getenv("MCP_EMAIL_SERVER_IMAP_PORT", "993")),
                imap_ssl=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_IMAP_SSL"), True),
                imap_start_ssl=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_IMAP_START_SSL"), False),
                imap_verify_ssl=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_IMAP_VERIFY_SSL"), True),
                smtp_host=smtp_host,
                smtp_port=int(os.getenv("MCP_EMAIL_SERVER_SMTP_PORT", "465")),
                smtp_ssl=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_SMTP_SSL"), True),
                smtp_start_ssl=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_SMTP_START_SSL"), False),
                smtp_verify_ssl=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_SMTP_VERIFY_SSL"), True),
                smtp_user_name=os.getenv("MCP_EMAIL_SERVER_SMTP_USER_NAME", user_name),
                smtp_password=os.getenv("MCP_EMAIL_SERVER_SMTP_PASSWORD", password),
                imap_user_name=os.getenv("MCP_EMAIL_SERVER_IMAP_USER_NAME", user_name),
                imap_password=os.getenv("MCP_EMAIL_SERVER_IMAP_PASSWORD", password),
                save_to_sent=_parse_bool_env(os.getenv("MCP_EMAIL_SERVER_SAVE_TO_SENT"), True),
                sent_folder_name=os.getenv("MCP_EMAIL_SERVER_SENT_FOLDER_NAME"),
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to create email settings from environment variables: {e}")
            return None

    def masked(self) -> EmailSettings:
        return self.model_copy(
            update={
                "incoming": self.incoming.masked(),
                "outgoing": self.outgoing.masked() if self.outgoing else None,
            }
        )


class ProviderSettings(AccountAttributes):
    provider_name: str
    api_key: SecretStr

    @field_serializer("api_key")
    def serialize_api_key(self, v: SecretStr, info: SerializationInfo) -> str:
        if info.context and info.context.get("secrets") == "keyring":
            return keyring_store.SENTINEL
        return v.get_secret_value()

    def masked(self) -> AccountAttributes:
        return self.model_copy(update={"api_key": SecretStr("********")})


class Settings(BaseSettings):
    emails: list[EmailSettings] = []
    providers: list[ProviderSettings] = []
    db_location: str = CONFIG_PATH.with_name("db.sqlite3").as_posix()
    enable_attachment_download: bool = False
    allowed_recipients: list[str] = []
    allowed_senders: list[str] = []
    report_blocked_mutations: bool = False
    credential_storage: Literal["auto", "keyring", "plaintext"] = "auto"

    # Env-var override for credential_storage. Kept separate from the loaded field
    # so environment precedence is explicit. A later store serializes the effective
    # value because the override controls the credential representation written to
    # that same file; persisting the old mode would make the file self-contradictory.
    _credential_storage_override: str | None = PrivateAttr(default=None)
    _loaded_keyring_references: set[tuple[str, str]] = PrivateAttr(default_factory=set)

    model_config = SettingsConfigDict(toml_file=CONFIG_PATH, validate_assignment=True, revalidate_instances="always")

    @property
    def effective_credential_storage(self) -> str:
        """The mode that actually governs storage decisions: env override, else the field.

        Returns the raw three-value literal — never probes the keyring. Only
        store() maps "auto" to a concrete backend via keyring_store.keyring_usable().
        """
        return self._credential_storage_override or self.credential_storage

    def _apply_bool_env_override(self, attr: str, env_var: str) -> None:
        value = os.getenv(env_var)
        if value is not None:
            setattr(self, attr, _parse_bool_env(value, False))
            logger.info(f"Set {attr}={getattr(self, attr)} from environment variable")

    def _pickup_credential_storage_override(self) -> None:
        override = os.getenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE")
        if override is None:
            return
        if override not in _VALID_CREDENTIAL_STORAGE_MODES:
            raise ValueError(
                f"Invalid MCP_EMAIL_SERVER_CREDENTIAL_STORAGE={override!r}; "
                f"must be one of {', '.join(_VALID_CREDENTIAL_STORAGE_MODES)}"
            )
        self._credential_storage_override = override

    def __init__(self, **data: Any) -> None:
        """Initialize Settings with support for environment variables."""
        super().__init__(**data)

        migration_load = _MIGRATION_LOAD

        # TOML normalisation is unconditional (safe during migration loads too): it
        # only reshapes values already in the file, independent of env state.
        if self.allowed_recipients:
            self.allowed_recipients = _normalize_address_list(self.allowed_recipients)
        if self.allowed_senders:
            self.allowed_senders = _normalize_pattern_list(self.allowed_senders)

        if not migration_load:
            self._apply_env_overrides()

        # Preserve which entries were keyring references before replacing their
        # sentinels with live secrets. Plaintext migration uses this provenance to
        # clean up only entries that the file actually referenced.
        pending = self._pending_keyring_sentinels()
        if migration_load:
            self._loaded_keyring_references = {(name, role) for name, role, _obj in pending}

        # Sentinel resolution always runs (including migration loads); only the
        # plaintext-mode hard error is suppressed during migration (§2/§5/§7).
        self._resolve_keyring_sentinels(migration_load=migration_load, pending=pending)

    def _apply_env_overrides(self) -> None:
        """Env-composited state, skipped entirely during migration loads (§7) so
        migration transforms the stored config, not the env-composited view.
        """
        self._pickup_credential_storage_override()
        self._apply_bool_env_override("enable_attachment_download", "MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD")
        self._apply_bool_env_override("report_blocked_mutations", "MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS")

        # Environment variable overrides TOML (comma-separated); an empty string clears the allowlist.
        env_allowed = os.getenv("MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS")
        if env_allowed is not None:
            self.allowed_recipients = _normalize_address_list(env_allowed.split(","))

        env_senders = os.getenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS")
        if env_senders is not None:
            self.allowed_senders = _normalize_pattern_list(env_senders.split(","))

        self._inject_env_account()

    def _inject_env_account(self) -> None:
        env_email = EmailSettings.from_env()
        if not env_email:
            return

        existing_account = None
        for i, email in enumerate(self.emails):
            if email.account_name == env_email.account_name:
                existing_account = i
                break

        if existing_account is not None:
            self.emails[existing_account] = env_email
            logger.info(f"Overriding email account '{env_email.account_name}' with environment variables")
        else:
            self.emails.insert(0, env_email)
            logger.info(f"Added email account '{env_email.account_name}' from environment variables")

    def _pending_keyring_sentinels(self) -> list[tuple[str, str, EmailServer | ProviderSettings]]:
        pending: list[tuple[str, str, EmailServer | ProviderSettings]] = []
        for email in self.emails:
            if email.incoming.password.get_secret_value() == keyring_store.SENTINEL:
                pending.append((email.account_name, "incoming", email.incoming))
            if email.outgoing and email.outgoing.password.get_secret_value() == keyring_store.SENTINEL:
                pending.append((email.account_name, "outgoing", email.outgoing))
        for provider in self.providers:
            if provider.api_key.get_secret_value() == keyring_store.SENTINEL:
                pending.append((provider.account_name, "api_key", provider))
        return pending

    def _resolve_keyring_sentinels(
        self,
        *,
        migration_load: bool,
        pending: list[tuple[str, str, EmailServer | ProviderSettings]] | None = None,
    ) -> None:
        pending = self._pending_keyring_sentinels() if pending is None else pending
        if not pending:
            return

        if not migration_load and self.effective_credential_storage == "plaintext":
            names = ", ".join(sorted({name for name, _role, _obj in pending}))
            raise ValueError(
                f"Account(s) {names} reference keyring-stored credentials but credential_storage "
                "is 'plaintext'. Run `mcp-email-server migrate-credentials --to plaintext` to "
                "convert them, or unset MCP_EMAIL_SERVER_CREDENTIAL_STORAGE / the credential_storage "
                "setting."
            )

        for account_name, role, obj in pending:
            self._resolve_one_sentinel(account_name, role, obj)

    @staticmethod
    def _resolve_one_sentinel(account_name: str, role: str, obj: EmailServer | ProviderSettings) -> None:
        try:
            value = keyring_store.get_secret(account_name, role)
        except Exception:
            value = None
        if value is None:
            raise ValueError(
                f"Could not resolve credential for account '{account_name}' ({role}) from the OS "
                f"keyring (service '{keyring_store.SERVICE}', entry '{account_name}:{role}'). Re-add "
                "the account, restore access to your OS keyring, or check for a Keychain access "
                "prompt/ACL denial if the server binary changed (e.g. uvx re-resolution)."
            )
        secret = SecretStr(value)
        if role == "api_key":
            obj.api_key = secret
        else:
            obj.password = secret

    @property
    def loaded_keyring_references(self) -> frozenset[tuple[str, str]]:
        """Keyring entries referenced by sentinels in the file at load time."""
        return frozenset(self._loaded_keyring_references)

    @classmethod
    def load_for_migration(cls) -> Settings:
        """Load ignoring env-composited state, so migration transforms the stored config only.

        Skips the credential_storage env override, env-account injection, and the
        bool/allowlist env reads; suppresses the plaintext-sentinel load error so
        sentinels always resolve via keyring regardless of the file's mode.
        """
        global _MIGRATION_LOAD
        _MIGRATION_LOAD = True
        try:
            return cls()
        finally:
            _MIGRATION_LOAD = False

    def add_email(self, email: EmailSettings) -> None:
        """Use re-assigned for validation to work."""
        _reject_sentinel_secret(email.incoming.password, "incoming password")
        if email.outgoing:
            _reject_sentinel_secret(email.outgoing.password, "outgoing password")
        self.emails = [email, *self.emails]

    def add_provider(self, provider: ProviderSettings) -> None:
        """Use re-assigned for validation to work."""
        _reject_sentinel_secret(provider.api_key, "api_key")
        self.providers = [provider, *self.providers]

    def delete_email(self, account_name: str) -> None:
        """Use re-assigned for validation to work."""
        self.emails = [email for email in self.emails if email.account_name != account_name]

    def delete_provider(self, account_name: str) -> None:
        """Use re-assigned for validation to work."""
        self.providers = [provider for provider in self.providers if provider.account_name != account_name]

    def get_account(self, account_name: str, masked: bool = False) -> EmailSettings | ProviderSettings | None:
        for email in self.emails:
            if email.account_name == account_name:
                return email if not masked else email.masked()
        for provider in self.providers:
            if provider.account_name == account_name:
                return provider if not masked else provider.masked()
        return None

    def get_accounts(self, masked: bool = False) -> list[EmailSettings | ProviderSettings]:
        accounts = self.emails + self.providers
        if masked:
            return [account.masked() for account in accounts]
        return accounts

    @model_validator(mode="after")
    def check_unique_account_names(self) -> Settings:
        account_names = set()
        for email in self.emails:
            if email.account_name in account_names:
                raise ValueError(f"Duplicate account name {email.account_name}")
            account_names.add(email.account_name)
        for provider in self.providers:
            if provider.account_name in account_names:
                raise ValueError(f"Duplicate account name {provider.account_name}")
            account_names.add(provider.account_name)

        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (TomlConfigSettingsSource(settings_cls),)

    def _to_toml(self, *, use_keyring: bool = False, credential_storage: str | None = None) -> str:
        context = {"secrets": "keyring"} if use_keyring else None
        data = self.model_dump(exclude_none=True, context=context)
        if credential_storage is not None:
            data["credential_storage"] = credential_storage
        return tomli_w.dumps(data)

    def _reject_cleartext_sentinels(self) -> None:
        """Defense-in-depth: catches sentinel values that bypassed add_email/add_provider
        (e.g. direct ``settings.emails.append(...)``) before they'd be written as a literal
        cleartext password.
        """
        for email in self.emails:
            _reject_sentinel_secret(email.incoming.password, f"'{email.account_name}' incoming password")
            if email.outgoing:
                _reject_sentinel_secret(email.outgoing.password, f"'{email.account_name}' outgoing password")
        for provider in self.providers:
            _reject_sentinel_secret(provider.api_key, f"'{provider.account_name}' api_key")

    def _store_secrets_to_keyring(self) -> list[tuple[str, str, str]]:
        """Push every secret to the keyring; returns (account_name, role, error) for failures."""
        failures: list[tuple[str, str, str]] = []
        for email in self.emails:
            for role, server in (("incoming", email.incoming), ("outgoing", email.outgoing)):
                if server is None:
                    continue
                try:
                    keyring_store.set_secret(email.account_name, role, server.password.get_secret_value())
                except Exception as e:
                    failures.append((email.account_name, role, str(e)))
        for provider in self.providers:
            try:
                keyring_store.set_secret(provider.account_name, "api_key", provider.api_key.get_secret_value())
            except Exception as e:
                failures.append((provider.account_name, "api_key", str(e)))
        return failures

    @staticmethod
    def _write_toml(toml_file: Path, content: str) -> None:
        if os.name != "posix":
            toml_file.write_text(content)
            return
        # Atomic, owner-only write. The file may hold cleartext IMAP/SMTP passwords
        # (plaintext mode), so the new content must never exist in a world-readable
        # file — not even transiently. Writing 0600 onto an *existing* 0644 file and
        # chmod'ing afterwards leaves such a window (and a permanent leak if the
        # chmod fails). Instead: write to a same-directory temp file that is 0600
        # from its first byte (tempfile.mkstemp opens it 0600), fsync it, then
        # os.replace() it over the destination. os.replace is atomic within a
        # filesystem, so a reader sees either the old file or the fully-written new
        # one (already 0600), never a partial or permissive intermediate.
        directory = toml_file.parent
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{toml_file.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, toml_file)
            # fsync the directory so the rename itself (not just the file bytes)
            # survives a crash; best-effort, never fatal to a successful write.
            with contextlib.suppress(OSError):
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def store(self) -> None:
        toml_file = self.model_config["toml_file"]
        toml_file.parent.mkdir(parents=True, exist_ok=True)

        effective = self.effective_credential_storage  # raw literal; never probes
        auto_probe_usable = effective == "auto" and keyring_store.keyring_usable()
        use_keyring = effective == "keyring" or auto_probe_usable

        if use_keyring:
            failures = self._store_secrets_to_keyring()
            if failures:
                if effective == "keyring":
                    detail = "; ".join(f"{name}:{role} ({err})" for name, role, err in failures)
                    raise ValueError(
                        f"Failed to store credential(s) in the OS keyring: {detail}. "
                        "An entry may already exist but be owned by a different application "
                        "(e.g. a previous install path). Remove the stale entries — on macOS: "
                        f"`security delete-generic-password -s {keyring_store.SERVICE}` — and retry, "
                        "or set credential_storage to 'auto' (falls back to plaintext) or 'plaintext'."
                    )
                logger.warning(
                    f"Keyring store failed for {len(failures)} credential(s) in auto mode; "
                    "falling back to plaintext for this write"
                )
                use_keyring = False
        elif effective == "auto":
            logger.warning(
                "No usable OS keyring backend detected; storing credentials in plaintext. Set "
                "MCP_EMAIL_SERVER_CREDENTIAL_STORAGE=keyring to require the keyring, or 'plaintext' "
                "to silence this warning."
            )

        if not use_keyring:
            self._reject_cleartext_sentinels()

        # The environment override determines the representation written above,
        # so persist that effective mode too. Otherwise a plaintext-config + keyring
        # override would produce plaintext mode alongside __KEYRING__ sentinels and
        # become unloadable as soon as the override was removed.
        persisted_storage = effective if self._credential_storage_override is not None else self.credential_storage
        content = self._to_toml(use_keyring=use_keyring, credential_storage=persisted_storage)
        self._write_toml(toml_file, content)
        if self._credential_storage_override is not None:
            self.credential_storage = effective
        logger.info(f"Settings stored in {toml_file} ({'keyring' if use_keyring else 'plaintext'})")


_settings = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if not _settings or reload:
        logger.info(f"Loading settings from {CONFIG_PATH}")
        _settings = Settings()
    return _settings


def clear_settings_cache() -> None:
    """Discard the cached Settings instance.

    Used after a failed store() to stop a divergent in-memory instance (one that
    was mutated before the store raised) from being served by a later
    get_settings() call. get_settings(reload=True) alone is NOT equivalent: if the
    reload itself raises (e.g. a locked keychain plus a sentinel-bearing file — the
    same failure that made store() raise), get_settings keeps the old divergent
    instance rather than discarding it.
    """
    global _settings
    _settings = None


def store_settings(settings: Settings | None = None) -> None:
    if not settings:
        settings = get_settings()
    settings.store()


def _reset_cleanup_mode(raw: dict[str, Any]) -> str:
    override = os.getenv("MCP_EMAIL_SERVER_CREDENTIAL_STORAGE")
    if override is not None:
        if override in _VALID_CREDENTIAL_STORAGE_MODES:
            return override
        logger.warning(
            f"Invalid MCP_EMAIL_SERVER_CREDENTIAL_STORAGE={override!r} while cleaning up keyring "
            "entries during reset; proceeding as if it were unset"
        )
    toml_mode = raw.get("credential_storage", "auto")
    return toml_mode if toml_mode in _VALID_CREDENTIAL_STORAGE_MODES else "auto"


def _cleanup_keyring_entries_for_reset() -> None:
    """Best-effort keyring cleanup for delete_settings(); must never raise.

    Deliberately does NOT construct Settings(): that would hard-fail on sentinel
    resolution exactly when the user's keyring is broken (the scenario `reset` is
    the escape hatch for) and would trigger unwanted side effects (env-account
    injection, env overrides). Parses the raw TOML instead.
    """
    try:
        raw = tomllib.loads(CONFIG_PATH.read_text())
        if _reset_cleanup_mode(raw) == "plaintext":
            return
        for email in raw.get("emails", []):
            name = email.get("account_name")
            if not name:
                continue
            roles = ["incoming"]
            if email.get("outgoing"):
                roles.append("outgoing")
            keyring_store.delete_account_credentials(name, roles)
        for provider in raw.get("providers", []):
            name = provider.get("account_name")
            if name:
                keyring_store.delete_account_credentials(name, ["api_key"])
    except Exception as e:
        logger.warning(
            f"Could not clean up keyring entries for {CONFIG_PATH}: {e}; some entries may remain "
            f"under service '{keyring_store.SERVICE}'"
        )


def delete_settings() -> None:
    if not CONFIG_PATH.exists():
        logger.info(f"Settings file {CONFIG_PATH} does not exist")
        return
    _cleanup_keyring_entries_for_reset()
    CONFIG_PATH.unlink()
    logger.info(f"Deleted settings file {CONFIG_PATH}")
