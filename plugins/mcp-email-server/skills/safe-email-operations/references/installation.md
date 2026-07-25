# Installation and provenance

The authoritative source is `https://github.com/Wh1isper/mcp-email-server`. Inspect the repository identity, selected plugin release or commit, both marketplace files, both plugin manifests, the root `.mcp.json`, and the canonical `skills/safe-email-operations/SKILL.md` before installation. Plugin and Python application releases are independent.

The bundled MCP declaration runs `uvx --from mcp-email-server@latest mcp-email-server-plugin`. Confirm that `uvx` is installed and understand that enabling the plugin may resolve and download the current published Python package before starting it locally. The plugin-only entry point is intentionally absent from legacy releases, so they fail closed rather than exposing their older MCP catalog. No email credential belongs in the plugin, marketplace, MCP declaration, chat, or command line.

Prefer a reviewed signed plugin release when available; otherwise record and review the exact commit selected. Do not curl and execute an installer.

## Codex

Add the repository marketplace with the native CLI:

```text
codex plugin marketplace add https://github.com/Wh1isper/mcp-email-server.git
codex plugin marketplace list
```

In the ChatGPT desktop app, open the Plugins Directory, select the `mcp-email-server` marketplace, inspect the install inventory, and explicitly install `mcp-email-server`. Verify that the source resolves to the repository above and that the plugin contributes the `safe-email-operations` skill plus one local stdio MCP server from `./.mcp.json`.

To refresh or remove the source:

```text
codex plugin marketplace upgrade mcp-email-server
codex plugin marketplace remove mcp-email-server
```

Use the Plugins Directory to uninstall the plugin before removing its source when the host does not do so automatically. Review changed plugin content and metadata before updating.

## Claude Code

Inside Claude Code, add the Git marketplace, install the plugin, and inspect it:

```text
/plugin marketplace add https://github.com/Wh1isper/mcp-email-server.git
/plugin install mcp-email-server@mcp-email-server
/plugin list
```

Verify the repository source, the `safe-email-operations` skill, and the bundled local stdio MCP server in the plugin details. Apply an explicitly reviewed update with `/plugin marketplace update mcp-email-server`, then reload plugins. Remove both plugin and source with:

```text
/plugin uninstall mcp-email-server@mcp-email-server
/plugin marketplace remove mcp-email-server
```

Never put repository credentials in these source URLs. Installation, update, enablement, and removal are explicit user actions; the skill must not perform them silently.
