import json
import re
import shutil
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "mcp-email-server"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "safe-email-operations"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
CODEX_MARKETPLACE_PATH = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MARKETPLACE_PATH = REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
CLAUDE_MANIFEST_PATH = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
MCP_CONFIG_PATH = PLUGIN_ROOT / ".mcp.json"
PLUGIN_VERSION = json.loads(CODEX_MANIFEST_PATH.read_text(encoding="utf-8"))["version"]
REPOSITORY_URL = "https://github.com/Wh1isper/mcp-email-server"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, raw_frontmatter, body = text.split("---\n", 2)
    metadata = dict(line.split(": ", 1) for line in raw_frontmatter.strip().splitlines())
    return metadata, body


def _plugin_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(PLUGIN_ROOT.rglob("*")) if path.is_file())


def test_official_dual_platform_marketplace_layout_and_sources():
    codex = _read_json(CODEX_MARKETPLACE_PATH)
    claude = _read_json(CLAUDE_MARKETPLACE_PATH)

    assert codex["name"] == "mcp-email-server"
    assert codex["interface"]["displayName"] == "mcp-email-server"
    assert len(codex["plugins"]) == 1
    codex_entry = codex["plugins"][0]
    assert codex_entry["name"] == "mcp-email-server"
    assert codex_entry["source"] == {
        "source": "local",
        "path": "./plugins/mcp-email-server",
    }
    assert codex_entry["policy"]["installation"] == "AVAILABLE"
    assert codex_entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}

    assert claude["$schema"] == "https://json.schemastore.org/claude-code-marketplace.json"
    assert claude["name"] == "mcp-email-server"
    assert claude["version"] == PLUGIN_VERSION
    assert len(claude["plugins"]) == 1
    claude_entry = claude["plugins"][0]
    assert claude_entry["name"] == "mcp-email-server"
    assert claude_entry["source"] == "./plugins/mcp-email-server"
    assert claude_entry["version"] == PLUGIN_VERSION
    assert claude_entry["repository"] == REPOSITORY_URL

    assert (REPOSITORY_ROOT / codex_entry["source"]["path"]).resolve() == PLUGIN_ROOT.resolve()
    assert (REPOSITORY_ROOT / claude_entry["source"]).resolve() == PLUGIN_ROOT.resolve()


def test_manifests_share_one_canonical_skill_directory():
    codex = _read_json(CODEX_MANIFEST_PATH)
    claude = _read_json(CLAUDE_MANIFEST_PATH)

    for manifest in (codex, claude):
        assert manifest["name"] == "mcp-email-server"
        assert manifest["version"] == PLUGIN_VERSION
        assert manifest["repository"] == REPOSITORY_URL
        assert manifest["license"] == "MIT"
        assert manifest["skills"] == "./skills/"
        assert manifest["mcpServers"] == "./.mcp.json"

    assert claude["$schema"] == "https://json.schemastore.org/claude-code-plugin-manifest.json"
    assert list(PLUGIN_ROOT.glob("skills/*/SKILL.md")) == [SKILL_PATH]
    assert SKILL_PATH.is_file()
    assert not SKILL_PATH.is_symlink()
    assert not list(REPOSITORY_ROOT.glob(".agents/**/SKILL.md"))
    assert not list(REPOSITORY_ROOT.glob(".claude-plugin/**/SKILL.md"))


def test_both_install_fixtures_load_identical_canonical_content(tmp_path):
    installed = tmp_path / "mcp-email-server"
    shutil.copytree(PLUGIN_ROOT, installed)

    resolved_skills = []
    for manifest_name in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        manifest = _read_json(installed / manifest_name)
        skills_root = (installed / manifest["skills"]).resolve()
        skills = list(skills_root.glob("*/SKILL.md"))
        assert len(skills) == 1
        resolved_skills.append(skills[0])

    assert resolved_skills[0] == resolved_skills[1]
    assert resolved_skills[0].read_bytes() == SKILL_PATH.read_bytes()
    assert (installed / ".mcp.json").read_bytes() == MCP_CONFIG_PATH.read_bytes()


def test_skill_has_standard_frontmatter_and_minimal_references():
    metadata, body = _frontmatter(SKILL_PATH)

    assert metadata == {
        "name": "safe-email-operations",
        "description": (
            "Use email through the bundled mcp-email-server MCP server, diagnose bounded non-secret state, "
            "and hand account or credential setup to a user-operated CLI or authenticated local UI."
        ),
    }
    assert "# Safe Email Operations" in body
    references = sorted((SKILL_ROOT / "references").glob("*.md"))
    assert [path.name for path in references] == ["installation.md", "safe-commands.md"]
    for reference in references:
        assert f"references/{reference.name}" in body


def test_plugin_has_only_the_reviewed_skill_and_bundled_mcp_surface():
    expected_files = {
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "skills/safe-email-operations/SKILL.md",
        "skills/safe-email-operations/references/installation.md",
        "skills/safe-email-operations/references/safe-commands.md",
    }
    actual_files = {path.relative_to(PLUGIN_ROOT).as_posix() for path in PLUGIN_ROOT.rglob("*") if path.is_file()}
    assert actual_files == expected_files

    forbidden_component_keys = {
        "agents",
        "apps",
        "commands",
        "dependencies",
        "hooks",
        "lspServers",
        "monitors",
        "scripts",
        "userConfig",
    }
    for manifest_path in (CODEX_MANIFEST_PATH, CLAUDE_MANIFEST_PATH):
        manifest = _read_json(manifest_path)
        assert forbidden_component_keys.isdisjoint(manifest)
        assert manifest["mcpServers"] == "./.mcp.json"

    assert _read_json(MCP_CONFIG_PATH) == {
        "mcpServers": {
            "mcp-email-server": {
                "type": "stdio",
                "command": "uvx",
                "args": ["--from", "mcp-email-server@latest", "mcp-email-server-plugin"],
            }
        }
    }
    forbidden_names = {".app.json", "hooks.json", "monitors.json"}
    assert not any(path.name in forbidden_names for path in PLUGIN_ROOT.rglob("*"))
    assert not any(path.suffix in {".bash", ".exe", ".js", ".ps1", ".py", ".sh"} for path in PLUGIN_ROOT.rglob("*"))


