# Fix: Recursion Overflow on Large Mailboxes

**Branch**: `pr/recursion-overflow-fix`
**Status**: Upstream PR (builds on #151)

## Problem

When `list_emails_metadata` is called on a mailbox with >1000 messages, the MCP
server hangs indefinitely. No timeout, no error — just silence.

### Root Cause Chain

1. `get_emails_metadata_stream()` calls `uid_search("ALL")` — returns all UIDs
2. `_batch_fetch_dates()` fetches `INTERNALDATE` for **all** UIDs to sort by date
3. With `chunk_size=5000` (default), all UIDs go in one IMAP FETCH command
4. `aioimaplib` (v2.0.1) uses **recursive** `_handle_responses()` to parse response lines
5. With >1000 response lines, Python's default recursion limit (1000) is exceeded
6. `RecursionError` is raised inside the asyncio event loop
7. The exception is swallowed — the MCP tool never returns a result

Additionally, `asyncio.gather(*tasks)` was used to fetch chunks in parallel on a
**single** IMAP connection. IMAP is a sequential protocol — parallel commands on
one connection cause undefined behaviour (RFC 9051 Section 5.5).

### Reproduction

Any mailbox with >1000 messages triggers the issue:

```
RecursionError: maximum recursion depth exceeded
  _handle_responses(tail, line_handler)  [Previous line repeated 973 more times]
```

## Fix Applied

Upstream #151 addressed the chunk_size (5000 -> 500). This PR adds three
additional resilience improvements:

### File: `mcp_email_server/emails/classic.py`

#### Change 1: Sequential instead of parallel chunk processing

```python
# Before (parallel — protocol violation on single IMAP connection)
tasks = [self._fetch_dates_chunk(imap, chunk, ...) for chunk in chunks]
results = await asyncio.gather(*tasks)

# After (sequential — correct for IMAP protocol)
uid_dates: dict[str, datetime] = {}
for chunk_num, chunk in enumerate(chunks, 1):
    chunk_dates = await self._fetch_dates_chunk(imap, chunk, ...)
    uid_dates.update(chunk_dates)
```

#### Change 2: Add timeout to prevent indefinite hangs

```python
# Before
_, data = await imap.uid("fetch", uid_list, "(INTERNALDATE)")

# After
_, data = await asyncio.wait_for(
    imap.uid("fetch", uid_list, "(INTERNALDATE)"),
    timeout=timeout,  # default 30s
)
```

## Verification

| Metric         | Before                | After                            |
| -------------- | --------------------- | -------------------------------- |
| >1000 messages | RecursionError / hang | Completes normally               |
| Chunks         | 1 (all UIDs)          | N x 500                          |
| Test suite     | 140 passed            | 142 passed (+2 regression tests) |
| `make check`   | clean                 | clean                            |

Validated in production on a mailbox with >4,000 messages: completes in
under 1 second across 9 sequential chunks.

### Regression Tests Added

- `test_batch_fetch_dates_chunks_large_uid_lists`: Verifies 1500 UIDs split into 3 chunks
- `test_batch_fetch_dates_sequential_not_parallel`: Verifies chunks execute serially
