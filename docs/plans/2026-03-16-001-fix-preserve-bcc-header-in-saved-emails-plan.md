---
title: "fix: Preserve BCC header when saving emails to IMAP folders"
type: fix
status: completed
date: 2026-03-16
---

# fix: Preserve BCC header when saving emails to IMAP folders

`compose_message()` intentionally omits the `Bcc` header because SMTP sending must never expose BCC recipients in message headers. However, two save-to-IMAP paths also use `compose_message`, so BCC recipients are silently lost:

1. **`save_to_mailbox`** — drafts saved via IMAP APPEND have no BCC header, so opening the draft in Thunderbird shows an empty BCC field.
2. **`send_email` → `append_to_sent`** — the sent copy lacks BCC, so users can't see who was BCC'd.

Thunderbird (and other clients) preserve BCC in both Drafts and Sent copies. We should do the same.

## Acceptance Criteria

- [x] `compose_message()` accepts an `include_bcc_header: bool = False` parameter
- [x] When `include_bcc_header=True` and `bcc` is a non-empty list, a `Bcc` header is added (comma-joined, matching the existing `Cc` pattern at line 819-820)
- [x] When `bcc` is `None` or `[]`, no `Bcc` header is added regardless of `include_bcc_header`
- [x] `save_to_mailbox` passes `include_bcc_header=True` to `compose_message`
- [x] When `save_to_sent` is enabled, `ClassicEmailHandler.send_email` injects `Bcc` header into `msg` after SMTP send but before `append_to_sent` (post-send mutation is safe since `aiosmtplib.send_message` serializes immediately; this ordering is load-bearing for security)
- [x] `send_email` (SMTP path) continues to NOT include BCC in headers — existing test `test_bcc_not_in_headers` still passes
- [x] Docstring on `compose_message` updated to explain the two use cases (SMTP vs local storage)

## Context

- PR #137 (`feat/save-to-mailbox`) — this fix extends the existing branch
- `compose_message` already accepts `bcc` param but ignores it (`classic.py:794-796`)
- CC uses bare `", ".join(cc)` at line 819-820 — BCC should follow the same pattern for consistency
- Security doc: `docs/solutions/security-issues/imap-flag-injection-prevention.md` — flag validation is already handled

## Implementation (TDD)

Work in three RED → GREEN → REFACTOR cycles, one per layer.

### Phase 1: RED — Write failing tests for `compose_message` BCC header

Add to `tests/test_save_to_mailbox.py`:

```python
class TestComposeMessageBccHeader:
    """Tests for BCC header inclusion in compose_message."""

    def test_bcc_header_included_when_flag_true(self, client):
        msg = client.compose_message(
            ["to@example.com"], "Sub", "Body",
            bcc=["secret@example.com"], include_bcc_header=True,
        )
        assert msg["Bcc"] == "secret@example.com"

    def test_bcc_header_multiple_recipients(self, client):
        msg = client.compose_message(
            ["to@example.com"], "Sub", "Body",
            bcc=["a@example.com", "b@example.com"], include_bcc_header=True,
        )
        assert msg["Bcc"] == "a@example.com, b@example.com"

    # Note: test_bcc_not_in_headers (existing) already covers include_bcc_header=False

    def test_bcc_header_omitted_when_empty_list(self, client):
        msg = client.compose_message(
            ["to@example.com"], "Sub", "Body",
            bcc=[], include_bcc_header=True,
        )
        assert msg["Bcc"] is None

    def test_bcc_header_omitted_when_none(self, client):
        msg = client.compose_message(
            ["to@example.com"], "Sub", "Body",
            bcc=None, include_bcc_header=True,
        )
        assert msg["Bcc"] is None
```

Run tests — confirm they fail (`TypeError: compose_message() got an unexpected keyword argument 'include_bcc_header'`).

### Phase 1: GREEN — Add `include_bcc_header` to `compose_message`

In `mcp_email_server/emails/classic.py` at line 777, add the parameter and BCC logic:

