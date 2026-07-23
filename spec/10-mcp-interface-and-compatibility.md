# 10. MCP Interface and Compatibility

## Baseline

MCP stdio is the supported managed mail-workflow interface. SSE and Streamable
HTTP compatibility commands may remain available, but they do not weaken
managed authority or create a remotely supported management plane. MCP Apps are
not delivered in this milestone.

The public MCP contract includes tool/resource names, descriptions, annotations,
visibility, input schemas, output schemas where exposed, result literals and
shapes, error categories, and transport behavior. These are compatibility
surface, not incidental implementation details.

## Static Catalog

The tool and resource catalog is fixed for the process lifetime. A tool does not
disappear when mode, account, policy, or provider capability makes a call
unavailable. Instead, the application service checks current authority at call
time and returns a bounded typed denial.

This preserves client discovery caches while ensuring a permissive startup state
does not authorize a later effect. Tool list callbacks MUST NOT resolve secrets,
contact providers, or scan mailboxes.

A complete version-controlled contract snapshot/assertion covers every exported:

- tool and resource name;
- human description;
- input and output JSON schema;
- required/optional field and default;
- enum and numeric/string/array bound;
- annotations and visibility;
- success and tagged partial-result shape.

Tests fail on addition, removal, schema drift, or description drift unless the
change is intentionally reviewed as a public contract change.

## No-secret and No-management Boundary

No tool/resource schema in either mode contains a password, token, private key,
secret value, reusable secret locator, set/rotate credential command, account
writer, import command, or generic management RPC. No result exposes binding
locators.

The historical `add_email_account` tool is intentionally removed from the
catalog. Host approval, HITL UI, annotations, or optional elicitation cannot
prevent a secret-bearing tool argument from entering model/client/protocol logs
and are not a portable credential channel. Interactive CLI and authenticated
local Web UI are the supported replacements; optional agent integrations provide
only the safe handoff defined in spec 11.

## Tool Families

The stable mail catalog may expose these compatibility families:

- account/mailbox discovery;
- metadata listing and querying;
- body and attachment reads;
- flag/read-state mutations;
- save/append, move, archive, and delete;
- SMTP send and sent-copy behavior.

Exact names and schemas are owned by the checked contract snapshot. Their
workflow semantics and safety are owned by specs 06 and 07. Account, policy,
credential, import, doctor, agent-installation, and UI-session operations are not
MCP tools.

## Request and Result Bounds

Every schema advertises enforceable structural bounds where MCP/JSON Schema can
express them. Application services enforce them again for direct callers.
MCP-specific ceilings include:

- maximum serialized success/partial response bytes;
- maximum mailbox items and aggregate mailbox-name/attribute bytes;
- maximum per-item and aggregate metadata/body detail;
- maximum target IDs, recipients, headers, and message bytes;
- maximum warnings, failures, unknown outcomes, and error-detail bytes;
- bounded string lengths for account/mailbox/path/query fields;
- maximum effective account and recipient/sender policy discovery cardinality;
- command/provider deadlines where safely enforceable.

Before returning, the adapter serializes or estimates with the canonical encoder
and rejects, reduces, or returns the explicit process-private oversized-result
handoff owned by spec 03 according to the documented result policy. It cannot
return an unbounded Python value merely because request cardinality was bounded.
Errors are shorter than the normal response ceiling and never include raw
provider responses, SQL, traceback, secret, body, or unintended local path.

The IMAP SEARCH pre-cardinality residual is documented in spec 06; after the
response arrives, candidate count/bytes are checked before any fetch or MCP
result construction.

## Result Semantics

Existing exact success literals remain exact where clients depend on them.
Multi-target/effect workflows use tagged bounded per-item outcomes and preserve
input order. They distinguish failure from unknown and provider success from
local projection/sent-copy warnings.

Errors caused by invalid input, policy denial, disabled/removed account, missing
binding, provider capability, conflict, limit, timeout, unknown effect, busy,
insecure storage, and internal failure map to stable safe categories. Framework
validation errors are normalized when necessary so they do not leak internals or
produce uncontrolled detail.

## Stdio Correctness

In stdio mode:

- stdout contains MCP protocol frames only;
- logs and diagnostics use stderr and obey redaction;
- startup freezes and preflights authority before protocol serving;
- malformed/oversized frames fail in a bounded way;
- request cancellation is propagated to application checkpoints;
- EOF, cancellation, initialization failure, and normal shutdown close all
  constructed resources;
- no browser UI, bootstrap token, HTTP route, or management server starts.

A generic MCP client or raw JSON-RPC harness validates initialization, catalog,
invocation, serialization, cancellation, and shutdown in addition to direct
Python tests.

## Compatibility and Evolution

A public catalog change requires:

1. update to the exact contract snapshot and relevant domain spec;
2. compatibility and security analysis plus migration/release note;
3. generic-client and GreenMail stdio validation;
4. a versioned alternative when semantics cannot be changed compatibly, unless
   retaining the old surface would preserve a material security flaw.

Removal of `add_email_account` is the explicit security exception: preserving a
credential-bearing MCP compatibility shim would violate the no-secret boundary.
Release notes and agent/CLI/UI guidance provide migration rather than emulation.

The current numeric message ID stays a compatibility field without claiming
UIDVALIDITY provenance. Introducing an epoch-bound identifier is additive or
versioned, never a silent reinterpretation.

This delivery adds no `ui://` resources, MCP App metadata, embedded-app CSP, or
host bridge calls. Any MCP App requires a separate accepted design and security
model.

## Acceptance Criteria

1. A complete exact catalog snapshot covers names, descriptions, input/output
   schemas, annotations, resources, visibility, and result shapes.
2. Schemas and results in both modes contain no secret input, value, reusable
   locator, or management operation; `add_email_account` is absent.
3. Tool visibility is static while current mode, lifecycle, policy, binding, and
   capability are enforced at invocation.
4. Schema, application, provider-work, serialized-response, spill-artifact, and
   error-detail bounds are tested at, below, and above each limit.
5. Partial results preserve order and distinguish known failure, unknown effect,
   provider success with local warning, and sent-copy outcome.
6. Raw protocol tests prove stdout purity, initialization/catalog behavior,
   malformed/oversized handling, cancellation, EOF, and cleanup.
7. GreenMail stdio E2E exercises representative read and mutation workflows
   through actual MCP framing, not only helper calls.
8. The catalog contains no MCP App, account/credential management, agent
   installation, or graphical management resource.
