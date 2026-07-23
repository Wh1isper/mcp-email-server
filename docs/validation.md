# Validation

The repository includes a Docker-backed black-box baseline for the current
application. It verifies that the installed MCP stdio server can communicate
with real SMTP and IMAP sockets before architecture changes are accepted.

## Run the baseline

Requirements:

- Docker Engine with Docker Compose v2.
- The development environment installed with `uv sync` or `make install`.

Run:

```bash
make test-e2e
```

The command creates a unique Compose project, publishes SMTP and IMAP on
dynamic loopback ports, waits by performing authenticated connections, runs the
E2E test, and removes only that run's container and network even when the test
fails. Concurrent runs and separate worktrees do not share lifecycle ownership.
Each MCP request has a 15-second response deadline so a live but unresponsive
stdio subprocess fails the test instead of hanging indefinitely. The run does
not modify the normal user configuration.

The regular test suite excludes tests marked `e2e` and remains independent of
Docker:

```bash
make test
```

## Local UI and package validation

Changes to the management UI run three layers in the regular suite:

1. React lint, TypeScript checking, unit tests, and deterministic Vite staging
   through `make frontend`; the checked-in `frontend/embedded-assets.json` binds
   the frontend sources and staging script to exact packaged asset hashes so
   `make frontend-check` also works without Node or `frontend/dist`;
2. backend route tests for one-time bootstrap, replay/expiry/concurrency/rate
   limiting, exact Host/Origin, CSRF, Fetch Metadata, JSON/body bounds, strict
   headers, logout/shutdown, explicit management DTOs, revision summaries, and
   absence of mail/generic routes;
3. distribution tests that inventory wheel and sdist assets, rebuild a wheel
   from the sdist with failing `node`/`npm` shims, compare static hashes, install
   the wheel into an isolated environment, and serve the authenticated UI on an
   ephemeral loopback port.

Critical browser flows are exercised with locked Playwright/Chromium against a
real CLI process launched under a PTY: fragment removal and bootstrap exchange,
cookie-session reload, stale-link replay, staging initialization, account
creation/lifecycle, credential rotation and conflict review, import preview/apply,
keyboard operation, secret-state clearing, logout, and process shutdown. Run:

```bash
cd frontend && npx playwright install chromium
make test-browser
```

The browser gate runs on a POSIX host and requires the `script` PTY utility
(`util-linux` on the CI runner). The test uses only synthetic credentials, a
temporary file-backed test keyring,
and private temporary catalog paths. Screenshots, video, and traces are disabled
so one-time launch material cannot enter browser artifacts.

## Continuous integration

The main GitHub Actions workflow runs the locked frontend build and rejects
staged-asset drift, installs locked Chromium, runs `make test-browser`, and runs
`make test-e2e` once on `ubuntu-latest` for every pull request and push to
`main`. The GreenMail job has a 10-minute outer
timeout in addition to the per-request MCP deadline. The regular Python
3.11-3.14 test matrix continues to exclude the `e2e` marker, so GreenMail is not
repeated for every interpreter version. The release workflow repeats the
frontend, static, Python, docs, browser, package, and GreenMail gates. It then
builds `dist/`, runs `make verify-dist` against those exact wheel/sdist bytes
(including authenticated isolated-install and local-wheel `uvx` UI smokes), and
publishes only that unchanged directory.

Run the baseline locally before pushing relevant mail or stdio changes; the
shared CI check is not a replacement for local diagnosis.

## Tested boundary

```mermaid
graph LR
    CLIENT[MCP ClientSession]
    STDIO[mcp-email-server stdio subprocess]
    CONFIG[Temporary TOML configuration]
    SMTP[GreenMail SMTP]
    IMAP[GreenMail IMAP]
    OBSERVER[Independent smtplib and imaplib observer]

    CLIENT -->|initialize, list_tools, call_tool| STDIO
    CONFIG --> STDIO
    STDIO -->|SMTP AUTH and delivery| SMTP
    STDIO -->|IMAP LOGIN and commands| IMAP
    SMTP --> IMAP
    OBSERVER -->|seed and independently verify| SMTP
    OBSERVER -->|inspect MIME, flags, folders, and bytes| IMAP
```

