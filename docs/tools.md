# MCP Tools

> **Version scope:** The mail-only catalog on this page is Local Email App V2
> behavior. See [Version availability](getting-started.md#version-availability)
> before using this contract with a PyPI installation.

mcp-email-server exposes bounded account discovery, message, mailbox, and
composition operations as MCP tools. Tool schemas are generated from the running server, so the MCP client
can inspect each parameter and response type directly.

## Typical workflow

Most message workflows follow this sequence:

1. Call `list_available_accounts` to select an `account_name`.
2. Call `list_emails_metadata` to search a mailbox and obtain `email_id` values.
3. Pass those IDs to a read or mutation tool with the same mailbox name.
4. Call `get_emails_content` only for messages whose bodies are needed.

This separates lightweight metadata searches from potentially large body
retrievals.

MCP input schemas advertise the enforceable string and collection envelopes from
the centralized application limits, including `maxLength`, `minItems`, and
`maxItems` for account/mailbox names, UID collections, recipients, attachments,
and flags. JSON Schema counts characters while the application limits UTF-8
bytes, so every application service independently revalidates direct and MCP
callers; aggregate recipient and payload limits also remain application-owned.

## Account resource

The resource URI `email://{account_name}` returns the same stable non-secret
capability record used by account discovery. It does not return configuration or
masked credential objects.

## Account tools

### `list_available_accounts`

Lists all enabled accounts from the selected configuration mode as explicit
capability records. Each record contains `account_name`, `account_type`,
`description`, optional `email_address`, `can_receive`, and `can_send`. Account
descriptions are limited to 4 KiB of UTF-8 data and expose the same structural
bound in the output schema. In managed mode, disabled accounts are omitted before any credential lookup or provider
access. Use only an account with `can_receive=true` for mail reads and
`can_send=true` for `send_email`. Text content, structured content, and the output
schema describe the same fields.

If the result is empty, account setup is unavailable over MCP. The agent should
ask the user to run `mcp-email-server ui` or the documented interactive CLI in
their own terminal and must never request or relay credentials. The output schema
and application boundary allow at most 1,000 accounts; the canonical JSON must
also fit the shared 8 MiB response ceiling. Oversized authority data is rejected
with `limit_exceeded` rather than truncated.

MCP exposes no account, endpoint, policy, catalog, or credential mutation tool in
either mode. Use `mcp-email-server ui` or the user-operated `config` and
`account` CLI commands. This prevents an agent or chat transcript from becoming
a credential handoff surface. The complete tool names, descriptions, input and
output schemas, annotations, resource template, and visibility are static and
covered by an exact catalog contract test.

`add_email_account`, which exists in PyPI 0.16.0 and earlier, is intentionally
absent from Local Email App V2 rather than renamed. See
[Upgrading to Local Email App V2](getting-started.md#upgrading-to-local-email-app-v2)
for client discovery and configuration migration steps.

## Agent planning annotations

Every tool advertises reviewed MCP `readOnlyHint`, `destructiveHint`,
`idempotentHint`, and `openWorldHint` values:

| Tools                                                                        | Read-only | Destructive | Idempotent | Open world |
| ---------------------------------------------------------------------------- | --------- | ----------- | ---------- | ---------- |
| `list_available_accounts`, `list_allowed_recipients`, `list_allowed_senders` | yes       | no          | yes        | no         |
| `list_emails_metadata`, `list_mailboxes`                                     | yes       | no          | yes        | yes        |
| `get_emails_content`                                                         | no        | no          | yes        | yes        |
| `send_email`, `save_to_mailbox`                                              | no        | no          | no         | yes        |
| `set_email_flags`, `mark_emails_as_read`                                     | no        | no          | yes        | yes        |
| `delete_emails`, `move_emails`, `archive_emails`, `download_attachment`      | no        | yes         | no         | yes        |

`get_emails_content` is conservatively non-read-only because
`mark_as_read=true` changes remote flags. Download is destructive because the
caller-selected destination may be replaced. Send and append create externally
meaningful effects but do not delete or replace an existing mailbox item, so
their destructive hint is false while their read-only and idempotent hints are
also false.

Annotations are advisory host/agent planning hints, not authorization or a
safe-retry guarantee. Tool descriptions, current policy, typed outcomes, and the
rule against replay after an ambiguous effect remain authoritative.

## Reading and searching

### `list_emails_metadata`

Searches one mailbox without downloading message bodies.

Important parameters include:

| Parameter                     | Default  | Description                                 |
| ----------------------------- | -------- | ------------------------------------------- |
| `account_name`                | Required | Configured account identifier.              |
| `page`                        | `1`      | One-based result page.                      |
| `page_size`                   | `10`     | Number of results per page, from 1 to 100.  |
| `mailbox`                     | `INBOX`  | Mailbox to search.                          |
| `before` / `since`            | None     | UTC datetime boundaries.                    |
| `subject`                     | None     | Subject filter.                             |
| `from_address` / `to_address` | None     | Address filters.                            |
| `seen`                        | None     | Filter by read status.                      |
| `flagged`                     | None     | Filter by flagged or starred status.        |
| `answered`                    | None     | Filter by replied status.                   |
| `body`                        | None     | Search message bodies with IMAP `BODY`.     |
| `text`                        | None     | Search headers and bodies with IMAP `TEXT`. |
| `has_attachment`              | None     | Apply a multipart attachment heuristic.     |
| `order`                       | `desc`   | Return ascending or descending results.     |

The response contains pagination metadata, a filtered `total`, and message
metadata including `email_id`, `message_id`, subject, sender, recipients, and
date. Because this operation fetches headers only, its `attachments` field is
empty. `get_emails_content` populates attachment names from the full message.

`has_attachment` uses a `multipart/mixed` heuristic. It can miss inline content
or report multipart messages that do not contain a conventional attachment.

When a sender allowlist is configured, blocked messages are removed before
pagination, so `total` and page sizes describe only visible messages.

The application keeps a rebuildable SQLite projection for unfiltered mailbox
pages. It uses that projection only after a small IMAP `STATUS` probe confirms
the same UIDVALIDITY, UIDNEXT, and message count and the projection covers the
whole mailbox. Text, date, address, flag, body, and attachment filters remain on
the bounded IMAP path so provider-specific search semantics and mutable flags
stay authoritative. A response normally omits `warnings`; if a validated IMAP
result was returned but its rebuildable projection could not be persisted, the
response includes `warnings: ["projection_write_failed"]`. It never includes the
local exception detail.

A refresh stores at most the 1,000 most recent UIDs and claims complete coverage
only when the whole mailbox fits that window and provider state is unchanged
across the refresh. Provider fallback accepts at most 10,000 unique canonical
single UIDs; ranges, sets, zero, duplicates, and values outside the IMAP UID
range are rejected before any UID FETCH. Metadata header requests use IMAP
partial fetches, with limits of 64 KiB per
message and 4 MiB total per metadata query or refresh. Each wire FETCH is also
sized below that aggregate ceiling. Missing, duplicate, or mismatched sender or
INTERNALDATE evidence is a bounded error because the server cannot otherwise
prove the exact total or ordering. Transport and protocol failures are mapped to
bounded categories without returning provider-controlled detail. If a work or
payload ceiling is exceeded,
the tool returns a bounded error instead of an inexact `total`, partial page, or
unbounded projection.

### `get_emails_content`

Fetches the body of one or more messages by `email_id`.

| Parameter         | Default  | Description                                                     |
| ----------------- | -------- | --------------------------------------------------------------- |
| `account_name`    | Required | Configured account identifier.                                  |
| `email_ids`       | Required | IDs returned by `list_emails_metadata`.                         |
| `mailbox`         | `INBOX`  | Mailbox containing the messages.                                |
| `mark_as_read`    | `false`  | Mark successfully retrieved messages as read.                   |
| `body_offset`     | `0`      | Character offset at which body output starts.                   |
| `max_body_length` | `20000`  | Maximum body characters returned per message, from 1 to 100000. |

If a body extends beyond the requested window, the returned body ends with
`...[TRUNCATED]`. Fetch the next chunk by increasing `body_offset` by
`max_body_length`.

The batch response reports requested and retrieved counts and includes
`failed_ids` for messages that could not be fetched. A full-message literal from
a successful IMAP FETCH is parsed regardless of its byte length; protocol
metadata without a message literal is not treated as content. Each returned
email also includes nullable `in_reply_to` and `references` values from the
corresponding RFC headers. `references` is returned as one decoded, unfolded string with
folding spaces and tabs normalized. Missing and whitespace-only values become
`null`; if an invalid message repeats either header, the parser's first observed
value is returned. This is untrusted observational header data, not a validated
list of Message-IDs. Well-formed values can be passed back to the compose tools,
but malformed values containing other control characters can be returned and
will be rejected by compose validation. These fields are available only from
full-content reads: they are not part of `list_emails_metadata` and are not
persisted in the SQLite metadata projection.

A request accepts 1 to 500 canonical positive decimal ASCII IMAP UIDs; zero,
leading zero, non-ASCII digits, signs, ranges, sets, and values above the IMAP UID
limit are rejected before provider access. The provider adapter repeats this
validation before opening an IMAP connection as defense in depth. Raw messages
above 50 MiB are rejected before MIME parsing. The production provider also
counts each returned body's UTF-8 bytes before retaining it and stops immediately
if the batch would exceed the 50 MiB aggregate body budget; the application
validates the aggregate again at its provider boundary. Each returned thread
header is limited to 64 KiB of UTF-8 data and both count toward the 4 MiB
aggregate returned-header budget. The production provider enforces these header
budgets before retaining each parsed result, and the application independently
revalidates them at its provider boundary. Oversized values fail explicitly
rather than being truncated into an invalid thread chain.

When the complete valid batch exceeds the inline MCP response ceiling, the
server writes the canonical JSON response to a randomly named owner-only file in
a process-private temporary directory. The bounded response then has
`content_omitted=true`, an empty `emails` preview, and
`output_file_path`, `output_media_type`, `output_bytes`, `output_sha256`, and
`output_lifetime` fields. A local MCP host can inspect that exact path with its
own filesystem tool. The file is available only until the email-server process
exits; copy needed content before restarting. The server does not add a generic
file-download tool or remote URL. Spill requires the complete POSIX owner/no-follow
profile or the local fixed NTFS Windows DACL/reparse/identity profile. Windows
crash remnants are removed only after bounded prefix, type, owner, DACL, and
identity validation. Spill never falls back to a broadly accessible temporary
file. Without the required profile, bounded inline results remain available,
while a batch that requires spill returns a bounded error. The process-lifetime
notice still applies.

Body retrieval always uses IMAP PEEK. When requested, successfully retrieved IDs
are deduplicated and marked through the same application mutation workflow as
`mark_emails_as_read` in batches of at most 100. A known mark failure is logged
but does not discard successfully retrieved content. An unknown or
reconciliation-needed mark outcome stops later mark batches so the application
does not continue after ambiguous state.

## Composing messages

### `send_email`

Sends a message through the selected account's SMTP server. It supports:

- To, CC, and BCC recipients.
- Plain-text or HTML bodies.
- Attachments from file paths available to the server process. Relative paths use the process working directory; absolute paths are recommended.
- `Reply-To`, `In-Reply-To`, and `References` headers.

The tool is always present in the stable MCP catalog. The selected
`account_name` must itself be enabled and send-capable; an IMAP-only account is
rejected before SMTP access.

If a recipient allowlist is configured, every To, CC, and BCC address must be
allowed. SMTP delivery reports accepted, rejected, and unknown recipients
separately when the result is partial or ambiguous. Failed and unknown targets
include reviewed fixed diagnostics when available, for example
`smtp-mail-rejected`, `smtp-recipient-rejected`, `smtp-data-rejected`,
`smtp-data-unknown`, or `provider-timeout`. Unrecognized detail and raw provider
response text are omitted.

Saving the Sent copy is a second IMAP effect and is reported in its own
`sent-copy` section; a failed or unknown copy never changes an accepted delivery
into a failure. Do not retry the whole send to repair a Sent copy. Sent-copy
APPEND payloads use CRLF line endings for compatibility with strict IMAP
providers.

### `save_to_mailbox`

Composes a message and appends it to an IMAP mailbox instead of sending it. It
works without SMTP and is useful for drafts or templates. It shares recipient,
body, attachment, and threading fields with `send_email`, adds `mailbox` and
`flags`, and does not support `reply_to`.

The default mailbox is `Drafts`. When no explicit flags are supplied, the
message is saved with `\Draft` and `\Seen`. The response includes the RFC
message ID. It includes an assigned IMAP `email_id` only when the server returns
RFC 4315 `APPENDUID`; otherwise the value is `unknown`, and the target mailbox
must be searched before a later operation can address the saved message.

The same recipient allowlist used by `send_email` applies to this tool. The
complete MIME payload is serialized with CRLF line endings before IMAP APPEND for
compatibility with strict providers. A known APPEND success without `APPENDUID`
returns `email_id: unknown`. A lost APPEND result is instead tagged `unknown`;
the server does not replay it because that could create a duplicate draft.

## Mailbox and mutation tools

### `list_mailboxes`

Lists IMAP mailboxes with their names, hierarchy delimiters, and flags. Call it
before moving or saving messages when provider-specific folder names are not
known.

`pattern` defaults to `*`, and `reference` defaults to an empty string. The
account name and both IMAP LIST values are validated before provider access;
`pattern` must be non-empty and pattern/reference values are each limited to
1,024 UTF-8 bytes.

### `set_email_flags`

Adds or removes approved IMAP flags from one or more message IDs in the selected
mailbox. `operation` must be `add` or `remove`, and applies to every supplied
flag. The non-empty `flags` list accepts unique values from:

- `\Seen`
- `\Flagged`
- `\Answered`
- `\Draft`

The provider sends one UID-scoped `+FLAGS.SILENT` or `-FLAGS.SILENT` operation
per message so results retain caller order and per-ID evidence. The operation is
logically idempotent, but an `unknown` result is not retried automatically
because the mailbox UID epoch or current authority may have changed.

`\Deleted` is intentionally rejected and remains owned by `delete_emails`,
which applies target-scoped expunge safety. `\Recent` is server-controlled, and
provider-specific keywords are not part of the portable public contract. To
mark a message unread, remove `\Seen`.

### `mark_emails_as_read`

Marks one or more message IDs as read in the selected mailbox. This focused
common-workflow tool uses the same implementation as `set_email_flags` with
`operation="add"` and `flags=["\\Seen"]`.

### `move_emails`

Moves messages from `source_mailbox`, which defaults to `INBOX`, to a required
`destination_mailbox`. Native IMAP `MOVE` is preferred. The COPY-and-delete
fallback is available only when the server advertises `UIDPLUS`, allowing the
source to be removed with target-scoped `UID EXPUNGE`; otherwise the operation
fails before copying a message.

### `archive_emails`

Moves messages to the account's archive mailbox. The server first uses the RFC
6154 `\Archive` mailbox flag and then falls back to `Archive`, `Archives`, or
`[Gmail]/All Mail`. Archive uses the same native-MOVE or safe UIDPLUS fallback
rules as `move_emails`.

### `delete_emails`

Deletes one or more messages from the selected mailbox. The provider must
advertise `UIDPLUS`: the server flags and expunges only the requested UIDs with
`UID EXPUNGE` and never sends mailbox-wide `EXPUNGE`. Without `UIDPLUS`, the
operation fails before adding the `\Deleted` flag.

An all-known-success mutation keeps the existing success sentence. Partial or
ambiguous results use tagged `succeeded`, `failed`, and `unknown` sections in
input order. Unknown targets can include a fixed substep tag such as `store`,
`copy`, or `expunge-after-copy`. `unknown` means the provider effect may have
started but its final result was lost; the server does not replay it
automatically, and every result containing `unknown` includes a `reconciliation
needed` warning. The same warning also appears when a known provider effect may
be authoritative but the rebuildable local metadata projection could not be
invalidated.

Mutation requests accept 1 to 100 unique canonical positive decimal IMAP UIDs.
`set_email_flags` accepts one to four unique approved flags and exactly one
add/remove operation. Mailbox names are limited to 1,024 UTF-8 bytes. Compose
requests allow at most 100 total To/CC/BCC entries of at most 1,024 UTF-8 bytes
each; every entry must
contain exactly one address. Compose requests also allow a 64 KiB UTF-8 subject,
a 1 MiB UTF-8 body, and 20 attachments. Threading and Reply-To values
are limited to 64 KiB each. Each attachment path is limited to 4,096 bytes,
each existing attachment to 25 MiB, and their combined size to 50 MiB. Outbound
attachments preserve the inferred MIME main type and subtype (for example,
`image/png` remains `image/png`) instead of coercing every file to
`application/*`. Saved messages accept at most 100 flags of 128 bytes each before protocol syntax
validation. Mailbox names, recipient/header values, and subjects reject control
characters before provider access.

When a sender allowlist is active, blocked messages are never changed. See
[Sender allowlist](security.md#sender-allowlist) for the privacy behavior of
blocked IDs.

## Attachments

### `download_attachment`

Downloads one named attachment from a message to the server host. By default,
the server creates a safe randomized filename under the current user's
`Downloads/mcp-email-server` directory. On Windows, it uses a valid Downloads
Known Folder registry value and otherwise falls back to the profile's `~/Downloads`; on
other platforms it uses `~/Downloads`. The returned
`saved_path` reports the resolved absolute destination.

`save_path` is optional. When supplied, it remains an exact destination: use an
absolute path when possible. A relative explicit path is resolved against the
server process's working directory.

The tool is registered even when downloading is disabled, but calling it then
raises a permission error. Enable it explicitly with:

```toml
enable_attachment_download = true
```

The application checks current account and feature policy, resolves and
preflights the local destination before provider construction, credential
resolution, download, or MIME decoding. For a default destination, it sanitizes
the attachment name, removes path/device syntax, adds a cryptographically random
suffix, and creates the application subdirectory with private permissions. It
checks authority again after fetch immediately before the write, so revocation
during a slow fetch discards the payload. Raw messages above 50 MiB and decoded
attachments above 25 MiB are rejected. The mail adapter returns bytes only and
never receives the resolved path.

The artifact adapter writes only the explicit or preflight-resolved destination.
It never falls back to the process working directory when default resolution
fails. POSIX uses pinned no-follow directory descriptors and owner-only files.
Windows supports only a local fixed NTFS drive-letter path and uses held
non-reparse handles, protected
DACLs, hard-link/identity checks, `FlushFileBuffers`, and same-volume
write-through replacement. Symlinked or junction parents, linked/permissive or
non-regular targets, UNC/network/device/alternate-stream/non-NTFS paths, and
replacement races fail closed. Existing private regular files may be replaced;
there is no weaker fallback.

Review [Attachment access](security.md#attachment-access) before enabling this
operation.

## Stable tool catalog

MCP initialization reports the installed `mcp-email-server` application version
in `serverInfo.version`, not the MCP SDK dependency version. The tool list is
static for the lifetime of a server process. `send_email`,
`list_allowed_recipients`, `list_allowed_senders`, and `download_attachment` are
always advertised. Account existence, enabled state, SMTP capability, and
current policies are enforced when each tool is called. The allowlist tools have
distinct empty semantics: an empty recipient list disables sending, while an
empty sender list does not restrict reading. Each list is limited to 1,000
entries, and the complete effective-configuration snapshot
is canonically serialized against the shared 8 MiB ceiling before either policy
result is returned; oversized authority data fails with `limit_exceeded`.

Account lifecycle changes therefore do not require a tools-list notification.
A bootstrap mode selection still requires a server restart because it changes
the selected configuration authority.

## Reply threading

To preserve conversation threading:

1. Fetch the original message with `get_emails_content`.
2. Use its RFC `message_id` as `in_reply_to`.
3. Build `references` from the returned `references` value followed by the
   original `message_id`, omitting missing values.
4. Send the reply with a suitable `Re:` subject.

For a complete example, see [Reply with proper threading](guides.md#reply-with-proper-threading).
