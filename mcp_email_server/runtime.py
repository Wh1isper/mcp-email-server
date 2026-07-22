from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from mcp_email_server.adapters.metadata import LocalMetadataBackend, LocalMetadataProjectionFactory
from mcp_email_server.adapters.mutations import LocalMutationBackend, LocalMutationProjectionFactory
from mcp_email_server.application.accounts import EffectiveAccountQueryService
from mcp_email_server.application.metadata import MetadataQueryService
from mcp_email_server.application.mutations import MutationServices


@dataclass(frozen=True)
class ApplicationRuntime:
    """Process-scoped application services injected into transport adapters."""

    accounts: EffectiveAccountQueryService
    metadata: MetadataQueryService
    mutations: MutationServices


@cache
def get_application_runtime() -> ApplicationRuntime:
    metadata_backend = LocalMetadataBackend()
    mutation_backend = LocalMutationBackend()
    return ApplicationRuntime(
        accounts=EffectiveAccountQueryService(metadata_backend),
        metadata=MetadataQueryService(
            metadata_backend,
            metadata_backend,
            LocalMetadataProjectionFactory(metadata_backend),
        ),
        mutations=MutationServices.compose(
            mutation_backend,
            mutation_backend,
            LocalMutationProjectionFactory(mutation_backend),
        ),
    )
