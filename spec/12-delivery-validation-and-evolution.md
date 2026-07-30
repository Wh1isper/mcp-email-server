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
| secret rotation/removal/cleanup/redaction          | 05          | atomic activation, unchanged-authority failure, leakage, and parity tests  |
| mailbox/index/body/attachment reads                | 06          | provider fakes, SQLite, filesystem race, bounds, and GreenMail tests       |
| mutations and independent effects                  | 07          | capability, ambiguity, cancellation, no-bare-expunge, SMTP/sent-copy tests |
| schema, WAL/SHM security, retention/rebuild        | 08          | exact schema/migration, pre-open race, concurrency, corruption tests       |
| loopback UI security and packaging                 | 09          | backend, frontend, real-browser, artifact, and no-Node tests               |
| exact MCP contract and stdio behavior              | 10          | catalog snapshot, raw protocol, generic client, and GreenMail E2E          |
| agent integration and safe setup handoff           | 11          | Codex/Claude Code install fixtures, scenario, drift, and no-secret tests   |

### Checked Delivery References

The following checked-in matrix records evidence for this implementation branch;
it is not a claim about an already released version. An acceptance-ID range
enumerates every criterion in that inclusive range. The final review disposition
is updated only after the independent review has examined the complete diff.

| Acceptance IDs | Production references                                                                                                         | Verification references                                                                                                                                     | Published documentation                                                                                              | Review disposition                                  |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| 01.1-01.5      | `mcp_email_server/bootstrap.py`, `runtime.py`, `cli.py`, `web_ui/server.py`                                                   | `tests/test_bootstrap.py`, `test_runtime_lifecycle.py`, `test_managed_runtime_and_fences.py`, `test_web_ui_server.py`                                       | `docs/getting-started.md`, `security.md`, `transports.md`                                                            | Independent review: no unresolved material findings |
| 02.1-02.6      | `mcp_email_server/emails/models.py`, `application/limits.py`, `application/{reads,metadata,mutations}.py`                     | `tests/test_models.py`, `test_application_limits.py`, `test_application_{management,reads}.py`, `test_mutation_application.py`                              | `docs/configuration.md`, `security.md`, `tools.md`                                                                   | Independent review: no unresolved material findings |
| 03.1-03.9      | `mcp_email_server/application/`, `adapters/{authority,management}.py`, `runtime.py`, `large_results.py`                       | `tests/test_application_*.py`, `test_*_adapters.py`, `test_large_results.py`, `test_runtime_lifecycle.py`                                                   | `docs/configuration.md`, `security.md`, `validation.md`                                                              | Independent review: no unresolved material findings |
| 04.1-04.8      | `mcp_email_server/config.py`, `managed.py`, `application/management.py`, `adapters/management.py`, `cli.py`, `web_ui/app.py`  | `tests/test_managed_catalog.py`, `test_managed_cli.py`, `test_application_management.py`, `test_management_adapters.py`, `test_web_ui_management.py`        | `docs/getting-started.md`, `configuration.md`, `security.md`, `troubleshooting.md`                                   | Pending independent review                          |
| 05.1-05.8      | `mcp_email_server/keyring_store.py`, `managed.py`, `application/management.py`, `adapters/authority.py`                       | `tests/test_keyring_store.py`, `test_application_management.py`, `test_managed_catalog.py`, `test_managed_runtime_and_fences.py`, `test_web_ui_security.py` | `docs/configuration.md`, `security.md`, `troubleshooting.md`                                                         | Independent review: no unresolved material findings |
| 06.1-06.7      | `mcp_email_server/metadata_index.py`, `application/{metadata,reads}.py`, `adapters/{metadata,reads}.py`, `emails/classic.py`  | `tests/test_metadata_*.py`, `test_read_*.py`, `test_email_client.py`, `test_email_attachments.py`, `e2e/test_stdio_greenmail.py`                            | `docs/tools.md`, `guides.md`, `security.md`, `validation.md`                                                         | Independent review: no unresolved material findings |
| 07.1-07.9      | `mcp_email_server/application/mutations.py`, `adapters/mutations.py`, `emails/classic.py`                                     | `tests/test_mutation_*.py`, `test_scoped_expunge_regression.py`, `test_save_to_{mailbox,sent}.py`, `e2e/test_stdio_greenmail.py`                            | `docs/tools.md`, `guides.md`, `security.md`, `troubleshooting.md`                                                    | Independent review: no unresolved material findings |
| 08.1-08.10     | `mcp_email_server/managed.py`, `metadata_index.py`, `large_results.py`, `adapters/reads.py`                                   | `tests/test_managed_catalog.py`, `test_metadata_index.py`, `test_large_results.py`, `test_read_adapters.py`                                                 | `docs/configuration.md`, `security.md`, `troubleshooting.md`                                                         | Independent review: no unresolved material findings |
| 09.1-09.8      | `mcp_email_server/web_ui/`, `frontend/`, `dev/build_frontend.py`, `Makefile`                                                  | `tests/test_web_ui_*.py`, `test_packaging.py`, `frontend/src/*.test.tsx`, `frontend/src/components/*.test.tsx`, `frontend/e2e/local-management.spec.ts`     | `docs/getting-started.md`, `configuration.md`, `security.md`, `transports.md`, `troubleshooting.md`, `validation.md` | Independent review: no unresolved material findings |
| 10.1-10.8      | `mcp_email_server/app.py`, `stdio.py`, `application/limits.py`, `large_results.py`                                            | `tests/fixtures/mcp_catalog.json`, `tests/test_mcp_tools.py`, `test_stdio_protocol.py`, `test_large_results.py`, `e2e/test_stdio_greenmail.py`              | `docs/tools.md`, `transports.md`, `validation.md`                                                                    | Independent review: no unresolved material findings |
| 11.1-11.8      | `plugins/mcp-email-server/`, `.agents/plugins/marketplace.json`, `.claude-plugin/marketplace.json`, `mcp_email_server/cli.py` | `tests/test_agent_integrations.py`, `test_cli.py`, `test_web_ui_server.py`                                                                                  | `docs/guides.md`, `getting-started.md`, `security.md`                                                                | Independent review: no unresolved material findings |

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
symlinks and replacement races, Linux `managed_secret` transaction atomicity,
system-keyring/SecretStore failures, unchanged binding authority after failed
saves, provider protocol edge cases, attachment no-follow behavior, bounded
parser/provider payloads, and redaction on unexpected failures.

