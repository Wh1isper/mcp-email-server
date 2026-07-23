from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from mcp_email_server.adapters.management import LocalManagementBackend
from mcp_email_server.adapters.metadata import LocalMetadataBackend, LocalMetadataProjectionFactory
from mcp_email_server.adapters.mutations import LocalMutationBackend, LocalMutationProjectionFactory
from mcp_email_server.adapters.reads import LocalArtifactWriter, LocalReadBackend
from mcp_email_server.application.accounts import EffectiveAccountQueryService, EffectiveConfigurationQueryService
from mcp_email_server.application.management import ManagementServices
from mcp_email_server.application.metadata import MetadataQueryService
from mcp_email_server.application.mutations import MutationServices
from mcp_email_server.application.reads import ReadServices
from mcp_email_server.large_results import LocalLargeResultWriter, local_large_results_supported


@dataclass(frozen=True)
class ApplicationRuntime:
    """Process-scoped application services injected into transport adapters."""

    accounts: EffectiveAccountQueryService
    configuration: EffectiveConfigurationQueryService
    management: ManagementServices
    metadata: MetadataQueryService
    mutations: MutationServices
    reads: ReadServices
    large_results: LocalLargeResultWriter | None

    async def aclose(self) -> None:
        """Close process-scoped resources and remove private result artifacts."""
        if self.large_results is not None:
            await self.large_results.aclose()


@cache
def get_application_runtime() -> ApplicationRuntime:
    metadata_backend = LocalMetadataBackend()
    mutation_backend = LocalMutationBackend()
    mutation_services = MutationServices.compose(
        mutation_backend,
        mutation_backend,
        LocalMutationProjectionFactory(mutation_backend),
    )
    read_backend = LocalReadBackend()
    # Inline bounded reads and the management plane remain available; only
    # oversized spill fails closed when owner-only local storage is absent.
    large_results = LocalLargeResultWriter() if local_large_results_supported() else None
    return ApplicationRuntime(
        accounts=EffectiveAccountQueryService(metadata_backend),
        configuration=EffectiveConfigurationQueryService(metadata_backend),
        management=ManagementServices.compose(LocalManagementBackend()),
        metadata=MetadataQueryService(
            metadata_backend,
            metadata_backend,
            LocalMetadataProjectionFactory(metadata_backend),
        ),
        mutations=mutation_services,
        reads=ReadServices.compose(
            read_backend,
            read_backend,
            mutation_services.mark_read,
            LocalArtifactWriter(),
            large_results,
        ),
        large_results=large_results,
    )


async def close_application_runtime() -> None:
    """Close and discard an already-constructed process runtime."""
    if get_application_runtime.cache_info().currsize == 0:
        return
    runtime = get_application_runtime()
    try:
        await runtime.aclose()
    finally:
        get_application_runtime.cache_clear()
