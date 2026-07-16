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

Lists all configured accounts with masked credentials. Use the returned
`account_name` in other tools.

### `add_email_account`

Adds and persists an email account. The input follows the nested account schema
documented in [Configuration](configuration.md#toml-example).

Account names must be unique. This tool changes persistent configuration and
may also move the supplied credentials into the operating system keyring,
depending on `credential_storage`.

## Reading and searching

### `list_emails_metadata`

Searches one mailbox without downloading message bodies.

Important parameters include:

| Parameter                     | Default  | Description                                 |
| ----------------------------- | -------- | ------------------------------------------- |
| `account_name`                | Required | Configured account identifier.              |
| `page`                        | `1`      | One-based result page.                      |
| `page_size`                   | `10`     | Number of results per page.                 |
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
`failed_ids` for messages that could not be fetched. A failure to apply
`mark_as_read` is logged but does not discard successfully retrieved content.

## Composing messages

### `send_email`

Sends a message through the selected account's SMTP server. It supports:

- To, CC, and BCC recipients.
- Plain-text or HTML bodies.
- Attachments from file paths available to the server process. Relative paths use the process working directory; absolute paths are recommended.
- `Reply-To`, `In-Reply-To`, and `References` headers.

This tool is visible only when at least one configured account has SMTP
settings. The selected `account_name` must itself be send-capable.

If a recipient allowlist is configured, every To, CC, and BCC address must be
allowed.

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

The same recipient allowlist used by `send_email` applies to this tool.

## Mailbox and mutation tools

### `list_mailboxes`

Lists IMAP mailboxes with their names, hierarchy delimiters, and flags. Call it
before moving or saving messages when provider-specific folder names are not
known.

`pattern` defaults to `*`, and `reference` defaults to an empty string.

### `mark_emails_as_read`

Marks one or more message IDs as read in the selected mailbox.

### `move_emails`

Moves messages from `source_mailbox`, which defaults to `INBOX`, to a required
`destination_mailbox`.

### `archive_emails`

Moves messages to the account's archive mailbox. The server first uses the RFC
6154 `\Archive` mailbox flag and then falls back to `Archive`, `Archives`, or
`[Gmail]/All Mail`.

### `delete_emails`

Deletes one or more messages from the selected mailbox.

Mutation tools return successful and failed counts. When a sender allowlist is
active, blocked messages are never changed. See
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

Review [Attachment access](security.md#attachment-access) before enabling this
operation.

## Conditional tools

The tool list adapts to the active configuration:

| Tool                      | Visibility condition                                     |
| ------------------------- | -------------------------------------------------------- |
| `send_email`              | At least one configured email account has SMTP settings. |
| `list_allowed_recipients` | `allowed_recipients` is not empty.                       |
| `list_allowed_senders`    | `allowed_senders` is not empty.                          |

`download_attachment` is not hidden when disabled; it checks permission when
called.

## Reply threading

To preserve conversation threading:

1. Fetch the original message with `get_emails_content`.
2. Use its RFC `message_id` as `in_reply_to`.
3. Include that ID and any known ancestor IDs in `references`, separated by
   spaces.
4. Send the reply with a suitable `Re:` subject.

For a complete example, see [Reply with proper threading](guides.md#reply-with-proper-threading).
