# 09. Local Management UI

## Product Role

`mcp-email-server ui` launches the graphical management adapter. It replaces the
legacy Gradio implementation behind the existing command with an embedded React
application and a small loopback ASGI adapter.

The UI is not a mail client, remote administration service, generic RPC API,
MCP App, or daemon. The CLI remains the complete headless management superset
and recovery surface, including provider-connectivity diagnostics intentionally
omitted from the Web UI.

## Functional Scope

The UI MUST provide the managed management plane:

- distinguish durable selected mode/catalog from frozen running mode/catalog,
  show bootstrap revision, restart requirement, effective legacy-source summary,
  and bounded catalog status, including an unavailable-catalog recovery state
  that can select legacy without opening that catalog;
- prepare a usable catalog at a backend-selected private default, selecting it
  immediately for a fresh install while preserving legacy selection until a
  reviewed v1 import succeeds;
- list/show/create/edit managed accounts;
- disable, re-enable, and soft-remove accounts with confirmation;
- set/rotate/remove credentials and perform explicit bounded cleanup;
- view/edit catalog and account policy;
- automatically preview effective TOML/environment legacy import on entry and
  explicitly apply only a changed, conflict-free plan;
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
GET routes have no mutation side effects; import preview capability creation is
also a CSRF-protected POST. After authentication and a status read, the frontend
may issue one initialization POST only when the backend proves a truly empty
installation: no bootstrap file/selection, no effective legacy
account/provider/policy content, and the initial bootstrap revision. The POST
rechecks the effective source, then binds the zero revision and absent-file proof
under the shared supported-POSIX bootstrap/legacy writer lock. Legacy content instead requires the user to choose **Import existing settings**
and review the preview. Preparation does not import, contact a provider, switch
away from legacy runtime, or restart the process. A fully successful reviewed
import automatically selects managed mode when all source account types are
supported; failure or unsupported providers keep legacy selected. Logout destroys the session. Restart
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

Provider connectivity is deliberately absent from the Web UI: there is no
connectivity-test route or control. The shared connectivity service and
`mcp-email-server account test` remain available as low-level CLI diagnostics.
There is also no OpenAPI, Swagger, framework debug route, generic
method-dispatch/RPC, filesystem route, mail route, metrics endpoint, or
unauthenticated operational health endpoint.

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

Every mutable request includes the expected aggregate revision. A request that
mutates or contacts a selected catalog also carries the exact reviewed catalog
identity and bootstrap revision bound to the frontend workspace snapshot that
produced the displayed data; a later status response MUST NOT retarget an existing
snapshot. A changed catalog identity remounts and reloads catalog-dependent
workspace state. The application binds and rechecks that target for the full use
case. Catalog selection carries the bootstrap revision
separately from the optional target catalog revision. A stale revision or changed
selection returns HTTP conflict with a bounded,
non-secret current summary. The frontend
shows the conflict and requires review; it does not automatically replay the
mutation. Destructive or externally meaningful actions show precise
confirmation and the account/catalog affected.

Web and application failures map only to a bounded set of stable error
categories and fixed safe messages. JSON contains no traceback, SQL, secret,
locator, raw provider response, exception text, message content, or unintended
path.

Management access logging is a safe metadata contract, not general HTTP request
logging. Each completed request records only a fixed operation identifier, a
bounded allowlisted method, status, and duration. Route resolution prefers an
exact path-and-method match over an earlier path-only method mismatch. It never records route or path
parameters, a route template, raw path, URL or query, request/response body,
account/email/filesystem value, session/CSRF/bootstrap token, secret, or exception
text.

## Frontend Design and Accessibility

The frontend is React + TypeScript built with Vite. Its default information
architecture is account-first and has exactly two primary destinations:

- **Email accounts** lists accounts and owns create/edit, pause/enable, soft
  removal, and each account's **Password** workflow;
- **Settings & help** presents importing earlier settings, sending/attachment
  safety, account checks, and email-search troubleshooting as progressively
  disclosed optional sections without exposing implementation vocabulary.

