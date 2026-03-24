# Sender Allowlist Design

**Date:** 2026-03-24
**Feature:** Global sender allowlist for `list_emails_metadata` and `get_emails_content`
**Repo:** [ai-zerolab/mcp-email-server](https://github.com/ai-zerolab/mcp-email-server)
**Related:** PR #139 (recipient allowlist, merged/open)

---

## Goal

Shield the MCP client from emails sent by untrusted senders — including spam and prompt-injection attempts — by filtering them out at the tool layer before they are ever surfaced to the AI. The emails remain untouched in the mailbox; only the AI's view is restricted.

---

## Design Decisions

### Filter at the tool layer, not IMAP

Emails are **not moved or deleted**. The filter is applied in `app.py` after the IMAP handler returns results. This means:

- Existing IMAP infrastructure is unchanged.
- The user's normal mail client still sees all emails.
- The feature is reversible (remove config → full visibility restored immediately).

### Glob pattern support

`allowed_senders` entries are glob patterns matched with `fnmatch`. This supports:

- Exact addresses: `alice@example.com`
- Domain wildcards: `*@glez.de`
- Prefix wildcards: `alice*@example.com`

Patterns are normalised to lowercase at parse time. Matching is case-insensitive (incoming address lowercased before comparison).

### Sender address extraction

The IMAP `From` header is often in `"Name <email@domain>"` format. `email.utils.parseaddr()` (stdlib) extracts the address portion. If parsing fails (e.g. malformed header returning `('', '')`), the raw string is used as a fallback. An unparseable sender will never match a normal pattern like `*@example.com` and is therefore treated as **not allowed** when an allowlist is configured — the safe direction for the threat model.

### Separate from `allowed_recipients`

`allowed_senders` is a distinct config field from `allowed_recipients`. The two lists address different threat models:

- `allowed_recipients` — controls where the AI can **send** (prevents accidental outbound sends)
- `allowed_senders` — controls whose emails the AI can **read** (prevents prompt injection / spam influence)

### Empty list = allow all

`allowed_senders = []` (the default) allows the AI to read emails from any sender. This preserves full backwards compatibility.

---

## Configuration

### TOML (top-level only)

Must be placed at the **root** of the config file, **not** inside an `[[emails]]` block. If placed inside `[[emails]]`, the field is silently ignored with no error — the allowlist will not take effect.

```toml
# ✅ Correct — top-level
allowed_senders = ["*@trusted-company.com", "alice@example.com"]

[[emails]]
account_name = "work"
# ...
```

```toml
# ❌ Wrong — silently ignored, allowlist has no effect
[[emails]]
account_name = "work"
allowed_senders = ["*@trusted-company.com"]   # inside [[emails]] block!
# ...
```

### Environment variable

Comma-separated list; takes precedence over TOML:

```
MCP_EMAIL_SERVER_ALLOWED_SENDERS=*@trusted-company.com,alice@example.com
```

---

## Implementation

### `mcp_email_server/config.py`

Add to `Settings`:

```python
allowed_senders: list[str] = []
```

In `Settings.__init__`, after the `allowed_recipients` blocks:

```python
# Normalise allowed_senders from TOML: lowercase and deduplicate
if self.allowed_senders:
    self.allowed_senders = list(
        dict.fromkeys(p.strip().lower() for p in self.allowed_senders if p.strip())
    )

# Parse allowed_senders from environment variable (comma-separated)
# Env var takes precedence over TOML-configured value
env_senders = os.getenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS")
if env_senders:
    self.allowed_senders = list(
        dict.fromkeys(p.strip().lower() for p in env_senders.split(",") if p.strip())
    )
```

### `mcp_email_server/app.py`

**New imports** (stdlib only):

```python
import fnmatch
from email import utils as email_utils
```

**New private helper:**

```python
def _sender_allowed(sender: str, patterns: list[str]) -> bool:
    """Return True if sender matches any pattern in the allowlist, or if the list is empty.

    Unparseable sender strings (malformed From headers) are treated as not allowed
    when an allowlist is configured — the safe default for the threat model.
    """
    if not patterns:
        return True
    _, addr = email_utils.parseaddr(sender)   # handles "Name <addr>" and bare addresses
    addr = (addr or sender).lower()           # fallback to raw string if parse fails
    return any(fnmatch.fnmatch(addr, pattern) for pattern in patterns)
```

**New tool — `list_allowed_senders`:**

Insert between `list_allowed_recipients` and `send_email`:

```python
@mcp.tool(
    description=(
        "List the globally allowed sender email address patterns. "
        "Returns an empty list if no allowlist is configured, meaning emails from all senders are visible. "
        "Patterns may include wildcards (e.g. *@example.com). "
        "Call this tool to understand which senders the MCP client is permitted to read."
    )
)
async def list_allowed_senders() -> list[str]:
    return get_settings().allowed_senders
```

**Updated `list_emails_metadata`** — read settings first, filter after handler call:

```python
# Read settings before the IMAP call to avoid round-trip when allowlist is empty
allowed = get_settings().allowed_senders
result = await handler.get_emails_metadata(...)
if allowed:
    result.emails = [e for e in result.emails if _sender_allowed(e.sender, allowed)]
return result
```

Note: `result.total` reflects the server-side IMAP count and is **not** adjusted (see Known Limitations).

**Updated `get_emails_content`** — filter and adjust count:

```python
# Read settings before the IMAP call to avoid round-trip when allowlist is empty
allowed = get_settings().allowed_senders
result = await handler.get_emails_content(email_ids, mailbox)
if allowed:
    result.emails = [e for e in result.emails if _sender_allowed(e.sender, allowed)]
    result.retrieved_count = len(result.emails)
return result
```

Blocked emails are silently dropped. They are **not** added to `result.failed_ids` — doing so would reveal to the AI that a blocked email exists.

> **Testing note:** All filtering tests for `list_emails_metadata` and `get_emails_content` must patch both `mcp_email_server.app.dispatch_handler` and `mcp_email_server.app.get_settings`. Follow the pattern from `test_send_email`, which patches both.

---

## Known Limitations

### `total` count in `list_emails_metadata`

The `total` field is returned by the IMAP server-side search and reflects the total number of matching emails before filtering. Post-filter, the actual number of emails in `emails` may be lower than `total`. Correcting `total` would require fetching all pages upfront, which is impractical. This is a documented limitation.

### `download_attachment` not filtered

The `download_attachment` tool accepts an `email_id` and `attachment_name` directly, with no sender information available without a pre-fetch. Because the AI can only learn `email_id` values from `list_emails_metadata` and `get_emails_content` (both of which apply the filter), a filtered sender's attachment IDs are never surfaced to the AI in practice. Adding a pre-fetch to `download_attachment` is left for a future iteration if needed.

---

## Testing

### Config tests (`tests/test_config.py`, `tests/test_env_config_coverage.py`)

| Test                                      | Description                                                   |
| ----------------------------------------- | ------------------------------------------------------------- |
| `test_allowed_senders_defaults_to_empty`  | `allowed_senders = []` by default                             |
| `test_allowed_senders_toml_normalised`    | Patterns lowercased, deduplicated (`*@GLEZ.DE` → `*@glez.de`) |
| `test_allowed_senders_from_env`           | Comma-separated env var parsed and normalised                 |
| `test_allowed_senders_env_empty_string`   | Empty env var leaves list empty                               |
| `test_allowed_senders_env_overrides_toml` | Env var takes precedence over TOML                            |

### App tests (`tests/test_mcp_tools.py`)

All tests that exercise filtering must patch both `mcp_email_server.app.dispatch_handler` and `mcp_email_server.app.get_settings` (follow the `test_send_email` pattern).

**`_sender_allowed` helper** (pure function — no mocking needed):

| Test                                                 | Description                                                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `test_sender_allowed_empty_patterns`                 | Returns `True` when allowlist is empty                                                            |
| `test_sender_allowed_exact_match`                    | `alice@example.com` matches pattern `alice@example.com`                                           |
| `test_sender_allowed_glob_domain`                    | `bob@glez.de` matches pattern `*@glez.de`                                                         |
| `test_sender_allowed_name_addr_format`               | `"Alice <alice@example.com>"` address extracted and matched                                       |
| `test_sender_allowed_case_insensitive`               | `Alice@EXAMPLE.COM` matches pattern `alice@example.com`                                           |
| `test_sender_allowed_no_match`                       | Unlisted address returns `False`                                                                  |
| `test_sender_allowed_matches_second_pattern_in_list` | Sender matching the second of two patterns returns `True` (exercises `any()` traversal)           |
| `test_sender_allowed_unparseable_sender`             | Malformed `From` header that `parseaddr` cannot extract an address from is treated as not allowed |

**`list_allowed_senders` tool:**

| Test                                         | Description                          |
| -------------------------------------------- | ------------------------------------ |
| `test_list_allowed_senders_empty`            | Returns `[]` when not configured     |
| `test_list_allowed_senders_returns_patterns` | Returns configured patterns verbatim |

**`list_emails_metadata` filtering:**

| Test                                               | Description                                          |
| -------------------------------------------------- | ---------------------------------------------------- |
| `test_list_emails_metadata_no_allowlist`           | All emails returned when allowlist empty             |
| `test_list_emails_metadata_filters_blocked_sender` | Blocked sender removed from results                  |
| `test_list_emails_metadata_total_unchanged`        | `total` reflects server count, not post-filter count |

**`get_emails_content` filtering:**

| Test                                                | Description                                  |
| --------------------------------------------------- | -------------------------------------------- |
| `test_get_emails_content_no_allowlist`              | All emails returned when allowlist empty     |
| `test_get_emails_content_filters_blocked_sender`    | Blocked sender silently dropped              |
| `test_get_emails_content_retrieved_count_adjusted`  | `retrieved_count` reflects post-filter count |
| `test_get_emails_content_blocked_not_in_failed_ids` | Blocked email not added to `failed_ids`      |

---

## Transparency

This spec was designed collaboratively between the author and Claude Code (Sonnet 4.6) using the Superpowers plugin. Implementation will follow TDD: tests written first, then implementation.
