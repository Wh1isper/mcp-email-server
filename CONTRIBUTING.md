# Contributing to `mcp-email-server`

Contributions are welcome, and they are greatly appreciated!
Every little bit helps, and credit will always be given.

You can contribute in many ways:

# Types of Contributions

## Report Bugs

Report bugs at https://github.com/wh1isper/mcp-email-server/issues

If you are reporting a bug, please include:

- Your operating system name and version.
- Any details about your local setup that might be helpful in troubleshooting.
- Detailed steps to reproduce the bug.

## Fix Bugs

Look through the GitHub issues for bugs.
Anything tagged with "bug" and "help wanted" is open to whoever wants to implement a fix for it.

## Implement Features

Look through the GitHub issues for features.
Anything tagged with "enhancement" and "help wanted" is open to whoever wants to implement it.

## Write Documentation

mcp-email-server could always use more documentation, whether as part of the official docs, in docstrings, or even on the web in blog posts, articles, and such.

## Submit Feedback

The best way to send feedback is to file an issue at https://github.com/wh1isper/mcp-email-server/issues.

If you are proposing a new feature:

- Explain in detail how it would work.
- Keep the scope as narrow as possible, to make it easier to implement.
- Remember that this is a volunteer-driven project, and that contributions
  are welcome :)

# Get Started!

Ready to contribute? Here's how to set up `mcp-email-server` for local development.
Please note this documentation assumes you already have `uv` and `Git` installed and ready to go.

1. Fork the `mcp-email-server` repo on GitHub.

2. Clone your fork locally:

```bash
cd <directory_in_which_repo_should_be_created>
git clone git@github.com:YOUR_NAME/mcp-email-server.git
```

3. Now we need to install the environment. Navigate into the directory

```bash
cd mcp-email-server
```

Then, install and activate the environment with:

```bash
uv sync
```

4. Install pre-commit to run linters/formatters at commit time:

```bash
uv run pre-commit install
```

5. Create a branch for local development:

```bash
git checkout -b name-of-your-bugfix-or-feature
```

Now you can make your changes locally.

6. Don't forget to add test cases for your added functionality to the `tests` directory.

7. When you're done making changes, run the formatting, linting, type, lockfile, and dependency checks.

```bash
make check
```

8. Validate that all unit tests and documentation checks are passing:

```bash
make test
make docs-test
make test-browser
```

`make test` combines coverage from the main pytest process and Python
subprocesses, then enforces the 80% aggregate project baseline. Codecov also
requires 80% coverage for changed lines. A focused diagnostic run may use
`--cov-fail-under=0`, but the complete suite must pass the configured threshold
before submission. Add tests for meaningful behavior and failure or security
boundaries rather than percentage-only execution.

Frontend changes require Node 22.12 or later. Rebuild and stage the locked React
assets before the Python checks:

```bash
make frontend
```

`make frontend` runs `npm ci`, lint, type checking, unit tests, and the Vite
production build, then replaces the embedded assets after checking for missing,
stale, remote, source-map, service-worker, and MCP-App files. It also refreshes
`frontend/embedded-assets.json`, which binds the maintainer source tree and build
script to exact staged asset hashes. Normal PEP 517 wheel/sdist builds run only
`make frontend-check`; rebuilding a wheel from the sdist and UI startup never
invoke Node or access npm. Commit frontend source, `package-lock.json`, the asset
manifest, third-party notices, and the corresponding staged
`mcp_email_server/web_ui/static` files together.

Changes to IMAP, SMTP, MCP stdio, configuration loading, attachment handling, or
mailbox mutations should also run the Docker-backed black-box baseline:

```bash
make test-e2e
```

This command requires Docker, starts an isolated GreenMail instance bound only
to loopback, and removes it after the test. See the
[validation guide](https://mcp-email-server.wh1isper.top/validation/) for the
covered flows and limitations.

The CI pipeline runs quality and strict documentation checks, the unit test
suite against every supported Python version, the locked frontend and
real-browser management E2E, and the GreenMail baseline once for pull requests
and pushes to `main`. It also builds one release-format wheel/sdist pair and runs
`make verify-dist` against those exact bytes, including the Node-free from-sdist
rebuild and installed/`uvx` UI smokes. Relevant changes should still run
`make test-browser` and `make test-e2e` locally before they are pushed so
failures can be diagnosed without waiting for CI.

9. Commit your changes and push your branch to GitHub:

```bash
git add .
git commit -m "Your detailed description of your changes."
git push origin name-of-your-bugfix-or-feature
```

10. Submit a pull request through the GitHub website.

# Pull Request Guidelines

Before you submit a pull request, check that it meets these guidelines:

1. The pull request should include tests.

2. If the pull request adds or changes user-facing functionality, update the relevant page in `docs/`.
   Keep `README.md` focused on the quick-start path.

# Releasing a New Version

This section is for project maintainers.

1. Create an API token on [PyPI](https://pypi.org/).
2. Add it to the repository's GitHub Actions secrets as `PYPI_TOKEN`.
3. Create a [GitHub release](https://github.com/wh1isper/mcp-email-server/releases/new).
4. Create a version tag in the form `vX.Y.Z` as part of the release.

Commit the intended package, plugin, marketplace, installation-guide, and lock
versions before creating the tag; `vX.Y.Z` must match the committed Python
package version. The release workflow does not rewrite version
metadata or the lockfile: it pins and verifies the peeled tag commit, then reruns
the complete Python 3.11-3.14 matrix against that exact tree. A separate
unprivileged validation job rebuilds the locked frontend, rejects staged-asset
drift, runs the default-Python, documentation, browser, packaging, and GreenMail
gates, builds the final wheel and sdist once, and records their checksums. The
credential-bearing publish job can only download, checksum, and publish those
unchanged verified artifacts; it contains no build step.