The test starts the installed `mcp-email-server` console script as a child
process rather than importing tool functions. The subprocess loads a temporary
plaintext TOML file containing only synthetic test credentials. Python's
standard-library `smtplib`, `imaplib`, and MIME parser act as an independent
seeder and observer, so the system is not solely verifying itself.

## Coverage

| Area          | Assertions                                                                                             |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| MCP lifecycle | stdio subprocess starts, `initialize` succeeds, and expected tools are visible                         |
| Configuration | legacy TOML plus managed CLI staging, activation, explicit selection, and process restart              |
| Managed mode  | a keyring-bound managed account reaches live IMAP; a missing selected database fails without fallback  |
| SMTP          | authenticated Alice-to-Bob delivery succeeds through `send_email`                                      |
| IMAP read     | Bob can list paged metadata with exact totals and retrieve full content by UID                         |
| Attachments   | source bytes arrive in Bob's MIME message, appear in full content, download to disk, and match exactly |
| Sent copy     | Alice receives the application-created copy in `Sent`                                                  |
| Flags         | `mark_emails_as_read` produces `\\Seen`; saved drafts have `\\Draft` and `\\Seen`                      |
| Mailboxes     | the observer provisions `Sent`, `Drafts`, and `Archive`; MCP discovers and uses them                   |
| Index         | initial projection, qualified SQLite reuse, filter fallback, bounds, and restart reuse are verified    |
| Mutations     | explicit move, automatic archive selection, draft save, and delete are observed in IMAP                |

`list_emails_metadata` currently fetches headers only and therefore returns an
empty attachment list. Attachment names are verified through
`get_emails_content`, which fetches and parses the complete MIME message. The
baseline records this existing contract rather than silently changing it.
The E2E suite also inspects only non-secret projection state to verify that a
complete five-message mailbox is persisted once, reused for a second page, and
reused after restarting the packaged stdio process. Subject, address, flag,
body, text, and attachment-filter requests exercise the application-owned IMAP
fallback with exact totals, and an oversized page is rejected at the MCP schema
boundary.

## Isolation and security

The Compose definition:

- pins GreenMail 2.1.11 by both tag and image digest;
- exposes only SMTP and IMAP on dynamically assigned loopback ports;
- gives each invocation a unique Compose project so concurrent cleanup cannot affect another run;
- uses only `example.test` addresses and fixed synthetic passwords;
- disables implicit TLS only inside this local test boundary; and
- never forwards messages to external mail servers.

The application configuration lives in a pytest-managed temporary directory
and contains only the fixed synthetic credentials above. Managed subprocess
coverage injects a test-only, process-persistent keyring backend whose file is
also confined to that directory; production code has no plaintext managed
fallback. Do not replace the synthetic accounts with real credentials or
personal message data.

## Why GreenMail

[GreenMail](https://greenmail-mail-test.github.io/greenmail/) is designed as a
sandbox mail server for integration tests and provides SMTP and IMAP with a
small, deterministic setup. The repository uses the official
[`greenmail/standalone`](https://hub.docker.com/r/greenmail/standalone) image.

GreenMail is a compatibility baseline, not proof against every provider. This
baseline also does not cover implicit TLS or STARTTLS; those paths retain their
focused unit tests until a dedicated certificate-backed integration service is
added. A future nightly matrix can add a production-oriented server such as
[Stalwart](https://stalw.art/docs/install/platform/docker/) and targeted canary
accounts for provider-specific behavior. Real-provider canaries must use
separate test accounts, minimal retention, and credentials supplied outside the
repository.

## Troubleshooting

The runner prints its unique Compose project name and assigned ports. If a run
is interrupted before cleanup completes, list matching projects with:

```bash
docker compose ls --all | grep mcp-email-server-e2e
```

Use the printed project name to inspect logs or remove only that run:

```bash
docker compose \
  --project-name <printed-project-name> \
  --file dev/greenmail/compose.yml \
  logs greenmail

docker compose \
  --project-name <printed-project-name> \
  --file dev/greenmail/compose.yml \
  down --volumes --remove-orphans
```
