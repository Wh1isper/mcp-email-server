# Safe commands and handoffs

## Bundled MCP mail tools

The plugin host starts the declared local stdio server with `uvx --from mcp-email-server@latest mcp-email-server-plugin`. This plugin-only entry point is unavailable in legacy application releases, which therefore fail closed instead of exposing their older MCP catalog. Use only the mail tools the host exposes, and follow their schemas, annotations, configured recipient/sender policy, and confirmation boundaries. The bundled MCP surface does not authorize account setup, credential changes, direct configuration access, or additional shell commands.

## Agent-run checks

After the user intentionally installs and enables the reviewed plugin, the agent may run only these bounded, non-secret application checks when diagnosis is needed:

```text
uvx mcp-email-server@latest --version
uvx mcp-email-server@latest config status --json
uvx mcp-email-server@latest config doctor --json
```

The plugin manifest version and application version are independent; do not compare them or infer the running application version from plugin metadata. If the executable does not support a listed command, stop and use documentation from the current published application; do not guess a replacement command. Parse status and doctor only when `schema_version` is `1`, `ok` is true, and `command` matches the requested check. Treat an unknown schema, command, or error code as unsupported. Summarize only non-secret mode, catalog status, restart requirement, lifecycle/schema/revision, account counts, binding-health counts, and bounded problems or handoff hints; never expose or infer omitted fields.

Do not augment these checks with environment dumps, filesystem searches, configuration/database reads, keyring queries, raw logs, network probes, or MCP calls. JSON support on another CLI command does not authorize the agent to run it.

## User-operated management

For adding or editing an account, tell the user to run this themselves in a local terminal:

```text
uvx mcp-email-server@latest ui
```

The user must open and operate the authenticated local browser UI themselves. The agent must not execute the command, inspect browser state, or receive the bootstrap URL.

A terminal-only user may inspect the installed release's interactive syntax and then run it themselves:

```text
uvx mcp-email-server@latest account add --help
uvx mcp-email-server@latest account set-secret --help
```

Secret prompts belong to that user-controlled terminal. Do not ask the user to pipe, forward, capture, paste, or save their input for the agent. Account names and endpoint settings are not permission to receive credentials.

After the user reports completion, offer only `config status --json` or `config doctor --json`. MCP remains a mail-workflow surface, not an account or credential management surface.

## Provider prerequisites

You may explain that a provider can require multi-factor authentication, IMAP/SMTP enablement, or a provider-created app password. Explain where the user can learn about the prerequisite, but never ask for its value or walk it through agent-visible tools.
