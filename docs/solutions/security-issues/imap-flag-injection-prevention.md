---
title: "IMAP Flag Injection Prevention in save_to_mailbox"
category: security-issues
date: 2026-03-13
tags:
  - imap
  - injection
  - flag-validation
  - rfc-3501
  - rfc-4315
  - mcp-tool
  - save-to-mailbox
related_issues:
  - "#130"
modules:
  - mcp_email_server/emails/classic.py
  - mcp_email_server/app.py
severity: high
---

# IMAP Flag Injection Prevention in save_to_mailbox

## Problem

While implementing IMAP APPEND support for saving emails to arbitrary mailboxes (issue #130), the `flags` parameter — a `list[str]` arriving from MCP tool callers — was concatenated directly into the IMAP APPEND command with no validation:

```python
flags_str = "(" + " ".join(flags) + ")"
```

A malformed flag like `\Seen) 25-Dec-2025 {99999}` could inject additional IMAP APPEND parameters depending on how the underlying `aioimaplib.append` constructs the wire command. Injecting `\Deleted` could cause the message to be immediately expunged on servers with auto-expunge.

## Root Cause

MCP tool parameters arrive from LLM-generated or user-provided JSON and must be treated as untrusted input. The flags string is passed directly to the IMAP wire protocol — the same class of vulnerability as SQL injection or shell injection, applied to the IMAP protocol layer.

## Solution

Added `_validate_flags()` with regex validation against RFC 3501 flag syntax before passing to the IMAP APPEND command:

```python
_VALID_IMAP_FLAG = re.compile(r"^\\[A-Za-z]+$|^[A-Za-z][A-Za-z0-9_-]*$")


def _validate_flags(flags: list[str]) -> str:
    for flag in flags:
        if not _VALID_IMAP_FLAG.match(flag):
            msg = f"Invalid IMAP flag: {flag!r}"
            raise ValueError(msg)
    return "(" + " ".join(flags) + ")"
```

The regex accepts:
- **System flags:** `\` followed by an alpha sequence (`\Seen`, `\Draft`, `\Flagged`, etc.)
- **Custom keywords:** alphanumeric atoms with `-` and `_` (`MyLabel`, `custom-tag`)

Anything containing parentheses, spaces, braces, `%`, `*`, quotes, or non-ASCII is rejected.

Validation is called in `ClassicEmailHandler.save_to_mailbox` before reaching the IMAP layer:

```python
if flags is None:
    flags_str = r"(\Draft \Seen)"
else:
    flags_str = _validate_flags(flags)
```

## Additional Design Decisions

### compose_message extraction

Message composition (MIME building, header setting) was extracted from `send_email` into a public `compose_message()` method on `EmailClient`. This is synchronous with no I/O, making it reusable by both `send_email` and `save_to_mailbox`.

### append_to_mailbox returns IMAP UID

`append_to_mailbox` returns `str | None` instead of `bool` — extracting the IMAP UID from the APPENDUID response (RFC 4315) when available. This enables workflow chaining: the MCP tool response includes both `Message-Id` and `email_id` (UID), so agents can reference the saved message in subsequent `delete_emails` calls.

### append_to_sent not refactored

`append_to_sent` was intentionally not refactored to delegate to `append_to_mailbox`. It tries multiple folder candidates (Sent, INBOX.Sent, [Gmail]/Sent Mail) within a single IMAP session. Calling `append_to_mailbox` per candidate would open a new connection each time, which is worse for the discovery use case. The composition-side duplication was eliminated by the shared `compose_message` method.

## Prevention Strategies

- **Validate at the protocol boundary.** Put flag validation on the class that owns the IMAP connection, not in the MCP handler. Protocol-level validation belongs at the protocol layer.
- **Define an allowlist, not a denylist.** IMAP system flags are a closed set. Custom keywords follow strict atom syntax. Enumerate what's allowed rather than trying to block what's dangerous.
- **Write rejection tests before success tests.** For any parameter that touches a wire protocol, write a test with a malicious input before writing the happy-path test.
- **Treat all MCP tool inputs as untrusted.** The same discipline as SQL parameterization or shell escaping applies to IMAP commands.

## Related Documentation

- `docs/solutions/logic-errors/imap-search-criteria-quoting-missing.md` — RFC 3501 compliance for IMAP search criteria quoting (#128)
- `docs/solutions/security-issues/credential-leak-in-dispatcher-and-repr.md` — credential leak prevention with SecretStr (#133)
- PR: https://github.com/ai-zerolab/mcp-email-server/pull/137
