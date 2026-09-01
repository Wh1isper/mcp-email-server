from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from mcp_email_server.application.management import (
    EndpointPatch,
    EndpointSummary,
    ManagedPolicy,
    UpdateAccountCommand,
)
from mcp_email_server.imap_keywords import ImapKeywordTag


class RequestModel(BaseModel):
    """Strict JSON request base for the local management adapter."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False)


class EmptyRequest(RequestModel):
    pass


class CatalogTargetRequest(RequestModel):
    expected_bootstrap_revision: int = Field(ge=0)
    expected_catalog: str = Field(min_length=1, max_length=4096)


class ExpectedRevisionRequest(CatalogTargetRequest):
    expected_revision: int = Field(ge=1)


class InitializeDefaultCatalogRequest(RequestModel):
    expected_bootstrap_revision: int = Field(ge=0)
    require_empty_install: bool


class SelectCatalogRequest(RequestModel):
    mode: Literal["legacy", "managed"]
    expected_bootstrap_revision: int = Field(ge=0)
    expected_catalog_revision: int | None = Field(default=None, ge=1)


class EndpointInput(RequestModel):
    host: str = Field(min_length=1, max_length=65535)
    port: int = Field(ge=1, le=65535)
    use_ssl: bool
    start_ssl: bool
    verify_ssl: bool
    user_name: str = Field(min_length=1, max_length=65535)

    @model_validator(mode="after")
    def validate_tls_mode(self) -> EndpointInput:
        if self.use_ssl and self.start_ssl:
            raise ValueError("TLS from connection start and STARTTLS are mutually exclusive")
        return self

    def summary(self) -> EndpointSummary:
        return EndpointSummary(**self.model_dump())

    def patch(self) -> EndpointPatch:
        return EndpointPatch(**self.model_dump())


class CredentialInput(RequestModel):
    incoming: SecretStr
    outgoing: SecretStr | None


class CreateAccountRequest(CatalogTargetRequest):
    expected_catalog_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=65535)
    full_name: str = Field(min_length=1, max_length=65535)
    email_address: str = Field(min_length=1, max_length=65535)
    save_to_sent: bool
    sent_folder_name: str | None = Field(default=None, max_length=65535)
    incoming: EndpointInput
    outgoing: EndpointInput | None
    credentials: CredentialInput
    tags: tuple[ImapKeywordTag, ...] = ()

    @model_validator(mode="after")
    def validate_outgoing_credential(self) -> CreateAccountRequest:
        if (self.outgoing is None) != (self.credentials.outgoing is None):
            raise ValueError("Outgoing endpoint and credential must be provided together")
        if not self.credentials.incoming.get_secret_value():
            raise ValueError("Incoming credential must not be empty")
        if self.credentials.outgoing is not None and not self.credentials.outgoing.get_secret_value():
            raise ValueError("Outgoing credential must not be empty")
        return self


class UpdateAccountRequest(CatalogTargetRequest):
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=65535)
    full_name: str = Field(min_length=1, max_length=65535)
    email_address: str = Field(min_length=1, max_length=65535)
    save_to_sent: bool
    sent_folder_name: str | None = Field(default=None, max_length=65535)
    incoming: EndpointInput
    outgoing: EndpointInput | None
    tags: tuple[ImapKeywordTag, ...] = ()

    def command(self, current_name: str, *, had_outgoing: bool) -> UpdateAccountCommand:
        return UpdateAccountCommand(
            name=current_name,
            expected_revision=self.expected_revision,
            new_name=self.name if self.name != current_name else None,
            full_name=self.full_name,
            email_address=self.email_address,
            incoming=self.incoming.patch(),
            outgoing=self.outgoing.patch() if self.outgoing is not None else None,
            remove_outgoing=had_outgoing and self.outgoing is None,
            save_to_sent=self.save_to_sent,
            sent_folder_name=self.sent_folder_name,
            update_sent_folder=True,
            tags=self.tags,
        )


class AccountLifecycleRequest(ExpectedRevisionRequest):
    pass


class RemoveAccountRequest(ExpectedRevisionRequest):
    confirmation: str = Field(min_length=1, max_length=65535)


class SetCredentialRequest(ExpectedRevisionRequest):
    secret: SecretStr

    @model_validator(mode="after")
    def validate_secret(self) -> SetCredentialRequest:
        if not self.secret.get_secret_value():
            raise ValueError("Credential must not be empty")
        return self


class CleanupCredentialsRequest(ExpectedRevisionRequest):
    limit: int = Field(ge=1, le=100)


class UpdatePolicyRequest(CatalogTargetRequest):
    expected_revision: int = Field(ge=1)
    enable_attachment_download: bool
    enable_attachment_content: bool
    allowed_recipients: tuple[str, ...]
    allowed_senders: tuple[str, ...]
    report_blocked_mutations: bool

    def policy(self) -> ManagedPolicy:
        return ManagedPolicy(
            revision=self.expected_revision,
            enable_attachment_download=self.enable_attachment_download,
            enable_attachment_content=self.enable_attachment_content,
            allowed_recipients=self.allowed_recipients,
            allowed_senders=self.allowed_senders,
            report_blocked_mutations=self.report_blocked_mutations,
        )


class ApplyImportRequest(RequestModel):
    expected_revision: int = Field(ge=1)
    preview_token: str = Field(min_length=1, max_length=4096)
    confirmation: str = Field(max_length=64)
