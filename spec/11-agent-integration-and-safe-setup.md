# 11. Agent Integration and Safe Setup

## Purpose

Users of coding agents should be able to discover, install, diagnose, and operate
the Local Email App without exposing account credentials to a model or MCP
protocol. The project therefore ships a small agent integration package that can
be installed through supported Codex and Claude Code skill/plugin mechanisms.

This package combines a standards-compliant local stdio MCP declaration with
safe guidance and orchestration metadata. The MCP server provides only the mail
workflow surface from spec 10. The package is not a credential channel,
authorization boundary, management API, or substitute for the local CLI and Web
UI.

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

- use the mail tools exposed by the bundled local MCP server while respecting
  their schemas, annotations, allowlists, and confirmation boundaries;
- report the current published application's non-secret version;
- explain the distinction between MCP mail workflows and local management;
- inspect bounded read-only mode/status output that contains no secrets or
  reusable secret locators;
- guide the user to run the interactive CLI or local UI directly;
- explain provider-specific app-password prerequisites without requesting the
  value;
- verify non-secret post-setup state after the user completes the handoff;
- run bounded doctor/status and explicit `account test` connectivity diagnostics
  in their leaf `--json` mode and branch on stable fields/codes rather than
  parsing prose;
- provide the correct MCP client configuration after setup without embedding
  credentials.

When a user asks an agent to add or modify an account, the skill MUST stop before
credential collection and provide a concise handoff such as:

```text
Run `uvx mcp-email-server@latest ui` in your local terminal, or run the
documented interactive account setup command. Complete credential entry there,
then return
here and ask me to verify status.
```

The exact command comes from checked project documentation, not an invented
shell sequence. The CLI is the low-level management API and deliberately retains
revision, catalog, binding-state, and restart-state vocabulary. The plugin's MCP
declaration and bounded checks use the same current-published application channel:
`uvx mcp-email-server@latest`. Agent-run diagnostics are limited to the version
check, `config status --json`, and `config doctor --json`. Provider connectivity
checks and all management commands remain user-operated.

The agent requires the stable `schema_version: 1` envelope, validates `ok` and
`command`, treats unknown schemas/codes as unsupported, and summarizes only
approved mode, count, post-operation revision/restart, binding-health,
connectivity, problem, and handoff fields. Dispatched errors have a typed stable
`error.code` and its fixed safe message. The agent never infers permission to run
another command merely because that command also supports JSON.

JSON mode is a presentation contract, not a broader authority grant. Account
mutation, policy, import, reset, migration, and credential commands remain
user-operated. Secret-writing commands may receive secrets only through
user-controlled stdin; their JSON support exists for user-owned automation and
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
- download or execute unreviewed helper code merely to perform account setup;
- start a remotely bound service, daemon, share tunnel, or generic management
  endpoint.

If a host cannot provide a user-controlled terminal/browser handoff, the skill
must explain the limitation instead of degrading to chat-based secret entry.

## Repository and Distribution Model

The main project repository is the authority for the integration. Application
and plugin releases have independent lifecycles: the plugin changes when its
manifests, MCP declaration, skill, or related content change, while its
`@latest` selector follows the current published Python application. The
repository contains:

- one concise canonical skill body with no embedded release number;
- only the references needed for current-channel commands and troubleshooting;
- one shared root `.mcp.json` referenced by both vendor manifests;
- minimal marketplace and plugin manifests required by supported Codex and
  Claude Code installation mechanisms;
- validation that both hosts load identical canonical content;
- installation documentation with explicit source and update/remove steps.

The implemented layout is:

```text
plugins/mcp-email-server/
  .mcp.json
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  skills/safe-email-operations/
    SKILL.md
    references/
      installation.md
      safe-commands.md
```

Actual vendor paths and manifests follow the supported host specifications at
implementation time. The canonical workflow and security rules MUST NOT be
forked manually into divergent copies. If a host requires duplication, a
deterministic staging/check script generates or verifies it.

The skill itself remains declarative and contains no bundled executable script.
The shared MCP declaration runs `uvx --from mcp-email-server@latest
mcp-email-server-plugin`; it contains no credentials or environment forwarding.
That dedicated entry point is introduced with the mail-only contract, so legacy
releases fail closed instead of exposing their older MCP catalog. Any future helper script requires a
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

Installing the plugin and publishing the Python application remain separate
release lifecycles. Before plugin installation, guidance discloses that enabling
the bundled MCP server allows `uvx` to resolve or download the current published
application and run it locally. The skill must not install the plugin, replace
`@latest` with an unreviewed source, or perform account setup silently.

## Version and Capability Handling

Plugin semver identifies the bundled plugin content only; it does not identify
the Python application selected by `@latest`. Skill prose contains no concrete
release number and never requires the two versions to match. The exact running
application version comes only from the bounded application version check.

Bounded automation uses structured CLI output from the same current-published
channel when available. Unknown commands, schemas, or error codes are unsupported
rather than guessed. Human interactive commands remain user-operated. After
handoff, verification reports only approved non-secret lifecycle categories.

## Relationship to Other Interfaces

- MCP remains the bounded mail-workflow surface defined in spec 10.
- CLI is the complete low-level management and connectivity-diagnostic surface;
  the local Web UI intentionally omits provider connectivity, as defined in
  specs 04, 05, and 09.
- The agent integration packages that same MCP entry point and explains safe
  setup, but owns no additional business workflow or persistence.
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
4. The integration can perform only documented bounded non-secret version,
   status, and doctor diagnostics without direct TOML, SQLite, keyring, or
   `SecretStore` access; status and doctor require the tested schema-version-1
   envelope and reject unknown schema versions or error codes.
5. Static scans and behavioral tests find no credential placeholders, secret
   forwarding, bootstrap-token relay, opaque remote scripts, telemetry, or
   remote-management behavior.
6. Install, source verification, update, uninstall, `uvx` prerequisites, and
   independent plugin/application version lifecycles are documented and tested
   for both supported hosts.
7. Both plugin manifests reference one validated root `.mcp.json` that starts
   the dedicated `mcp-email-server-plugin` entry point from
   `mcp-email-server@latest`, forwards no credentials, fails closed on legacy
   releases, and exposes no management tool.
8. Vendor-specific manifests cannot drift from the canonical skill or MCP
   declaration without failing repository checks, and skill guidance contains no
   concrete release number.
9. Removing the historical MCP account-add tool has explicit release notes and
   optional migration guidance to interactive CLI and local UI.
