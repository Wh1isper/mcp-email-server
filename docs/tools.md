# MCP Tools

mcp-email-server exposes account, message, mailbox, and composition operations as
MCP tools. Tool schemas are generated from the running server, so the MCP client
can inspect each parameter and response type directly.

## Typical workflow

Most message workflows follow this sequence:

1. Call `list_available_accounts` to select an `account_name`.
2. Call `list_emails_metadata` to search a mailbox and obtain `email_id` values.
3. Pass those IDs to a read or mutation tool with the same mailbox name.
4. Call `get_emails_content` only for messages whose bodies are needed.

This separates lightweight metadata searches from potentially large body
retrievals.

## Account resource

The resource URI `email://{account_name}` returns the selected account's
configuration with credentials masked.

## Account tools

### `list_available_accounts`

Lists all enabled accounts from the selected configuration mode with masked
credentials. In managed mode, disabled accounts are omitted before any
credential lookup or provider access. Use the returned `account_name` in other
tools.

### `add_email_account`

Adds and persists an email account. The input follows the nested account schema
documented in [Configuration](configuration.md#toml-example).

Account names must be unique. In legacy mode this tool changes persistent TOML
configuration and may also move the supplied credentials into the operating
system keyring, depending on `credential_storage`.

In managed mode this legacy writer is rejected before any TOML or keyring
mutation. Use `mcp-email-server account add` so secrets enter through a masked
prompt or explicit stdin and managed candidate bindings remain recoverable.

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
stay authoritative. This choice does not change the response schema or expose
whether a particular call used SQLite.

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
`failed_ids` for messages that could not be fetched. A request accepts 1 to 500
canonical positive decimal IMAP UIDs; zero, signs, ranges, sets, and values above
the IMAP UID limit are rejected before provider access. Raw messages above 50
MiB are rejected before MIME parsing.

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
separately when the result is partial or ambiguous. Saving the Sent copy is a
second IMAP effect and is reported in its own `sent-copy` section; a failed or
unknown copy never changes an accepted delivery into a failure. Do not retry the
whole send to repair a Sent copy.

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

The same recipient allowlist used by `send_email` applies to this tool. A known
APPEND success without `APPENDUID` returns `email_id: unknown`. A lost APPEND
result is instead tagged `unknown`; the server does not replay it because that
could create a duplicate draft.

## Mailbox and mutation tools

### `list_mailboxes`

Lists IMAP mailboxes with their names, hierarchy delimiters, and flags. Call it
before moving or saving messages when provider-specific folder names are not
known.

`pattern` defaults to `*`, and `reference` defaults to an empty string. The
account name and both IMAP LIST values are validated before provider access;
`pattern` must be non-empty and pattern/reference values are each limited to
1,024 UTF-8 bytes.

### `mark_emails_as_read`

Marks one or more message IDs as read in the selected mailbox.

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
automatically. A
`reconciliation needed` warning means the provider outcome is authoritative but
the rebuildable local metadata projection could not be invalidated.

Mutation requests accept 1 to 100 unique canonical positive decimal IMAP UIDs.
Mailbox names are limited to 1,024 UTF-8 bytes. Compose requests allow at most
100 total To/CC/BCC entries of at most 1,024 UTF-8 bytes each; every entry must
contain exactly one address. Compose requests also allow a 64 KiB UTF-8 subject,
a 1 MiB UTF-8 body, and 20 attachments. Threading and Reply-To values
are limited to 64 KiB each. Each attachment path is limited to 4,096 bytes,
each existing attachment to 25 MiB, and their combined size to 50 MiB. Saved
messages accept at most 100 flags of 128 bytes each before protocol syntax
validation. Mailbox names, recipient/header values, and subjects reject control
characters before provider access.

When a sender allowlist is active, blocked messages are never changed. See
[Sender allowlist](security.md#sender-allowlist) for the privacy behavior of
blocked IDs.

## Attachments

### `download_attachment`

Downloads one named attachment from a message and writes it to a path on the
server host. Use an absolute path when possible. A relative path is resolved
against the server process's working directory.

The tool is registered even when downloading is disabled, but calling it then
raises a permission error. Enable it explicitly with:

```toml
enable_attachment_download = true
```

The application checks the current account and feature policy before fetching,
checks them again when opening provider access, and resolves authority once more
after fetch immediately before the filesystem write. Revocation during a slow
fetch therefore discards the payload without creating a file. It rejects raw
messages above 50 MiB and decoded attachment content above 25 MiB. The mail adapter
returns bytes only; it cannot choose or write a filesystem path. The local
artifact adapter writes only the exact requested destination, creates files with
owner-only mode on POSIX, and rejects symlinked/non-directory parents plus
symlinked or non-regular final targets. Existing regular files at the exact path
may be replaced.

Review [Attachment access](security.md#attachment-access) before enabling this
operation.

## Stable tool catalog

The tool list is static for the lifetime of a server process. `send_email`,
`list_allowed_recipients`, `list_allowed_senders`, and `download_attachment` are
always advertised. Account existence, enabled state, SMTP capability, and
current policies are enforced when each tool is called. The two allowlist tools
return an empty list when their corresponding policy is unrestricted.

Account lifecycle changes therefore do not require a tools-list notification.
A bootstrap mode selection still requires a server restart because it changes
the selected configuration authority.

## Reply threading

To preserve conversation threading:

1. Fetch the original message with `get_emails_content`.
2. Use its RFC `message_id` as `in_reply_to`.
3. Include that ID and any known ancestor IDs in `references`, separated by
   spaces.
4. Send the reply with a suitable `Re:` subject.

For a complete example, see [Reply with proper threading](guides.md#reply-with-proper-threading).
