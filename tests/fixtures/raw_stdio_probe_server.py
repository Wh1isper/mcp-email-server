from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import anyio

from mcp_email_server import app as app_module
from mcp_email_server.application.reads import ListMailboxesQuery
from mcp_email_server.runtime import get_application_runtime
from mcp_email_server.stdio import run_bounded_stdio


async def _blocked_list_mailboxes(_query: ListMailboxesQuery) -> list[object]:
    marker_directory = Path(sys.argv[1])
    (marker_directory / "started").write_text("started", encoding="utf-8")
    try:
        await anyio.sleep_forever()
    except anyio.get_cancelled_exc_class():
        (marker_directory / "cancelled").write_text("cancelled", encoding="utf-8")
        raise
    raise AssertionError("Blocking probe returned without cancellation")


async def _allocate_cleanup_probe(marker_directory: Path) -> None:
    writer = get_application_runtime().large_results
    if writer is None:
        raise RuntimeError("Large-result cleanup probe is unavailable")
    reference = await writer.write(prefix="stdio-cleanup", content=b"cleanup-probe")
    (marker_directory / "artifact-path").write_text(reference.output_file_path, encoding="utf-8")


def main() -> None:
    marker_directory = Path(sys.argv[1])
    asyncio.run(_allocate_cleanup_probe(marker_directory))
    app_module.list_mailboxes_query = _blocked_list_mailboxes
    run_bounded_stdio(app_module.mcp)


if __name__ == "__main__":
    main()
