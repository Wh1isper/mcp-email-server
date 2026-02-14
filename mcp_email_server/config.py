from __future__ import annotations

import datetime
import os
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from mcp_email_server.log import logger

DEFAILT_CONFIG_PATH = "~/.config/zerolib/mcp_email_server/config.toml"

CONFIG_PATH = Path(os.getenv("MCP_EMAIL_SERVER_CONFIG_PATH", DEFAILT_CONFIG_PATH)).expanduser().resolve()


class ConnectionSecurity(str, Enum):
    """Connection security mode per RFC 8314.

    - TLS: Implicit TLS — encrypted from the first byte (IMAP port 993, SMTP port 465)
    - STARTTLS: Connect plaintext, then upgrade via STARTTLS command (IMAP port 143, SMTP port 587)
    - NONE: No encryption (not recommended, only for trusted local connections)
    """

    TLS = "tls"
    STARTTLS = "starttls"
    NONE = "none"


class EmailServer(BaseModel):
    user_name: str
    password: str
    host: str
    port: int
    security: ConnectionSecurity = ConnectionSecurity.TLS
    verify_ssl: bool = True  # Set to False for self-signed certificates (e.g., ProtonMail Bridge)

    # Deprecated: use `security` instead. Kept for backward compatibility with existing configs.
    use_ssl: bool | None = None
    start_ssl: bool | None = None

    @model_validator(mode="after")
    @classmethod
    def resolve_security_from_legacy(cls, obj: EmailServer) -> EmailServer:
        """Derive `security` from deprecated `use_ssl`/`start_ssl` for backward compatibility.

        If both old and new fields are set, `security` takes precedence.
        If only old fields are set, derive `security` from them.
        """
        # If legacy fields are explicitly set and security was not explicitly provided
        # (i.e. it's at default), derive security from legacy fields
        if obj.use_ssl is not None or obj.start_ssl is not None:
            use_ssl = obj.use_ssl if obj.use_ssl is not None else False
            start_ssl = obj.start_ssl if obj.start_ssl is not None else False

            if use_ssl and start_ssl:
                raise ValueError(
                    "Invalid configuration: 'use_ssl' and 'start_ssl' cannot both be true. "
                    "Use 'security = \"tls\"' for implicit TLS or 'security = \"starttls\"' for STARTTLS."
                )

            if use_ssl:
                obj.security = ConnectionSecurity.TLS
            elif start_ssl:
                obj.security = ConnectionSecurity.STARTTLS
            else:
                obj.security = ConnectionSecurity.NONE

        return obj

    def masked(self) -> EmailServer:
        return self.model_copy(update={"password": "********"})


