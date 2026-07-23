# Safe commands and handoffs

## Agent-run checks

The agent may run only these bounded, non-secret application checks:

```text
mcp-email-server --version
mcp-email-server config status
mcp-email-server config doctor
```

Run the version check first. The plugin and application versions should match (`0.0.1` for this release). If the executable does not support the listed command or the versions differ, stop and use documentation from the installed application release; do not guess a replacement command. Status and doctor output may be summarized only as non-secret lifecycle, account-count, binding-health, cleanup, and connectivity categories.

Do not augment these checks with environment dumps, filesystem searches, configuration/database reads, keyring queries, raw logs, network probes, or MCP calls.

## User-operated management

For adding or editing an account, tell the user to run this themselves in a local terminal:

```text
mcp-email-server ui
```

The user must open and operate the authenticated local browser UI themselves. The agent must not execute the command, inspect browser state, or receive the bootstrap URL.

A terminal-only user may inspect the installed release's interactive syntax and then run it themselves:

```text
mcp-email-server account add --help
mcp-email-server account set-secret --help
```

Secret prompts belong to that user-controlled terminal. Do not ask the user to pipe, forward, capture, paste, or save their input for the agent. Account names and endpoint settings are not permission to receive credentials.

After the user reports completion, offer only `config status` or `config doctor`. MCP remains a mail-workflow surface, not an account or credential management surface.

## Provider prerequisites

You may explain that a provider can require multi-factor authentication, IMAP/SMTP enablement, or a provider-created app password. Explain where the user can learn about the prerequisite, but never ask for its value or walk it through agent-visible tools.