Setup/status remains persistent context rather than a peer destination. It uses
task language for import readiness, **Use these accounts** only when an explicit
mode choice remains necessary, and restart while mapping modes, account states,
revisions, binding states, problem codes, and conflict summaries to user
outcomes. Empty-workspace and settled-ready banners with no next action are
hidden rather than occupying permanent attention; actionable setup, conflict,
or restart states remain visible. Paths and support versions remain under
**Setup details**. Raw terms such as `legacy`, `managed`, `catalog`, `bootstrap`,
`lifecycle`, `revision`, `binding`, and `metadata index` MUST NOT appear in the
ordinary rendered workflow unless they are part of user-owned data or a file
path. There is no catalog activation mutation: a structurally complete enabled
account is usable as soon as its save succeeds. A fully successful reviewed
legacy import with no unsupported provider type compare-and-swap selects managed
mode automatically; an import failure or unsupported provider leaves legacy
mode selected and preserves an explicit user choice. Conflicts MUST NOT be
auto-replayed. Catalog-dependent content remains unmounted until the selected
catalog is usable. Opening one optional settings disclosure mounts only that
section, so closed sections neither fetch nor mutate state.

The add-account form is email/password-first. It derives an editable sender name
and account nickname, uses a finite local preset table for common email domains,
and otherwise suggests `imap.<domain>` and `smtp.<domain>`. There is no network
discovery and no redundant read-only connection preview. A suggestion continues
changing only while its field remains untouched; editing existing account
identity MUST NOT silently rename its nickname or sender name. Advanced account
settings (server, login, port, TLS/STARTTLS/plain selection, certificate
verification, and Sent-folder behavior) are folded behind progressive
disclosure, as is optional outgoing mail. Changing an untouched transport
default updates its standard port, but never overwrites a manually chosen port.
After creation, credential rotation and removal stay in that account's
**Password** context rather than a separate global credential workspace. Because
bounded cleanup is catalog-wide and cleanup rows can outlive a removed account,
**Email accounts** shows its cleanup action only when the doctor summary reports
inactive password data; the action remains reachable when no active account
remains and never relies on the selected account's projected binding state.
Account creation displays typed credential outcomes and directs cleanup instead
of showing ordinary success when superseded data remains. A failed save displays
an error, preserves the prior password authority, and offers only a fresh save
after the reported problem is corrected. No-op import plans require no confirmation. Changed plans use compact account
cards with technical source and
version details folded; the user checks an explicit review box before the adapter
submits the fixed `IMPORT` confirmation value.

Visual styling uses a native UI sans-serif stack, compact but readable type
scale, at least 44-pixel primary form controls, neutral black/white/gray hierarchy,
restrained semantic attention colors from a pinned maintained color-token library,
and locally packaged text-accompanied icons from a pinned maintained icon library.
Desktop and mobile layouts preserve clear label/input rhythm, visible focus,
and touch-sized actions. Critical
actions MUST NOT rely on icon-only or color-only meaning. No runtime CDN or
remote asset is permitted.

Allowed-recipient and allowed-sender policy values are presented as individual
items with add, edit, and remove actions. The empty states are explicit and must
not be conflated: no allowed recipients disables sending, while no allowed
senders means reading is unrestricted by sender.

Secret input state exists only in the active account editor or selected account
**Password** component. It is cleared after every success, failure, or conflict;
when optional SMTP is disabled; when account or credential role changes; and on
component unmount. It does not enter application-wide stores, URL state,
persisted browser storage, clipboard, or dev-oriented logging.

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
4. CLI and UI exercise the same management services for catalog, account,
   credential, cleanup, policy, import, doctor, health, and revision conflict
   flows;
   route inventory proves provider connectivity is not exposed by the Web UI.
5. Browser E2E covers empty-install automatic account-storage preparation,
   explicit earlier-settings import preparation and automatic successful cutover,
   two-destination keyboard navigation,
   reload with session, stale/replayed bootstrap, email-and-password-first account
   create with editable suggestions and no connection preview, folded advanced
   and optional outgoing settings, edit/enable/disable and soft removal, per-account
   **Password** rotation and failed-save unchanged-authority behavior,
   individual policy-item editing with both empty
   semantics, hidden settled-ready status, checkbox-confirmed import
   preview/apply, conflict review, secret clearing, logout, and process shutdown.
6. Route inventory proves no mail workflow, generic RPC, debug/OpenAPI, remote
   binding, MCP App, or filesystem-browser surface.
7. Frontend lint/typecheck/unit/build pass from the lockfile and generated assets
   are reproducible/stale-free.
8. Wheel, sdist, wheel rebuilt from sdist without Node, isolated install, and
   local `uvx --no-open --port 0` under a PTY all serve a functioning
   authenticated UI with identical required asset hashes.
