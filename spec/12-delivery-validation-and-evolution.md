# 12. Delivery, Validation, and Evolution

## Purpose

This document turns the domain acceptance criteria into release gates and keeps
traceability centralized. Detailed specs remain normative contracts and do not
carry implementation-status labels or per-file evidence diaries.

A requirement is complete only when implementation, automated verification,
published user documentation, packaged artifacts, and independent review agree.
Passing unit tests alone is not completion.

## Verification Matrix

The delivery maintains a reviewable matrix using the following ownership. Exact
file names may evolve, but every row must have concrete code and test references
before release.

| Contract area                                      | Owning spec | Required evidence                                                          |
| -------------------------------------------------- | ----------- | -------------------------------------------------------------------------- |
| foreground process and trust boundaries            | 01          | startup/shutdown, fail-closed, and route/transport tests                   |
| identities, revisions, outcomes, bounds vocabulary | 02          | domain purity and invariant tests                                          |
| layers, late secrets, resource/transaction scopes  | 03          | import/architecture, isolation, lifecycle, and transaction tests           |
| mode, catalog/account/policy/import lifecycle      | 04          | application + CLI + UI contract and crash/conflict tests                   |
| secret rotation/removal/recovery/redaction         | 05          | candidate-boundary, concurrency, leakage, and adapter parity tests         |
| mailbox/index/body/attachment reads                | 06          | provider fakes, SQLite, filesystem race, bounds, and GreenMail tests       |
| mutations and independent effects                  | 07          | capability, ambiguity, cancellation, no-bare-expunge, SMTP/sent-copy tests |
| schema, WAL/SHM security, retention/rebuild        | 08          | exact schema/migration, pre-open race, concurrency, corruption tests       |
| loopback UI security and packaging                 | 09          | backend, frontend, real-browser, artifact, and no-Node tests               |
| exact MCP contract and stdio behavior              | 10          | catalog snapshot, raw protocol, generic client, and GreenMail E2E          |
| agent integration and safe setup handoff           | 11          | Codex/Claude Code install fixtures, scenario, drift, and no-secret tests   |

For each normative acceptance item, the implementation review records:

```text
contract ID or heading
owning spec
production code reference
unit/contract/security test reference
E2E reference where applicable
published docs reference
review disposition
```

The matrix may live in test manifests or a maintained section of this document,
but it MUST be checked in, reviewable, and free of claims unsupported by the
current branch.

## Test Layers

### Domain and application

Use injected fakes to cover validation, revisions, ordering, bounds, authority
changes, late secret resolution, typed outcomes, cancellation checkpoints, and
external-effect planning. Direct service tests prove adapters are not the only
defense.

### Infrastructure and security

Cover concrete SQLite schema/migrations, DB/WAL/SHM/lock preflight, permissions,
symlinks and replacement races, keyring/SecretStore failures, provider protocol
edge cases, attachment no-follow behavior, bounded parser/provider payloads, and
redaction on unexpected failures.

### Interface contracts

- MCP: complete catalog snapshot and raw protocol behavior.
- CLI: command help/options, confirmations, stdin/masked secrets, conflict and
  cleanup results, no-secret output.
- Web backend: exact routes, authentication/session/CSRF/Host/Origin/CORS,
  headers, rate limits, body/response limits, and service mapping.
- Frontend: lint, typecheck, component/unit tests, secret-state clearing,
  conflict review, accessibility semantics, and deterministic production build.
- Agent integration: canonical skill validation, Codex/Claude Code installation
  fixtures, vendor-copy drift checks, version mismatch, safe handoff scenarios,
  and forbidden secret/bootstrap-token behavior.

### End to end

GreenMail tests exercise real IMAP/SMTP through MCP stdio, including restart,
managed selection, read/mutation evidence, rotation, disablement, and explicit
import. Browser E2E starts the installed UI process and exercises bootstrap,
management workflows, refresh/logout/stale tabs, conflict, and shutdown.

E2E must not use a developer keyring or real user config. Test fixtures have
bounded cleanup and never print sentinel secrets.

## Required Release Gates

Before delivery, all of the following pass from a clean checkout:

1. supported Python matrix (3.11 through 3.14 unless project support changes);
2. formatting, lint, type checking, lock consistency, and full Python tests with
   configured coverage threshold;
