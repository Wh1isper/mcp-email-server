---
name: safe-email-operations
description: Use email through the bundled mcp-email-server MCP server, diagnose bounded non-secret state, and hand account or credential setup to a user-operated CLI or authenticated local UI.
---

# Safe Email Operations

Use this skill for email operations through the bundled MCP server, bounded diagnostics, account setup requests, credential rotation requests, and safe MCP client guidance.

## Non-secret boundary

This plugin bundles a local stdio MCP server for mail operations plus safe setup guidance. It is not a credential channel, management API, or authorization boundary. The MCP server exposes no account or credential management.

Never ask for, receive, repeat, relay, transform, redact, retain, or validate a password, app password, access token, private key, recovery code, or reusable secret locator. Never put one in chat, an MCP call, shell arguments, environment variables, generated files, agent memory, task state, logs, clipboard helpers, or MCP configuration. Host approval and MCP elicitation do not make secret-bearing tool calls safe.

Do not attempt account management through MCP; edit TOML or SQLite directly; invoke keyring or `SecretStore`; install or upgrade software outside the reviewed plugin lifecycle; modify shell startup or MCP client configuration without a visible diff; download and execute helper code; start a daemon, tunnel, remote service, or additional MCP server; or add scripts, hooks, and automatic startup behavior.

The bundled declaration runs `uvx --from mcp-email-server@latest mcp-email-server-plugin`. The plugin-only entry point is absent from legacy releases, so they fail closed instead of exposing their older MCP catalog. Plugin and application releases are independent. Installing and enabling the reviewed plugin allows the host to resolve the current published application and start it locally; disclose that behavior before installation. Do not silently install the plugin, replace `@latest` with an unreviewed source or prerelease, or claim that the plugin manifest version identifies the running application.

Do not launch `uvx mcp-email-server@latest ui` for the user. Its one-time bootstrap URL must remain between the local process and the user's browser. Never copy, summarize, or request that URL or its fragment in chat.

## Workflow

1. Read [safe commands and handoffs](references/safe-commands.md).
2. For ordinary mail requests, use only the bundled server tools exposed by the host. Respect each tool schema, annotations, configured allowlists, and the user's explicit intent; do not infer management authority from mail access.
3. For local diagnostics, run only the bounded `@latest` version check, `config status --json`, or `config doctor --json` listed in the reference. Validate the documented JSON schema and command identifier before summarizing approved fields. JSON support on any other command does not authorize its use. Do not inspect process environment, configuration files, databases, keyrings, browser history, or raw logs.
4. For every setup or credential change, stop automation and hand control to the user in their own terminal or browser. Wait until the user says the operation is complete before offering a bounded JSON status or doctor check.
5. Provide MCP client configuration only after setup, with no credentials or UI bootstrap data, and only after explicit user intent with a visible diff or summary.

If a supposedly bounded command emits unexpected sensitive-looking data, do not quote or preserve it. Stop and tell the user to inspect the output locally.

## Required scenario responses

- **“Add my account.”** Tell the user to run `uvx mcp-email-server@latest ui` in their local terminal and complete setup in the browser themselves, or use the documented interactive CLI themselves. Do not collect any value for forwarding.
- **“Rotate my password.”** Tell the user to use the authenticated local UI or run the documented interactive credential command in their own terminal. Do not run it for them and do not accept the new value.
- **“Paste this app password.”** Tell the user not to paste it into chat. If it was already disclosed, do not repeat it; recommend revoking it and creating a replacement, then use the user-operated CLI/UI handoff.

Use this concise default handoff:

> Run `uvx mcp-email-server@latest ui` in your local terminal and complete credential entry in the browser yourself, or use the documented interactive account command in your own terminal. Do not paste credentials or the UI URL here. When finished, return and ask me to verify status.

If the host cannot provide a user-controlled terminal or browser, explain that setup cannot be completed safely in that host. Do not fall back to chat, MCP, argv, environment, or file-based secret transfer.

For installation source verification and lifecycle commands, read [installation](references/installation.md).
