from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from mcp_email_server.adapters.metadata import LocalMetadataBackend, LocalMetadataProjectionFactory
from mcp_email_server.application.accounts import EffectiveAccountQueryService
from mcp_email_server.application.metadata import MetadataQueryService


@dataclass(frozen=True)
class ApplicationRuntime:
    """Process-scoped application services injected into transport adapters."""

    accounts: EffectiveAccountQueryService
    metadata: MetadataQueryService


@cache
def get_application_runtime() -> ApplicationRuntime:
    backend = LocalMetadataBackend()
    return ApplicationRuntime(
        accounts=EffectiveAccountQueryService(backend),
        metadata=MetadataQueryService(backend, backend, LocalMetadataProjectionFactory(backend)),
    )
