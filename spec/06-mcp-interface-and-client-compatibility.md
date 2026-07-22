# 06. MCP Interface and Client Compatibility

Status: Implemented

Previous: [`05-sqlite-persistence-and-data-model.md`](05-sqlite-persistence-and-data-model.md)
Next: [`README.md`](README.md)

## Scope

stdio is the target MCP transport. Static tools and complete text/JSON-compatible
results are the cross-client baseline. Resources, prompts, structured output,
annotations, and list-change notifications may enhance capable clients but may
not be required for core workflows.

Existing SSE and Streamable HTTP commands remain documented compatibility
surfaces until a separate versioned retirement decision. They do not expand the
managed architecture into a remote service.

## Stable Session Catalog

The MCP server constructs one tool catalog for the life of a stdio process.
Account additions, disablement, capability changes, and mode changes do not
replace tools mid-session. Tools validate account existence, enabled state,
capability, and policy at call time. A mode change requires restart.

The implemented catalog is fully static: capability and allowlist discovery
tools remain advertised and return data or bounded call-time errors. Catalog
contents do not depend on transient index freshness, account rows, policy, or
secret-store availability.

## Adapter Contract

Every MCP handler:

1. validates schema and explicit bounds;
2. creates an application command/query DTO;
3. calls one application service;
4. maps its typed result to the existing public response;
5. sanitizes bounded expected errors.

Handlers do not read SQLite directly, resolve keyring entries, instantiate mail
clients, implement allowlists, or choose index/provider fallback. A migrated
workflow has no direct `ClassicEmailHandler` bypass.

## Metadata Tool Compatibility

`list_emails_metadata` keeps its existing public name and arguments:

- `account_name`;
- `page` and `page_size`;
- `before`, `since`, `subject`, `from_address`, and `to_address`;
- `order`;
- `mailbox`;
- `seen`, `flagged`, and `answered`;
- `body`, `text`, and `has_attachment`.

The response remains `EmailMetadataPageResponse` with `page`, `page_size`,
`before`, `since`, `subject`, `emails`, and required exact filtered `total`.
Each email retains `email_id`, optional `message_id`, `subject`, `sender`,
`recipients`, `date`, and `attachments`.

The application query may answer from SQLite only when it can satisfy that whole
contract. Otherwise it uses a bounded IMAP fallback, including for unsupported
body/text/attachment filtering or incomplete coverage. The response does not
expose internal cursors, coverage tables, SQL IDs, source fingerprints, or
fallback implementation details.

A cursor-native or intentionally partial public query requires a separately
versioned proposal and does not silently change this tool.

## Existing Workflow Compatibility

The managed migration retains current tool names, required inputs, return
content types, all-known-success output, account-specific errors, allowlist
behavior, and conditional capability checks for:

- account and mailbox discovery;
- metadata and content retrieval;
- recipient/sender allowlist discovery;
- send and sent-copy behavior;
- save to mailbox;
- delete, mark read, move, and archive;
- attachment download.

Secret-bearing account creation is a CLI management responsibility in managed
mode. The existing `add_email_account` tool keeps its legacy-mode contract; in
managed mode it must reject before any legacy write with actionable CLI guidance
unless a separately reviewed managed-safe MCP design replaces it. MCP never
accepts managed passwords or tokens as ordinary tool arguments.

Existing mutation tools keep their string return schema and current literal for
an all-known-success result. A partial or ambiguous result uses bounded tagged
text in that same schema to identify per-target `succeeded`, `failed`, or
`unknown` status; SMTP delivery and sent-copy status use separate tagged
sections. This is the compatibility mapping for the MVP, not flattening. Adding
a new structured mutation result or changing successful literals requires a
versioned proposal.

## Progressive Disclosure and Bounds

Clients should discover accounts/mailboxes, list bounded metadata, then request
selected bodies or attachments. This is a usage pattern, not a requirement for
client-specific features.

- Metadata `page_size` is between 1 and 100. A provider fallback scans at most
  10,000 matching UIDs and performs at most the configured bounded header/date
  fetch work; if exact ordering and the requested page cannot be proven within
  that ceiling, the call fails with `query_too_broad` rather than returning an
  inexact `total` or partial page. IMAP ESEARCH/SORT capabilities may satisfy the
  same contract with less work but are not required.