def test_static_security_scan_rejects_secret_forwarding_and_remote_execution_patterns():
    text = _plugin_text()
    lowered = text.lower()

    forbidden_patterns = (
        r"add_email_account",
        r"curl\s+[^\n|]+\|\s*(?:ba)?sh",
        r"wget\s+[^\n|]+\|\s*(?:ba)?sh",
        r"(?:password|token|secret)\s*=\s*[^\s]",
        r"export\s+\w*(?:password|token|secret)\w*\s*=",
        r"https?://[^\s/@]+:[^\s/@]+@",
        r"0\.0\.0\.0",
    )
    for pattern in forbidden_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None

    assert "never ask for, receive, repeat, relay, transform, redact, retain" in lowered
    assert "never put one in chat, an mcp call, shell arguments, environment variables" in lowered
    assert "do not launch `uvx mcp-email-server@latest ui`" in lowered
    assert "must not execute the command" in lowered
    assert "bootstrap url" in lowered
    assert "host approval and mcp elicitation do not make" in lowered


def test_only_bounded_non_secret_agent_checks_are_allowed():
    commands = (SKILL_ROOT / "references" / "safe-commands.md").read_text(encoding="utf-8")
    agent_section, user_section = commands.split("## User-operated management", 1)

    assert "uvx mcp-email-server@latest --version" in agent_section
    assert "uvx mcp-email-server@latest config status --json" in agent_section
    assert "uvx mcp-email-server@latest config doctor --json" in agent_section
    assert "mcp-email-server ui" not in agent_section
    assert "account add" not in agent_section
    assert "account set-secret" not in agent_section
    assert "JSON support on another CLI command does not authorize" in agent_section
    assert "uvx mcp-email-server@latest ui" in user_section
    assert "agent must not execute" in user_section.lower()
    assert "environment dumps" in commands
    assert "configuration/database reads" in commands
    assert "keyring queries" in commands
    assert "mcp calls" in commands.lower()


def test_account_and_secret_scenarios_always_handoff_to_the_user():
    skill = SKILL_PATH.read_text(encoding="utf-8").lower()

    assert "“add my account.”" in skill
    assert "“rotate my password.”" in skill
    assert "“paste this app password.”" in skill
    assert "not to paste it into chat" in skill
    assert "recommend revoking it and creating a replacement" in skill
    assert "do not run it for them" in skill
    assert "user-controlled terminal or browser" in skill
    assert "setup cannot be completed safely" in skill
    assert "do not fall back to chat, mcp, argv, environment, or file-based secret transfer" in skill


def test_install_update_remove_and_independent_version_lifecycles_are_documented_for_both_hosts():
    install = (SKILL_ROOT / "references" / "installation.md").read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert REPOSITORY_URL in install
    assert "Plugin and Python application releases are independent" in install
    assert "mcp-email-server@latest" in install
    assert "Codex" in install
    assert "codex plugin marketplace add" in install
    assert "codex plugin marketplace upgrade" in install
    assert "codex plugin marketplace remove" in install
    assert "Claude Code" in install
    assert "/plugin marketplace add" in install
    assert "/plugin install mcp-email-server@mcp-email-server" in install
    assert "/plugin uninstall mcp-email-server@mcp-email-server" in install
    assert "/plugin marketplace remove mcp-email-server" in install
    assert "Do not curl and execute" in install
    assert "Plugin and application releases are independent" in skill
    assert "plugin manifest version identifies the running application" in skill


def test_plugin_version_is_consistent_but_independent_from_the_application():
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    claude_marketplace = _read_json(CLAUDE_MARKETPLACE_PATH)
    manifests = (_read_json(CODEX_MANIFEST_PATH), _read_json(CLAUDE_MANIFEST_PATH))

    assert project["urls"]["Repository"].casefold() == REPOSITORY_URL.casefold()
    assert project["scripts"]["mcp-email-server-plugin"] == "mcp_email_server.cli:plugin_stdio"
    assert claude_marketplace["version"] == PLUGIN_VERSION
    assert claude_marketplace["plugins"][0]["version"] == PLUGIN_VERSION
    assert all(manifest["version"] == PLUGIN_VERSION for manifest in manifests)
    assert all(manifest["repository"].casefold() == REPOSITORY_URL.casefold() for manifest in manifests)
    assert _read_json(MCP_CONFIG_PATH)["mcpServers"]["mcp-email-server"]["args"] == [
        "--from",
        "mcp-email-server@latest",
        "mcp-email-server-plugin",
    ]


def test_skill_guidance_contains_no_release_number():
    markdown = "\n".join(path.read_text(encoding="utf-8") for path in SKILL_ROOT.rglob("*.md"))

    assert re.search(r"(?<![\w.])v?\d+\.\d+\.\d+(?![\w.])", markdown) is None
