# 09. Local Management UI

## Product Role

`mcp-email-server ui` launches the graphical management adapter. It replaces the
legacy Gradio implementation behind the existing command with an embedded React
application and a small loopback ASGI adapter.

The UI is not a mail client, remote administration service, generic RPC API,
MCP App, or daemon. The CLI remains a complete headless equivalent and recovery
surface.

## Functional Scope

The UI MUST provide the managed management plane:

- inspect current mode, bootstrap revision, selected catalog, restart
  requirement, and bounded catalog status, including an unavailable-catalog
  recovery state that can select legacy without opening that catalog;
- initialize a staging catalog, validate/activate it, and explicitly select it;
- list/show/create/edit managed accounts;
- disable, re-enable, and soft-remove accounts with confirmation;
- set/rotate/remove credentials and perform explicit cleanup/repair;
- view/edit catalog and account policy;
- test IMAP and SMTP connectivity separately;
- preview and explicitly apply legacy import;
- run bounded doctor and index-health checks;
- display revision conflicts and current non-secret summaries for user review.

In legacy mode it may inspect bounded status and guide/initiate migration. It
MUST NOT remain a general legacy TOML editor.

It MUST NOT browse/read email, render HTML email, compose/send, save, move,
archive, delete, or expose arbitrary filesystem browsing.

## Command and Process Lifecycle

Canonical command:

```text
mcp-email-server ui [--no-open] [--port PORT]
```

- Bind host is exactly `127.0.0.1`; no `--host`, wildcard, IPv6-any, or `--share`
  option exists.
- Default port is `0` (OS-assigned ephemeral). A fixed valid port may be chosen
  explicitly.
- The process freezes bootstrap mode and selected catalog before constructing UI
  state or binding its listener. Freezing does not open the catalog, so a missing
  or corrupt selected catalog remains recoverable through explicit legacy
  selection; the resulting durable change reports that restart is required.
- The process runs in the foreground and prints a bounded local launch message.
- Unless `--no-open` is set, it opens one bootstrap URL in the default browser.
- With `--no-open`, or if browser launch reports failure, the one-time URL is
  written only to an attached stdout/stderr TTY. If neither stream is a TTY, the
  process fails before serving rather than leaking the token to a pipe or log.
- SIGINT/SIGTERM, startup failure, or normal exit closes the server, invalidates
  all sessions/tokens, and releases packaged-resource contexts.
- No environment variable or framework default may enable external binding,
  sharing, debug mode, reload, or additional routes.

## One-time Bootstrap

At startup the process generates a high-entropy token and puts it only in the
URL fragment, for example:

```text
http://127.0.0.1:<port>/<process-route>/#bootstrap=<token>
```

Fragments are not sent in HTTP requests. The frontend reads the fragment,
immediately removes it with `history.replaceState`, and exchanges it in an
`Authorization` header against the process-unique bootstrap endpoint.

The server compares tokens in constant time and atomically consumes the token on
first success. It never accepts the token in query parameters, path segments,
cookies, referrers, or logs. Replay, malformed, missing, expired, and excessive
attempts fail with a uniform bounded response. Authentication failures are
rate-limited without revealing token validity by timing or detail.

## Session and CSRF

Successful exchange creates:

- a random process-local session stored only in memory;
- a process-unique cookie name and narrow process route path;
- an `HttpOnly`, `SameSite=Strict` cookie (and `Secure` whenever transport is
  HTTPS; loopback HTTP is the supported default);
- a separate random CSRF value returned to authenticated frontend memory.

Every state-changing request requires the session cookie, exact CSRF header,
JSON content type, accepted Fetch Metadata where available, exact Host, and
allowed Origin. Missing/`null`/foreign Origin is rejected for mutation routes.
GET routes have no mutation side effects. Logout destroys the session. Restart
destroys all sessions and makes stale tabs unauthorized.

CORS is disabled: no permissive `Access-Control-Allow-Origin`, credentials, or
preflight policy is emitted. Redirects are not used for authentication failures.

## Host, Origin, and Route Surface

Accepted Host values correspond exactly to the bound `127.0.0.1` address and
actual port. DNS names and alternative loopback spellings are rejected. Allowed
Origin is the exact startup origin.

The backend exposes only:

- static application resources under the process route;
- bootstrap exchange, session status/logout, and CSRF-protected management
  endpoints required by the UI;
