# mcp-email-server

[![Release](https://img.shields.io/github/v/release/ai-zerolab/mcp-email-server)](https://img.shields.io/github/v/release/ai-zerolab/mcp-email-server)
[![Build status](https://img.shields.io/github/actions/workflow/status/ai-zerolab/mcp-email-server/main.yml?branch=main)](https://github.com/ai-zerolab/mcp-email-server/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/ai-zerolab/mcp-email-server/branch/main/graph/badge.svg)](https://codecov.io/gh/ai-zerolab/mcp-email-server)
[![Commit activity](https://img.shields.io/github/commit-activity/m/ai-zerolab/mcp-email-server)](https://img.shields.io/github/commit-activity/m/ai-zerolab/mcp-email-server)
[![License](https://img.shields.io/github/license/ai-zerolab/mcp-email-server)](https://img.shields.io/github/license/ai-zerolab/mcp-email-server)
[![smithery badge](https://smithery.ai/badge/@ai-zerolab/mcp-email-server)](https://smithery.ai/server/@ai-zerolab/mcp-email-server)

IMAP and SMTP via MCP Server

- **Github repository**: <https://github.com/ai-zerolab/mcp-email-server/>
- **Documentation** <https://ai-zerolab.github.io/mcp-email-server/>

## Installation

### Installing via Smithery

To install Email Server for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@ai-zerolab/mcp-email-server):

```bash
npx -y @smithery/cli install @ai-zerolab/mcp-email-server --client claude
```

### Manual Installation

We recommend using [uv](https://github.com/ai-zerolab/uv) to manage your environment.

Try `uvx mcp-email-server ui` to config, and use following configuration for mcp client:

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "uvx",
      "args": ["mcp-email-server", "stdio"]
    }
  }
}
```

To upgrade, use `uv tool upgrade mcp-email-server`

This package is available on PyPI, so you can install it using `pip install mcp-email-server`

After that, configure your email server using the ui: `mcp-email-server ui`

Then you can try it in [Claude Desktop](https://claude.ai/download). If you want to intergrate it with other mcp client, run `$which mcp-email-server` for the path and configure it in your client like:

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "{{ ENTRYPOINT }}",
      "args": ["stdio"]
    }
  }
}
```

If `docker` is avaliable, you can try use docker image, but you may need to config it in your client using `tools` via `MCP`. The default config path is `~/.config/zerolib/mcp_email_server/config.toml`

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "docker",
      "args": ["run", "-it", "ghcr.io/ai-zerolab/mcp-email-server:latest"]
    }
  }
}
```

## Development

This project is managed using [uv](https://github.com/ai-zerolab/uv).

Try `make install` to install the virtual environment and install the pre-commit hooks.

Use `uv run mcp-email-server` for local development.

## Releasing a new version

- Create an API Token on [PyPI](https://pypi.org/).
- Add the API Token to your projects secrets with the name `PYPI_TOKEN` by visiting [this page](https://github.com/ai-zerolab/mcp-email-server/settings/secrets/actions/new).
- Create a [new release](https://github.com/ai-zerolab/mcp-email-server/releases/new) on Github.
- Create a new tag in the form `*.*.*`.

For more details, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/cicd/#how-to-trigger-a-release).
