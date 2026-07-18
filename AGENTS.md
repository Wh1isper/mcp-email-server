# Repository Overview

`mcp-email-server` is a Python MCP server that connects MCP clients to email
accounts through IMAP and SMTP.

Primary repository areas:

- `mcp_email_server/app.py` — FastMCP server, resources, tools, and tool visibility.
- `mcp_email_server/cli.py` — stdio, SSE, Streamable HTTP, UI, reset, and credential migration commands.
- `mcp_email_server/config.py` — TOML settings, environment composition, account models, and persistence.
- `mcp_email_server/keyring_store.py` — operating system keyring integration.
- `mcp_email_server/emails/` — IMAP and SMTP behavior and response models.
- `mcp_email_server/ui.py` — Gradio account configuration UI.
- `tests/` — unit and integration-style tests with mocked mail services.
- `docs/` — user documentation published with MkDocs.
- `spec/` — unpublished architecture and product design proposals kept as flat numbered documents.

## Project Conventions

- Support Python 3.11 and later.
- Use `uv` for dependency management and command execution.
- Follow the existing typed, asynchronous Python style.
- Prefer explicit types and `isinstance` checks over dynamic attribute checks.
- Use Pydantic models for configuration and structured responses.
- Keep MCP-facing descriptions accurate because clients derive tool schemas from them.
- Preserve the distinction between persistent TOML settings and the environment-composited runtime view.
- Treat credential storage, allowlists, attachment paths, and HTTP transport settings as security-sensitive behavior.
- Do not log, document, or commit real email credentials, API keys, message contents, or tokens.

## Current Architecture Direction

The proposed target under `spec/` is a local, single-user Email App: MCP uses
stdio, CLI is the management plane, SQLite stores managed non-secret
configuration and reusable mail metadata/index state, and a `SecretStore` owns
credentials. Current TOML, environment, and keyring behavior remains a
compatibility mode and explicit import source. HTTP, daemon, multi-user, and
cloud-service design are out of scope for this proposal.

## Development Workflow

Install the environment and pre-commit hooks:

```bash
make install
```

Before completing a change, run:

```bash
make check
make test
make docs-test
```

During development, focused checks are encouraged, but the full relevant suite
must pass before a change is considered complete.

Useful commands:

| Command                         | Description                                                  |
| ------------------------------- | ------------------------------------------------------------ |
| `uv run mcp-email-server stdio` | Run the local stdio MCP server.                              |
| `uv run mcp-email-server ui`    | Open the account configuration UI.                           |
| `make check`                    | Run lockfile, formatting, lint, type, and dependency checks. |
| `make test`                     | Run the test suite with coverage.                            |
| `make docs-test`                | Build the MkDocs site in strict mode.                        |
| `make docs`                     | Serve the documentation locally.                             |

## Documentation Requirements

Code and documentation must remain aligned.

- Every code change must include a review and corresponding update of the relevant documentation in the same change.
- User-visible behavior changes must always update the appropriate page under `docs/`; internal changes must still keep docstrings and developer guidance accurate.
- Configuration fields or environment variables require updates to `docs/configuration.md` and, when security-sensitive, `docs/security.md`.
- CLI commands, arguments, transport defaults, or HTTP security behavior require updates to `docs/transports.md`.
- MCP tool names, parameters, responses, visibility, or workflows require updates to `docs/tools.md`.
- New special cases and operational caveats belong in `docs/guides.md` or `docs/troubleshooting.md`.
- Keep `README.md` limited to the quickest supported configuration path and links to the full documentation.
- Update `CONTRIBUTING.md` when contributor or release workflows change.
- Run `make docs-test` after changing code or documentation that can affect published docs.

## Specification Requirements

`spec/` is the repository's workspace for architecture and product design before
those decisions become stable implementation or published user behavior.

- Keep `spec/` outside the MkDocs navigation; published, implemented behavior belongs under `docs/`.
- Maintain `spec/README.md` as the global spec map and keep the current proposal's numbered documents directly under `spec/`.
- Name detailed documents `NN-topic.md` and order them from system context and boundaries toward workflows, data design, and interfaces.
- Write specs in English Markdown and use Mermaid for architecture, sequence, state, flow, and ER diagrams.
- Cross-reference owning specs instead of duplicating contracts across files.
- Declare each spec `Proposed`, `Accepted`, `Implemented`, or `Superseded`; proposed text must not imply that code already exists.
- Update the owning spec before or with architecture, workflow, persistence, or security-boundary changes.
- When a design ships, update its status, implementation/test evidence, and the corresponding user documentation under `docs/`.
- Keep unresolved product decisions explicit in the owning spec rather than hiding them in implementation plans.

The current Local Email App proposal starts at `spec/README.md`. Continue design
discussions by updating those numbered documents and their cross-references.

## AnyCap

This project uses [AnyCap](https://anycap.ai) for web research, web crawling,
multimodal generation and understanding, file sharing, and static page hosting.
Before using it, read the installed AnyCap skill and verify the locally installed
CLI and authentication:

```bash
anycap status
```

Submit a bug or feature request when a capability fails or is missing:

```bash
anycap feedback --type bug -m "describe the issue" --request-id <id>
anycap feedback --type feature -m "describe the use case"
```

## Testing Expectations

- Add or update tests for every behavior change and regression fix.
- Keep unit tests deterministic and independent of live IMAP, SMTP, or keyring services.
- Run `make test-e2e` for changes to IMAP, SMTP, MCP stdio, configuration loading, attachment handling, or mailbox mutations. This uses synthetic accounts on a loopback-only GreenMail container, and CI runs it once on the default Python version.
- Cover both successful operations and security or failure boundaries.
- When changing configuration, test TOML loading, supported environment overrides, persistence, and migration behavior as applicable.
- When changing MCP tools, test schemas, responses, conditional visibility, and account-specific error paths.

## Repository Change Checklist

Keep related files synchronized:

- Dependency changes: `pyproject.toml` and `uv.lock`.
- Public configuration changes: implementation, tests, and `docs/configuration.md`.
- Credential or access-control changes: implementation, tests, `docs/security.md`, and troubleshooting guidance.
- MCP surface changes: `mcp_email_server/app.py`, tests, and `docs/tools.md`.
- CLI or transport changes: `mcp_email_server/cli.py`, tests, and `docs/transports.md`.
- Architecture, workflow, persistence, or security-boundary changes: owning files under `spec/`, tests, and relevant published docs when implemented.
- Quick-start changes: `README.md`, `docs/getting-started.md`, and `mkdocs.yml` when navigation changes.
