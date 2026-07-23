from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.runtime import ApplicationRuntime, close_application_runtime, get_application_runtime


@pytest.mark.asyncio
async def test_close_application_runtime_closes_cached_instance_and_rebuilds() -> None:
    get_application_runtime.cache_clear()
    first = get_application_runtime()
    close = AsyncMock()

    with patch.object(ApplicationRuntime, "aclose", close):
        await close_application_runtime()

    close.assert_awaited_once_with()
    second = get_application_runtime()
    assert second is not first
    get_application_runtime.cache_clear()


@pytest.mark.asyncio
async def test_runtime_keeps_inline_and_management_services_when_secure_spill_is_unavailable() -> None:
    get_application_runtime.cache_clear()
    with patch("mcp_email_server.runtime.local_large_results_supported", return_value=False):
        runtime = get_application_runtime()

    assert runtime.large_results is None
    assert runtime.management is not None
    assert runtime.reads is not None
    await close_application_runtime()


@pytest.mark.asyncio
async def test_runtime_construction_does_not_allocate_optional_spill_storage() -> None:
    get_application_runtime.cache_clear()
    with patch("mcp_email_server.large_results.tempfile.mkdtemp", side_effect=OSError("unavailable")):
        runtime = get_application_runtime()

    assert runtime.management is not None
    assert runtime.reads is not None
    await close_application_runtime()


@pytest.mark.asyncio
async def test_close_application_runtime_does_not_construct_unused_runtime() -> None:
    get_application_runtime.cache_clear()

    await close_application_runtime()

    assert get_application_runtime.cache_info().currsize == 0
