# Installation and provenance

The authoritative source is `https://github.com/Wh1isper/mcp-email-server`. Plugin release `0.0.1` matches application release `0.0.1`. Inspect the repository identity, selected release tag or commit, both marketplace files, both plugin manifests, and the canonical `skills/safe-email-operations/SKILL.md` before installation. Plugin installation does not install or upgrade the Python application.

Prefer a signed release tag when available; otherwise record and review the exact commit selected. Do not curl and execute an installer.

## Codex

Add a pinned repository marketplace with the native CLI:

```text
codex plugin marketplace add https://github.com/Wh1isper/mcp-email-server.git --ref v0.0.1
codex plugin marketplace list
```

In the ChatGPT desktop app, open the Plugins Directory, select the `mcp-email-server` marketplace, inspect the install inventory, and explicitly install `mcp-email-server`. Verify that the source resolves to the repository above, the installed plugin version is `0.0.1`, and the only contributed component is `safe-email-operations` from `./skills/`.

To refresh or remove the source:

```text
codex plugin marketplace upgrade mcp-email-server
codex plugin marketplace remove mcp-email-server
```

Use the Plugins Directory to uninstall the plugin before removing its source when the host does not do so automatically. Review a new tag/commit and its version metadata before updating.

## Claude Code

Inside Claude Code, add the pinned Git marketplace, install the plugin, and inspect it:

```text
/plugin marketplace add https://github.com/Wh1isper/mcp-email-server.git#v0.0.1
/plugin install mcp-email-server@mcp-email-server
/plugin list
```

Verify the repository source, plugin version `0.0.1`, and the single `safe-email-operations` skill in the plugin details view. Apply an explicitly reviewed update with `/plugin marketplace update mcp-email-server`, then reload plugins. Remove both plugin and source with:

```text
/plugin uninstall mcp-email-server@mcp-email-server
/plugin marketplace remove mcp-email-server
```

Never put repository credentials in these source URLs. Installation, update, and removal are explicit user actions; the skill must not perform them silently.
