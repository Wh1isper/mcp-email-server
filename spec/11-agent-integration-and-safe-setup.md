# 11. Agent Integration and Safe Setup

## Purpose

Users of coding agents should be able to discover, install, diagnose, and operate
the Local Email App without exposing account credentials to a model or MCP
protocol. The project therefore ships a small agent integration package that can
be installed through supported Codex and Claude Code skill/plugin mechanisms.

This package is guidance and orchestration metadata. It is not a credential
channel, authorization boundary, management API, MCP tool, or substitute for the
local CLI and Web UI.

## Security Decision

The MCP catalog contains no account-add, account-edit, credential, import, or
other management tool. In particular, `add_email_account` is removed rather than
retained as a legacy exception.

Host approval or human-in-the-loop prompts do not make secret-bearing MCP tool
arguments safe. A value may still pass through model context, protocol payloads,
client histories, traces, logs, screenshots, or third-party middleware. MCP
client support for approval and elicitation also varies and cannot form the
product's credential security baseline.

All account and credential setup occurs through one of:

- an interactive CLI command in a user-controlled terminal, using masked
  input/stdin or another documented non-argv secret channel; or
- the authenticated local Web UI opened and operated by the user.

Removing the historical MCP tool is an intentional security-breaking change. It
requires release notes and migration guidance rather than a compatibility shim
that continues accepting credentials.

## Supported Agent Workflows

The integration may help an agent:

- detect whether `mcp-email-server` is installed and report its non-secret
  version;
- explain the distinction between MCP mail workflows and local management;
- inspect bounded read-only mode/status output that contains no secrets or
  reusable secret locators;
- guide the user to run the interactive CLI or local UI directly;
- explain provider-specific app-password prerequisites without requesting the
  value;
- verify non-secret post-setup state after the user completes the handoff;
- run bounded doctor/status commands in their leaf `--json` mode and branch on
  stable fields/codes rather than parsing prose;
- provide the correct MCP client configuration after setup without embedding
  credentials.

When a user asks an agent to add or modify an account, the skill MUST stop before
credential collection and provide a concise handoff such as:

```text
Run `mcp-email-server ui` in your local terminal, or run the documented
interactive account setup command. Complete credential entry there, then return
here and ask me to verify status.
```

The exact command is version-aware and comes from checked project documentation,
not an invented shell sequence. For the matching V2 release, bounded agent-run
checks use `mcp-email-server config status --json` and `mcp-email-server config
doctor --json`. The agent validates `schema_version`, `ok`, and `command`, treats
unknown schemas/codes as unsupported, and summarizes only approved lifecycle,
count, restart, binding-health, problem, and handoff fields. It never infers
permission to run another command merely because that command also supports JSON.

JSON mode is a presentation contract, not a broader authority grant. Account,
policy, import, reset, migration, and credential commands remain user-operated;
secret-writing JSON commands requiring stdin exist for user-owned automation and
MUST NOT be used to route secrets through an agent.

## Forbidden Agent Behavior

The skill/plugin MUST NOT:

- ask a user to paste a password, token, app password, private key, or reusable
  secret locator into chat;
- accept a secret for later forwarding, redaction, transformation, or storage;
- place credentials in shell argv, environment variables, generated files,
  agent memory, task state, clipboard helpers, logs, or MCP configuration;
- call a removed/private MCP management tool or directly edit TOML/SQLite;
- invoke keyring or `SecretStore` backends directly;
- launch the UI in a way that relays its bootstrap fragment through model-visible
  output, or copy the bootstrap URL into chat;
- claim that host approval, tool annotations, JSON output, or elicitation makes
  secret-bearing tool calls safe;
- install binaries/plugins, modify shell startup files, or change MCP client
  configuration without explicit user intent and a visible diff/summary;
- download or execute unpinned helper code merely to perform account setup;
- start a remotely bound service, daemon, share tunnel, or generic management
  endpoint.

If a host cannot provide a user-controlled terminal/browser handoff, the skill
must explain the limitation instead of degrading to chat-based secret entry.

## Repository and Distribution Model

The main project repository is the authority for the integration so application,
CLI, docs, and guidance can be versioned together. It contains:

- one concise canonical skill body;
- only the references needed for versioned commands and troubleshooting;
- minimal vendor manifests/wrappers required by supported Codex and Claude Code
  installation mechanisms;
- validation that staged vendor copies are semantically identical to the
  canonical source;
- installation documentation with explicit source, version, and update/remove
  steps.

A representative layout is:

```text
integrations/
  agents/
    mcp-email-server/
      SKILL.md
      references/
        commands.md
        security-handoff.md
    codex/
      ... minimal installation metadata or staged skill ...
    claude-code/
      ... minimal plugin manifest or staged skill ...
```

Actual vendor paths and manifests follow the supported host specifications at
implementation time. The canonical workflow and security rules MUST NOT be
forked manually into divergent copies. If a host requires duplication, a
deterministic staging/check script generates or verifies it.

The skill itself should be mostly declarative. The first release SHOULD avoid
bundled executable scripts; invoking the installed, version-matched
`mcp-email-server` CLI is preferable. Any future helper script requires a
specific need, source review, deterministic tests, and the same no-secret rule.

## Installation Contract

Installation is explicit and attributable to a repository URL and version or
commit. The documented path should be as close as host support permits to:

1. inspect the source/repository identity;
2. install or register the official integration through the host's native
   skill/plugin mechanism;
3. verify the installed skill name and version/source;
4. update or uninstall it through the same mechanism.

The project MUST NOT curl-and-execute an opaque shell script as the primary
installation path. Generated vendor packages contain no credentials, telemetry,
remote code loaders, binary payloads, or unrelated agent instructions.

Plugin/skill installation and Python application installation are separate
steps. The integration may explain supported Python installation commands, but
must not silently install or upgrade the application while handling an account
request.

## Version and Capability Handling

The integration checks the installed application version before suggesting
commands. It does not assume a command exists because it appears in the latest
repository. If versions differ, it recommends either version-matched guidance or
an explicit application upgrade.

Read-only automation uses structured/bounded CLI output when available. Human
interactive commands remain user-operated. After handoff, verification reports
only non-secret lifecycle and connectivity categories.

## Relationship to Other Interfaces

- MCP remains the bounded mail-workflow surface defined in spec 10.
- CLI and local Web UI remain the complete management surfaces defined in specs
  04, 05, and 09.
- The agent integration explains and invokes safe entry points but owns no
  business workflow or persistence.
- The integration is optional. The application remains fully usable without a
  coding agent, plugin marketplace, or skill installation.

## Acceptance Criteria

1. The MCP contract snapshot contains no `add_email_account`, credential,
   account-management, import, or generic management tool.
2. Codex and Claude Code installation fixtures load the same canonical workflow
   and security rules through their supported repository mechanisms.
3. Scenario tests for “add my account,” “rotate my password,” and “paste this app
   password” always hand off to user-operated CLI/UI and never request or retain
   the value in chat.
4. The integration can perform only documented bounded non-secret status/version
   checks without direct TOML, SQLite, keyring, or `SecretStore` access; status
   and doctor use the tested JSON envelope and reject unknown schema versions.
5. Static scans and behavioral tests find no credential placeholders, secret
   forwarding, bootstrap-token relay, opaque remote scripts, telemetry, or
   remote-management behavior.
6. Install, source/version verification, update, uninstall, and application
   version mismatch are documented and tested for both supported hosts.
7. Vendor-specific staged content cannot drift from the canonical skill without
   failing repository checks.
8. Removing the historical MCP account-add tool has explicit release notes and
   migration guidance to interactive CLI and local UI.