3. documentation strict build and link/cross-reference validation;
4. GreenMail stdio E2E;
5. frontend `npm ci`, lint, typecheck, unit tests, and production build;
6. Web backend security suite and real-browser E2E;
7. focused security regressions for secrets, exact Host/Origin/CSRF/bootstrap,
   SQLite pre-open sidecars, scoped expunge, attachment races, and bounds;
8. clean sdist and wheel build with exact content verification and license/notice
   checks;
9. wheel rebuilt from the sdist in an environment without Node/npm/network
   frontend access, with matching required static-asset manifest/hashes;
10. isolated wheel install and `mcp-email-server ui --no-open --port 0` smoke;
11. local-wheel `uvx` smoke including bootstrap/authenticated asset/status access
    and graceful termination;
12. Codex and Claude Code integration install/update/remove fixtures, canonical
    content drift, application-version mismatch, and no-secret handoff scenarios;
13. independent architecture, correctness, and security review with all material
    findings resolved or explicitly accepted by the maintainer;
14. clean git tree after generated-asset drift and all checks.

CI and release publishing invoke the same authoritative artifact build/verify
workflow. Release cannot publish an artifact that skipped frontend build,
notices, from-sdist reconstruction, installed UI smoke, or asset verification.
Prefer publishing the exact immutable artifacts validated by CI.

## Artifact Contract

The wheel contains only runtime Python/package resources and the complete
prebuilt local UI. The sdist contains the Python source, build metadata, required
scripts, frontend source and lockfile, notices, and the same prebuilt UI needed
for a Node-free wheel build.

Artifact verification rejects:

- missing index or referenced assets;
- stale/unreferenced generated chunks;
- missing third-party notices/license material;
- repository-relative runtime assumptions;
- unexpected source maps, debug artifacts, secrets, test traces, or browser
  recordings;
- Gradio remnants when the replacement is complete;
- MCP App bundles or metadata in this delivery;
- any PEP 517 step that invokes Node or downloads frontend dependencies.

Runtime assets are read via `importlib.resources`. If extraction context is
needed, its lifetime covers the server and is released on shutdown.

## Documentation Gates

Implementation changes update all affected user documentation in the same
change:

- README and getting started for mode/UI launch and restart behavior;
- configuration for catalog, accounts, policy, credential and import lifecycle;
- security for secret boundaries, loopback bootstrap/session, exact attachment
  path, SQLite files/sidecars, and transport posture;
- tools for the exact mail-only MCP contract, removal of account-add, and
  migration to CLI/UI;
- agent integration guidance for verified Codex/Claude Code install, safe
  credential handoff, version matching, update, and removal;
- transports for stdio baseline and local UI non-transport status;
- troubleshooting for doctor/cleanup, conflicts, insecure files, provider
  ambiguity, ports/browser, and artifact startup;
- validation for reproducible release and E2E commands.

Docs MUST distinguish accepted target behavior from released-version behavior
until the implementation ships. They do not claim that loopback alone is auth,
that attachments are confined to an approved workspace, or that public numeric
IDs carry listing epoch.

## Evolution Rules

A change to authority, persistent schema, secret lifecycle, public MCP contract,
UI trust model, provider-effect semantics, or cross-cutting limits requires an
accepted spec change before implementation. The owning document changes; other
specs link rather than duplicate.

Versioned migration is required for incompatible SQLite or MCP changes. Security
properties cannot be weakened through a compatibility flag without explicit
threat analysis and maintainer acceptance.

Deferred items such as MCP Apps, remote UI, hard purge, online backup/restore,
OAuth, epoch-bound public IDs, QRESYNC, or background sync require separate
scope, authority, security, migration, and acceptance designs. Their future
possibility does not add placeholders or generic abstractions now.

## Acceptance Criteria

1. Every acceptance item in specs 01-11 has concrete production, test, docs, and
   review references in the checked delivery matrix.
2. The entire Python/frontend/security/E2E/docs gate passes from a clean checkout
   and leaves no uncommitted generated drift.
3. Published wheel and sdist pass exact content checks; a wheel rebuilt from the
   sdist and the installed/`uvx` UI work without Node or frontend network access.
4. CI and release use the same verified artifact path and publish only artifacts
   that passed it.
5. Independent review finds no unresolved material correctness, authorization,
   secret, persistence, provider-effect, packaging, or test-adequacy issue.
6. User documentation matches implemented behavior and contains none of the
   rejected overclaims named above.
7. Future incompatible or deferred work cannot ship by silently changing this
   contract; it receives an owning spec and migration/compatibility decision.
