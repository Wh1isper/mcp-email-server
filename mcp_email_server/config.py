from __future__ import annotations

import datetime
import fnmatch
import os
from collections.abc import Iterable
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import tomli_w
from pydantic import BaseModel, Field, SecretStr, field_serializer, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from mcp_email_server.log import logger

DEFAULT_CONFIG_PATH = "~/.config/zerolib/mcp_email_server/config.toml"


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
    def serialize_password(self, v: SecretStr) -> str:
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
    def serialize_api_key(self, v: SecretStr) -> str:
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

    model_config = SettingsConfigDict(toml_file=CONFIG_PATH, validate_assignment=True, revalidate_instances="always")

    def _apply_bool_env_override(self, attr: str, env_var: str) -> None:
        value = os.getenv(env_var)
        if value is not None:
            setattr(self, attr, _parse_bool_env(value, False))
            logger.info(f"Set {attr}={getattr(self, attr)} from environment variable")

    def __init__(self, **data: Any) -> None:
        """Initialize Settings with support for environment variables."""
        super().__init__(**data)

        self._apply_bool_env_override("enable_attachment_download", "MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD")
        self._apply_bool_env_override("report_blocked_mutations", "MCP_EMAIL_SERVER_REPORT_BLOCKED_MUTATIONS")

        # Normalise allowed_recipients from TOML (bare, lowercased, de-duplicated)
        if self.allowed_recipients:
            self.allowed_recipients = _normalize_address_list(self.allowed_recipients)

        # Environment variable overrides TOML (comma-separated); an empty string clears the allowlist.
        env_allowed = os.getenv("MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS")
        if env_allowed is not None:
            self.allowed_recipients = _normalize_address_list(env_allowed.split(","))

        # Normalise allowed_senders from TOML (lowercased, de-duplicated; globs preserved)
        if self.allowed_senders:
            self.allowed_senders = _normalize_pattern_list(self.allowed_senders)

        # Environment variable overrides TOML (comma-separated); an empty string clears the allowlist.
        env_senders = os.getenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS")
        if env_senders is not None:
            self.allowed_senders = _normalize_pattern_list(env_senders.split(","))

        # Check for email configuration from environment variables
        env_email = EmailSettings.from_env()
        if env_email:
            # Check if this account already exists (from TOML)
            existing_account = None
            for i, email in enumerate(self.emails):
                if email.account_name == env_email.account_name:
                    existing_account = i
                    break

            if existing_account is not None:
                # Replace existing account with env configuration
                self.emails[existing_account] = env_email
                logger.info(f"Overriding email account '{env_email.account_name}' with environment variables")
            else:
                # Add new account from env
                self.emails.insert(0, env_email)
                logger.info(f"Added email account '{env_email.account_name}' from environment variables")

    def add_email(self, email: EmailSettings) -> None:
        """Use re-assigned for validation to work."""
        self.emails = [email, *self.emails]

    def add_provider(self, provider: ProviderSettings) -> None:
        """Use re-assigned for validation to work."""
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

    def _to_toml(self) -> str:
        data = self.model_dump(exclude_none=True)
        return tomli_w.dumps(data)

    def store(self) -> None:
        toml_file = self.model_config["toml_file"]
        toml_file.parent.mkdir(parents=True, exist_ok=True)
        toml_file.write_text(self._to_toml())
        logger.info(f"Settings stored in {toml_file}")


_settings = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if not _settings or reload:
        logger.info(f"Loading settings from {CONFIG_PATH}")
        _settings = Settings()
    return _settings


def store_settings(settings: Settings | None = None) -> None:
    if not settings:
        settings = get_settings()
    settings.store()


def delete_settings() -> None:
    if not CONFIG_PATH.exists():
        logger.info(f"Settings file {CONFIG_PATH} does not exist")
        return
    CONFIG_PATH.unlink()
    logger.info(f"Deleted settings file {CONFIG_PATH}")