- The adapter applies a bounded command timeout and response-byte ceiling where
  its protocol library exposes one. Basic IMAP SEARCH can produce its UID list
  before cardinality is known; the 10,000-candidate gate bounds subsequent local
  work and is the documented residual limitation, not a completeness claim.
- Identifier and recipient batches, body and attachment sizes, mailbox counts,
  and error details use constants owned by one `ApplicationLimits` value. Their
  shipped values and any supported configuration ranges are documented with the
  implementing slice; adapters cannot widen them.
- Metadata listing never embeds body content.
- Body batch responses preserve requested/retrieved counts and failed IDs.
- Attachment output is confined by application policy.
- Large or malformed provider content is truncated or rejected with explicit
  status; it is not written raw to logs or protocol errors.

## Results and Errors

Provider-controlled email content is untrusted data. Tool descriptions and
results must not present message text as instructions. Structured and text
representations, when both emitted, describe the same outcome.

Expected failures use stable, bounded categories such as invalid input, unknown
account, disabled account, denied by policy, capability unavailable, provider
failure, unknown provider outcome, managed store unavailable, restart required,
and reconciliation needed. Errors do not include secrets, secret locators,
message bodies, SQL, stack traces, or unintended local paths.

Partial mutation results preserve per-target input order and distinguish known
success, known failure, and unknown outcome through the compatible tagged-text
mapping above. SMTP delivery and sent-copy status remain separate. Adapters do
not flatten these distinctions into a misleading all-success or all-failure
claim.

## Stdio Correctness

stdout is reserved for MCP protocol frames. Logs and diagnostics go to stderr
and contain no secrets or message content. Startup either completes a valid
selected-mode runtime or exits non-zero before serving requests. EOF,
cancellation, and normal termination close runtime resources.

## Implementation Evidence

Every MCP account, policy, metadata, mailbox, body, attachment, compose, and
mutation path maps to a typed application query or command while retaining its
public name, arguments, and compatible response mapping. The former dispatcher
and dynamic visibility subclass were removed. The static catalog always
advertises capability/policy tools; calls revalidate current account state and
policy. The adapter enforces one-based metadata pages and a page size from 1 to 100. Application and provider layers enforce the same bounds for direct and
embedded callers, and provider candidate work is capped at 10,000 UIDs.

Index selection and fallback are no longer MCP-handler decisions. The application
service conservatively uses SQLite only for an unfiltered complete-mailbox
request whose UIDVALIDITY, UIDNEXT, and message count still match provider state.
Every current filter remains compatible through the bounded provider path. The
stdio GreenMail suite verifies schema serialization, pagination, totals, filter
fallback, projection reuse in one process and after restart, and bounded input
failure.

All explicit compose and mutation tools now map typed application outcomes back
to their existing string content type. All-known-success literals are unchanged.
Partial batches expose input-ordered `succeeded`, `failed`, and `unknown`
sections; SMTP recipients and Sent-copy status remain separate. Commands enforce
one bounded canonical UID batch and bounded mailbox, recipient, content, and
attachment inputs before provider access. Managed GreenMail coverage proves
mutation wiring, projection invalidation, lifecycle disable/re-enable/removal,
credential replacement, real IMAP state changes, explicit stored-only legacy
import, and recipient-policy rejection before SMTP. Body reads accept at most
500 canonical UIDs and split explicit read marking into safe mutation batches.
Attachment download separates provider bytes from exact-path filesystem effects
and rechecks current policy. Provider adapter tests preserve mixed
accepted/rejected SMTP recipients because the GreenMail test configuration
accepts arbitrary local recipients.

## Validation

Contract tests freeze tool names, schemas, descriptions, visibility behavior,
response models, exact metadata totals, all metadata filters, error mapping, and
account-specific capability failures. Subprocess tests verify stdout protocol
purity, restart behavior, startup failure, and cleanup.

GreenMail E2E starts the packaged stdio command, performs MCP initialization and
real `tools/list`/`tools/call` exchanges, and covers:

- legacy and managed account mailbox discovery;
- metadata pagination, filters, totals, and provider fallback;
- body PEEK and explicit mark-read behavior;
- save, move, archive, delete, and scoped expunge safety;
- SMTP recipient outcomes and independent sent-copy status;
- disablement, secret rotation, restart, and import paths;
- bounded failures for unsupported capabilities and invalid accounts.

Validation uses at least one generic MCP client implementation or raw protocol
harness in addition to direct Python function tests, so adapter wiring and
serialization are exercised end to end.
