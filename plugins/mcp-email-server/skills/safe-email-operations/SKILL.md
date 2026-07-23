---
name: safe-email-operations
description: Safely diagnose mcp-email-server and hand account or credential setup to a user-operated interactive CLI or authenticated local UI without exposing secrets to an agent or MCP.
---

# Safe Email Operations

Use this skill for mcp-email-server installation checks, bounded diagnostics, account setup requests, credential rotation requests, and safe MCP client guidance.

## Non-secret boundary

This plugin is guidance only. It is not a credential channel, management API, MCP server, or authorization boundary.

Never ask for, receive, repeat, relay, transform, redact, retain, or validate a password, app password, access token, private key, recovery code, or reusable secret locator. Never put one in chat, an MCP call, shell arguments, environment variables, generated files, agent memory, task state, logs, clipboard helpers, or MCP configuration. Host approval and MCP elicitation do not make secret-bearing tool calls safe.

Do not call account-management MCP tools; edit TOML or SQLite directly; invoke keyring or `SecretStore`; install or upgrade software without explicit user intent; modify shell startup or MCP client configuration without a visible diff; download and execute helper code; start a daemon, tunnel, remote service, or MCP server; or add scripts, hooks, and automatic startup behavior.

Do not launch `mcp-email-server ui` for the user. Its one-time bootstrap URL must remain between the local process and the user's browser. Never copy, summarize, or request that URL or its fragment in chat.

## Workflow

1. Read [safe commands and handoffs](references/safe-commands.md).
2. Establish the installed application version using only the bounded version check. This plugin release is `0.0.1` and is intended for application `0.0.1`.
3. If versions differ, stop using release-specific commands. Offer version-matched guidance or an explicit application upgrade; never upgrade silently.
4. Run only a documented, bounded `version`, `config status`, or `config doctor` check. Do not inspect process environment, configuration files, databases, keyrings, browser history, or raw logs.
5. For every setup or credential change, stop automation and hand control to the user in their own terminal or browser. Wait until the user says the operation is complete before offering a bounded status or doctor check.
6. Provide MCP client configuration only after setup, with no credentials or UI bootstrap data, and only after explicit user intent with a visible diff or summary.

If a supposedly bounded command emits unexpected sensitive-looking data, do not quote or preserve it. Stop and tell the user to inspect the output locally.

## Required scenario responses

- **“Add my account.”** Tell the user to run `mcp-email-server ui` in their local terminal and complete setup in the browser themselves, or use the documented interactive CLI themselves. Do not collect any value for forwarding.
- **“Rotate my password.”** Tell the user to use the authenticated local UI or run the documented interactive credential command in their own terminal. Do not run it for them and do not accept the new value.
- **“Paste this app password.”** Tell the user not to paste it into chat. If it was already disclosed, do not repeat it; recommend revoking it and creating a replacement, then use the user-operated CLI/UI handoff.

Use this concise default handoff:

> Run `mcp-email-server ui` in your local terminal and complete credential entry in the browser yourself, or use the documented interactive account command in your own terminal. Do not paste credentials or the UI URL here. When finished, return and ask me to verify status.

If the host cannot provide a user-controlled terminal or browser, explain that setup cannot be completed safely in that host. Do not fall back to chat, MCP, argv, environment, or file-based secret transfer.

For installation source verification and lifecycle commands, read [installation](references/installation.md).
