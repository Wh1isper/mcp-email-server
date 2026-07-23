import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import MarkReadCommand, MutationAccountSnapshot, MutationProviderError


def _mark_read_provider(error: BaseException) -> ClassicMutationProvider:
    handler = MagicMock()
    handler.incoming_client.mark_emails_as_read_with_outcome = AsyncMock(side_effect=error)
    return ClassicMutationProvider(handler)


def _account() -> MutationAccountSnapshot:
    return MutationAccountSnapshot("primary", "managed", (), (), False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        ValueError("invalid mutation"),
        PermissionError("mutation denied"),
    ],
)
async def test_mutation_adapter_preserves_control_and_policy_exceptions(error: BaseException) -> None:
    provider = _mark_read_provider(error)

    with pytest.raises(type(error)) as caught:
        await provider.mark_read(MarkReadCommand("primary", ("1",)), _account())

    assert caught.value is error


@pytest.mark.asyncio
async def test_mutation_adapter_sanitizes_unexpected_provider_failure() -> None:
    provider_detail = "provider-controlled secret detail"
    provider = _mark_read_provider(RuntimeError(provider_detail))

    with pytest.raises(
        MutationProviderError,
        match=r"^provider_failure: mutation provider request failed$",
    ) as caught:
        await provider.mark_read(MarkReadCommand("primary", ("1",)), _account())

    assert provider_detail not in str(caught.value)
    assert caught.value.__cause__ is None