```python
def compose_message(
    self,
    recipients: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    include_bcc_header: bool = False,
) -> MIMEText | MIMEMultipart:
    """Compose an email message without sending it.

    Builds MIME structure, sets headers (Subject, From, To, Cc, Date,
    Message-Id, threading headers). Synchronous — no I/O.

    When ``include_bcc_header`` is True (used for local IMAP storage such
    as Drafts or Sent copies), the Bcc header is included so mail clients
    can display the BCC recipients.  When False (default, used for SMTP
    sending), the Bcc header is omitted — BCC recipients are delivered
    via the SMTP envelope only.
    """
    # ... existing code unchanged ...

    # Add CC header if provided (visible to recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)

    # Add BCC header when saving locally (drafts, sent copies)
    if bcc and include_bcc_header:
        msg["Bcc"] = ", ".join(bcc)

    # ... rest unchanged ...
```

Run tests — all 4 new tests pass. Existing `test_bcc_not_in_headers` still passes (it covers the `include_bcc_header=False` default).

### Phase 2: RED — Write failing tests for `save_to_mailbox` BCC preservation

Add to `tests/test_save_to_mailbox.py`:

```python
class TestSaveToMailboxBcc:
    """Tests that save_to_mailbox preserves BCC in the saved message."""

    @pytest.mark.asyncio
    async def test_save_to_mailbox_includes_bcc_header(self, handler):
        # ... mock setup (follow existing TestClassicEmailHandlerSaveToMailbox pattern) ...
        await handler.save_to_mailbox(
            ["to@example.com"], "Sub", "Body",
            bcc=["secret@example.com"],
        )
        # Don't mock compose_message — let it run for real so we verify
        # include_bcc_header=True is actually passed and produces a Bcc header
        appended_msg = handler.outgoing_client.append_to_mailbox.call_args[0][0]
        assert appended_msg["Bcc"] == "secret@example.com"
```

Run test — fails because `save_to_mailbox` doesn't pass `include_bcc_header=True`.

### Phase 2: GREEN — Wire `include_bcc_header=True` in `save_to_mailbox`

In `mcp_email_server/emails/classic.py` at line 1226, pass the flag:

```python
msg = self.outgoing_client.compose_message(
    recipients, subject, body, cc, bcc, html, attachments,
    in_reply_to, references, include_bcc_header=True,
)
```

Run test — passes.

### Phase 3: RED — Write failing test for `send_email` Sent-copy BCC

Add to `tests/test_save_to_sent.py`:

```python
class TestSendEmailSentCopyBcc:
    """Tests that send_email includes BCC in the Sent folder copy."""

    @pytest.mark.asyncio
    async def test_sent_copy_includes_bcc_header(self, handler):
        # ... mock setup with save_to_sent=True (follow existing TestSaveToSent pattern) ...
        await handler.send_email(
            ["to@example.com"], "Sub", "Body",
            bcc=["secret@example.com"],
        )
        # Verify BCC header was added before append_to_sent
        appended_msg = handler.outgoing_client.append_to_sent.call_args[0][0]
        assert appended_msg["Bcc"] == "secret@example.com"
```

Run test — fails because `send_email` doesn't inject BCC before `append_to_sent`.

### Phase 3: GREEN — Inject BCC header in `send_email` before `append_to_sent`

In `mcp_email_server/emails/classic.py` at line 1201:

```python
# Save to Sent folder if enabled
if self.save_to_sent and msg:
    # Add BCC header to the saved copy so users can see who was BCC'd.
    # This MUST happen after smtp.send_message() — that ordering is
    # load-bearing for security (BCC must not appear in sent headers).
    if bcc and msg["Bcc"] is None:
        msg["Bcc"] = ", ".join(bcc)
    try:
        await self.outgoing_client.append_to_sent(
            msg, self.email_settings.incoming, self.sent_folder_name,
        )
    except Exception as e:
        logger.error(f"Failed to save email to Sent folder: {e}", exc_info=True)
```

Run full test suite — all tests pass, no regressions.

### Final: Run full suite

```bash
pytest --tb=short
```

Verify all existing tests (137+) plus new tests pass.

## Sources

- Existing `compose_message`: `mcp_email_server/emails/classic.py:777-833`
- `save_to_mailbox` handler: `mcp_email_server/emails/classic.py:1212-1238`
- `send_email` handler (save-to-Sent): `mcp_email_server/emails/classic.py:1185-1210`
- Existing BCC-not-in-headers test: `tests/test_save_to_mailbox.py:155-162`
- PR #137: https://github.com/ai-zerolab/mcp-email-server/pull/137