- a bounded authenticated health/status endpoint if needed by lifecycle tests.

There is no OpenAPI, Swagger, framework debug route, generic method-dispatch/RPC,
filesystem route, mail route, metrics endpoint, or unauthenticated operational
health endpoint.

## Response Security Policy

Every HTML, asset, JSON, success, and error response uses `Cache-Control:
no-store` and an appropriate strict policy including:

- `Content-Security-Policy` permitting only packaged same-origin assets and
  disallowing framing, plugins, base changes, and form submission;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: no-referrer`;
- `X-Frame-Options: DENY` or equivalent CSP `frame-ancestors 'none'`;
- restrictive `Permissions-Policy`;
- no server/debug version disclosure.

The application includes no CDN script, remote font, analytics, telemetry,
service worker, runtime asset download, or inline executable code that requires
weakening CSP.

## Management API Design

Routes are explicit use-case adapters, not CRUD exposure of database rows.
Requests and responses have bounded typed schemas. Secret values appear only in
a protected mutation request body, are passed once to the application service,
and are never echoed.

Every mutable request includes the expected aggregate revision. Catalog
selection carries the bootstrap revision separately from the optional target
catalog revision. A stale revision returns HTTP conflict with a bounded,
non-secret current summary. The frontend
shows the conflict and requires review; it does not automatically replay the
mutation. Destructive or externally meaningful actions show precise
confirmation and the account/catalog affected.

Application errors map to stable categories and safe remediation. JSON contains
no traceback, SQL, secret, locator, raw provider response, message content, or
unintended path. Request bodies are never logged.

## Frontend Design and Accessibility

The frontend is React + TypeScript built with Vite. It presents management by
domain: setup/status, accounts, credentials/connectivity, policy, migration, and
health. It clears sensitive component state after submission and does not place
secrets in application stores, URL state, persisted browser storage, clipboard,
or dev-oriented logging.

Forms have associated labels, keyboard operation, visible focus, semantic status
and error regions, non-color-only state, and usable responsive layouts. Async
work exposes pending/disabled state without hiding typed outcomes. Provider and
account strings render as text, never unsanitized HTML.

## Packaging

Prebuilt static assets are loaded using `importlib.resources`, not repository
relative paths. Wheel and sdist contain `index.html`, all referenced hashed
assets, and third-party notices. The sdist also contains frontend source and a
committed lockfile for reproducibility, but building a wheel from the published
sdist merely packages prebuilt assets.

Installing the wheel, building a wheel from the sdist, and running the UI or
`uvx` require no Node executable, npm, registry access, CDN, or runtime asset
download. Node is a maintainer/release build dependency only. Build staging
cleans old hashed assets and verifies no missing, stale/unreferenced, unexpected
MCP-App, or unintended source-map files enter artifacts.

## Acceptance Criteria

1. CLI tests prove only `--no-open` and `--port` are exposed, bind is exact
   `127.0.0.1`, default port is ephemeral, no share/debug/daemon path exists,
   and manual token handoff requires an attached TTY.
2. Backend security tests cover bootstrap success, replay, expiry, concurrency,
   rate limiting, timing-safe comparison, process-unique routes/cookies,
   logout/restart, exact Host/Origin, CSRF, Fetch Metadata, JSON-only mutations,
   no CORS, headers, and bounded errors.
3. Sentinel secrets are absent from URLs after exchange, history, HTML, JSON
   responses, logs, browser storage, screenshots/traces, exceptions, and assets.
4. CLI and UI exercise the same management services for lifecycle, account,
   credential, policy, connectivity, import, doctor, health, and revision
   conflict flows.
5. Browser E2E covers first launch, reload with session, stale/replayed bootstrap,
   account CRUD/lifecycle, credential rotation outcome, import preview/apply,
   conflict review, logout, keyboard/accessibility smoke, and process shutdown.
6. Route inventory proves no mail workflow, generic RPC, debug/OpenAPI, remote
   binding, MCP App, or filesystem-browser surface.
7. Frontend lint/typecheck/unit/build pass from the lockfile and generated assets
   are reproducible/stale-free.
8. Wheel, sdist, wheel rebuilt from sdist without Node, isolated install, and
   local `uvx --no-open --port 0` under a PTY all serve a functioning
   authenticated UI with identical required asset hashes.
