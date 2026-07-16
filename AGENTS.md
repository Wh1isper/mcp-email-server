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

## Testing Expectations

- Add or update tests for every behavior change and regression fix.
- Keep tests deterministic and independent of live IMAP, SMTP, or keyring services.
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
- Quick-start changes: `README.md`, `docs/getting-started.md`, and `mkdocs.yml` when navigation changes.