class AccountAttributes(BaseModel):
    model_config = ConfigDict(json_encoders={datetime.datetime: lambda v: v.isoformat()})
    account_name: str
    description: str = ""
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(ZoneInfo("UTC")))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(ZoneInfo("UTC")))

    @model_validator(mode="after")
    @classmethod
    def update_updated_at(cls, obj: AccountAttributes) -> AccountAttributes:
        """Update updated_at field."""
        # must disable validation to avoid infinite loop
        obj.model_config["validate_assignment"] = False

        # update updated_at field
        obj.updated_at = datetime.datetime.now(ZoneInfo("UTC"))

        # enable validation again
        obj.model_config["validate_assignment"] = True
        return obj

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
    outgoing: EmailServer
    save_to_sent: bool = True  # Save sent emails to IMAP Sent folder
    sent_folder_name: str | None = None  # Override Sent folder name (auto-detect if None)

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
        smtp_host: str,
        imap_user_name: str | None = None,
        imap_password: str | None = None,
        imap_port: int = 993,
        imap_security: ConnectionSecurity = ConnectionSecurity.TLS,
        imap_verify_ssl: bool = True,
        smtp_port: int = 465,
        smtp_security: ConnectionSecurity = ConnectionSecurity.TLS,
        smtp_verify_ssl: bool = True,
        smtp_user_name: str | None = None,
        smtp_password: str | None = None,
        save_to_sent: bool = True,
        sent_folder_name: str | None = None,
        # Deprecated parameters for backward compatibility
        imap_ssl: bool | None = None,
        smtp_ssl: bool | None = None,
        smtp_start_ssl: bool | None = None,
    ) -> EmailSettings:
        # Build incoming server config
        incoming_kwargs: dict[str, Any] = {
            "user_name": imap_user_name or user_name,
            "password": imap_password or password,
            "host": imap_host,
            "port": imap_port,
            "verify_ssl": imap_verify_ssl,
        }
        if imap_ssl is not None:
            # Legacy path: use_ssl was explicitly passed
            incoming_kwargs["use_ssl"] = imap_ssl
        else:
            incoming_kwargs["security"] = imap_security

        # Build outgoing server config
        outgoing_kwargs: dict[str, Any] = {
            "user_name": smtp_user_name or user_name,
            "password": smtp_password or password,
            "host": smtp_host,
            "port": smtp_port,
            "verify_ssl": smtp_verify_ssl,
        }
        if smtp_ssl is not None or smtp_start_ssl is not None:
            # Legacy path: use_ssl/start_ssl were explicitly passed
            if smtp_ssl is not None:
                outgoing_kwargs["use_ssl"] = smtp_ssl
            if smtp_start_ssl is not None:
                outgoing_kwargs["start_ssl"] = smtp_start_ssl
        else:
            outgoing_kwargs["security"] = smtp_security

        return cls(
            account_name=account_name,
            full_name=full_name,
            email_address=email_address,
            incoming=EmailServer(**incoming_kwargs),
            outgoing=EmailServer(**outgoing_kwargs),
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
        - MCP_EMAIL_SERVER_IMAP_SECURITY (default: "tls") — "tls", "starttls", or "none"
        - MCP_EMAIL_SERVER_IMAP_VERIFY_SSL (default: true)
        - MCP_EMAIL_SERVER_SMTP_HOST
        - MCP_EMAIL_SERVER_SMTP_PORT (default: 465)
        - MCP_EMAIL_SERVER_SMTP_SECURITY (default: "tls") — "tls", "starttls", or "none"
        - MCP_EMAIL_SERVER_SMTP_VERIFY_SSL (default: true)
        - MCP_EMAIL_SERVER_SAVE_TO_SENT (default: true)
        - MCP_EMAIL_SERVER_SENT_FOLDER_NAME (default: auto-detect)

        Deprecated (still supported for backward compatibility):
        - MCP_EMAIL_SERVER_IMAP_SSL → use MCP_EMAIL_SERVER_IMAP_SECURITY instead
        - MCP_EMAIL_SERVER_SMTP_SSL → use MCP_EMAIL_SERVER_SMTP_SECURITY instead
        - MCP_EMAIL_SERVER_SMTP_START_SSL → use MCP_EMAIL_SERVER_SMTP_SECURITY instead
        """
        # Check if minimum required environment variables are set
        email_address = os.getenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS")
        password = os.getenv("MCP_EMAIL_SERVER_PASSWORD")

        if not email_address or not password:
            return None

        # Parse boolean values
        def parse_bool(value: str | None, default: bool = True) -> bool:
            if value is None:
                return default
            return value.lower() in ("true", "1", "yes", "on")

        def parse_security(
            value: str | None, default: ConnectionSecurity = ConnectionSecurity.TLS
        ) -> ConnectionSecurity:
            if value is None:
                return default
            try:
                return ConnectionSecurity(value.lower())
            except ValueError:
                logger.warning(f"Invalid security value '{value}', using default '{default.value}'")
                return default

        # Get all environment variables with defaults
        account_name = os.getenv("MCP_EMAIL_SERVER_ACCOUNT_NAME", "default")
        full_name = os.getenv("MCP_EMAIL_SERVER_FULL_NAME", email_address.split("@")[0])
        user_name = os.getenv("MCP_EMAIL_SERVER_USER_NAME", email_address)
        imap_host = os.getenv("MCP_EMAIL_SERVER_IMAP_HOST")
        smtp_host = os.getenv("MCP_EMAIL_SERVER_SMTP_HOST")

        # Required fields check
        if not imap_host or not smtp_host:
            logger.warning("Missing required email configuration environment variables (IMAP_HOST or SMTP_HOST)")
            return None

        # Resolve security settings: new env vars take precedence over legacy
        imap_security_env = os.getenv("MCP_EMAIL_SERVER_IMAP_SECURITY")
        smtp_security_env = os.getenv("MCP_EMAIL_SERVER_SMTP_SECURITY")
        imap_ssl_env = os.getenv("MCP_EMAIL_SERVER_IMAP_SSL")
        smtp_ssl_env = os.getenv("MCP_EMAIL_SERVER_SMTP_SSL")
        smtp_start_ssl_env = os.getenv("MCP_EMAIL_SERVER_SMTP_START_SSL")

        try:
            imap_port = int(os.getenv("MCP_EMAIL_SERVER_IMAP_PORT", "993"))
            smtp_port = int(os.getenv("MCP_EMAIL_SERVER_SMTP_PORT", "465"))
        except ValueError as e:
            logger.error(f"Invalid port configuration: {e}")
            return None

        init_kwargs: dict[str, Any] = {
            "account_name": account_name,
            "full_name": full_name,
            "email_address": email_address,
            "user_name": user_name,
            "password": password,
            "imap_host": imap_host,
            "imap_port": imap_port,
            "imap_verify_ssl": parse_bool(os.getenv("MCP_EMAIL_SERVER_IMAP_VERIFY_SSL"), True),
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_verify_ssl": parse_bool(os.getenv("MCP_EMAIL_SERVER_SMTP_VERIFY_SSL"), True),
            "smtp_user_name": os.getenv("MCP_EMAIL_SERVER_SMTP_USER_NAME", user_name),
            "smtp_password": os.getenv("MCP_EMAIL_SERVER_SMTP_PASSWORD", password),
            "imap_user_name": os.getenv("MCP_EMAIL_SERVER_IMAP_USER_NAME", user_name),
            "imap_password": os.getenv("MCP_EMAIL_SERVER_IMAP_PASSWORD", password),
            "save_to_sent": parse_bool(os.getenv("MCP_EMAIL_SERVER_SAVE_TO_SENT"), True),
            "sent_folder_name": os.getenv("MCP_EMAIL_SERVER_SENT_FOLDER_NAME"),
        }

        # IMAP security
        if imap_security_env is not None:
            init_kwargs["imap_security"] = parse_security(imap_security_env)
        elif imap_ssl_env is not None:
            init_kwargs["imap_ssl"] = parse_bool(imap_ssl_env, True)

        # SMTP security
        if smtp_security_env is not None:
            init_kwargs["smtp_security"] = parse_security(smtp_security_env)
        elif smtp_ssl_env is not None or smtp_start_ssl_env is not None:
            if smtp_ssl_env is not None:
                init_kwargs["smtp_ssl"] = parse_bool(smtp_ssl_env, True)
            if smtp_start_ssl_env is not None:
                init_kwargs["smtp_start_ssl"] = parse_bool(smtp_start_ssl_env, False)

        try:
            return cls.init(**init_kwargs)
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to create email settings from environment variables: {e}")
            return None

    def masked(self) -> EmailSettings:
        return self.model_copy(
            update={
                "incoming": self.incoming.masked(),
                "outgoing": self.outgoing.masked(),
            }
        )


class ProviderSettings(AccountAttributes):
    provider_name: str
    api_key: str

    def masked(self) -> AccountAttributes:
        return self.model_copy(update={"api_key": "********"})


def _parse_bool_env(value: str | None, default: bool = False) -> bool:
    """Parse boolean value from environment variable."""
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


class Settings(BaseSettings):
    emails: list[EmailSettings] = []
    providers: list[ProviderSettings] = []
    db_location: str = CONFIG_PATH.with_name("db.sqlite3").as_posix()
    enable_attachment_download: bool = False

    model_config = SettingsConfigDict(toml_file=CONFIG_PATH, validate_assignment=True, revalidate_instances="always")

    def __init__(self, **data: Any) -> None:
        """Initialize Settings with support for environment variables."""
        super().__init__(**data)

        # Check for enable_attachment_download from environment variable
        env_enable_attachment = os.getenv("MCP_EMAIL_SERVER_ENABLE_ATTACHMENT_DOWNLOAD")
        if env_enable_attachment is not None:
            self.enable_attachment_download = _parse_bool_env(env_enable_attachment, False)
            logger.info(f"Set enable_attachment_download={self.enable_attachment_download} from environment variable")

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
    @classmethod
    def check_unique_account_names(cls, obj: Settings) -> Settings:
        account_names = set()
        for email in obj.emails:
            if email.account_name in account_names:
                raise ValueError(f"Duplicate account name {email.account_name}")
            account_names.add(email.account_name)
        for provider in obj.providers:
            if provider.account_name in account_names:
                raise ValueError(f"Duplicate account name {provider.account_name}")
            account_names.add(provider.account_name)

        return obj

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
    return


def delete_settings() -> None:
    if not CONFIG_PATH.exists():
        logger.info(f"Settings file {CONFIG_PATH} does not exist")
        return
    CONFIG_PATH.unlink()
    logger.info(f"Deleted settings file {CONFIG_PATH}")
