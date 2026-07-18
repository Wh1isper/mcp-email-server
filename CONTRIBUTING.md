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
```

Changes to IMAP, SMTP, MCP stdio, configuration loading, attachment handling, or
mailbox mutations should also run the Docker-backed black-box baseline:

```bash
make test-e2e
```

This command requires Docker, starts an isolated GreenMail instance bound only
to loopback, and removes it after the test. See the
[validation guide](https://mcp-email-server.wh1isper.top/validation/) for the
covered flows and limitations.

The CI pipeline runs the unit test suite against every supported Python version
and runs the GreenMail baseline once on Python 3.13 for pull requests and pushes
to `main`. Relevant changes should still run `make test-e2e` locally before they
are pushed so failures can be diagnosed without waiting for CI.

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
4. Create a version tag in the form `X.Y.Z` as part of the release.

The release workflow publishes the package associated with the tagged release.