### Interface contracts

- MCP: complete catalog snapshot, application-version identity, reviewed tool
  annotations, text/structured discovery equivalence, and raw protocol behavior.
- CLI: command help/options, confirmations, user-controlled stdin secrets, one
  parsed schema-version-1 JSON envelope for every finite command, typed errors
  with fixed safe messages, post-operation revisions/restart state,
  conflict/cleanup results, path-free agent diagnostics, and no-secret output.
- Web backend: exact routes including absence of provider-connectivity routes,
  authentication/session/CSRF/Host/Origin/CORS, headers, rate limits,
  body/response limits, bounded error categories with fixed safe messages, and
  access logs limited to fixed operation id, bounded method, status, and
  duration.
- Frontend: lint, typecheck, component/unit tests, email/password-first account
  flow, folded advanced and optional outgoing settings, no connection preview,
  individual allowlist-item editing and distinct empty semantics, hidden no-action
  empty/settled status, secret-state clearing, conflict review, accessibility semantics,
  and deterministic production build.
- Agent integration: canonical skill validation, bounded JSON status/doctor
  parsing without deriving command authority, one shared Codex/Claude Code
  `.mcp.json` declaration, independent plugin/application version lifecycles,
  safe handoff scenarios, and forbidden secret/bootstrap-token behavior.

### End to end

GreenMail tests exercise real IMAP/SMTP through MCP stdio, including restart,
managed selection, read/mutation evidence, rotation, disablement, and explicit
import. Browser E2E starts the installed UI process and exercises bootstrap,
management workflows, refresh/logout/stale tabs, conflict, and shutdown.

E2E must not use a developer keyring or real user config. Test fixtures have
bounded cleanup and never print sentinel secrets.

## Required Release Gates

Before delivery, all of the following pass from a clean checkout:

1. supported Python matrix (3.11 through 3.14 unless project support changes),
   rerun from the exact peeled release-tag commit after the isolated release job
   deterministically stamps its canonical `X.Y.Z` value (accepting an optional
   `v` tag prefix) into only Python package metadata and the matching editable
   lock entry;
2. formatting, lint, type checking, lock consistency, and full Python tests with
   combined in-process/subprocess statement-and-branch coverage at or above the
   configured 80% aggregate threshold, plus an 80% changed-line Codecov target;
3. documentation strict build and link/cross-reference validation;
4. GreenMail stdio E2E;
5. frontend `npm ci`, lint, typecheck, unit tests, and production build;
6. Web backend security suite and real-browser E2E;
7. focused security regressions for secrets, exact Host/Origin/CSRF/bootstrap,
   safe Web access logs and fixed error messages, SQLite pre-open sidecars,
   scoped expunge, attachment races, and bounds;
8. clean sdist and wheel build with exact content verification and license/notice
   checks;
9. wheel rebuilt from the sdist in an environment without Node/npm/network
   frontend access, with matching required static-asset manifest/hashes;
10. isolated wheel install and PTY-backed `mcp-email-server ui --no-open
--port 0` bootstrap/authenticated asset/status smoke;
11. PTY-backed local-wheel `uvx` smoke including bootstrap/authenticated
    asset/status access and graceful termination;
12. Codex and Claude Code integration install/update/remove fixtures, shared
    MCP-declaration drift, independent version lifecycles, current-channel launch,
    and no-secret handoff scenarios;
13. independent architecture, correctness, and security review with all material
    findings resolved or explicitly accepted by the maintainer;
14. clean git tree after generated-asset drift and all checks.

CI and release publishing invoke the same authoritative artifact build/verify
workflow. Release cannot publish an artifact that skipped the supported Python
matrix, frontend build, notices, from-sdist reconstruction, authenticated
installed/`uvx` UI smoke, or asset verification. The release workflow pins and
verifies the peeled tag commit, deterministically stamps only application package
and lock metadata from the canonical tag, then builds `dist/` once in an
unprivileged validation job. It records checksums and gives the credential-bearing
publish job only those unchanged verified bytes. Plugin metadata is not part of
application release stamping.

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
- agent integration guidance for verified Codex/Claude Code install, shared
  local MCP launch, safe credential handoff, independent version lifecycles,
  update, and removal;
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

After a managed catalog or MCP contract is released, versioned migration is
required for incompatible changes. This pre-release managed-catalog redesign has
no current V2 users and makes no compatibility promise for earlier development
schemas. Explicit import from legacy TOML, environment, and keyring sources
remains required. Security properties cannot be weakened through a compatibility
flag without explicit threat analysis and maintainer acceptance.

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
