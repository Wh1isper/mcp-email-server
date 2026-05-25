# PATCH.md — Scher Extensions auf mcp-email-server

Dieser Fork (`hemati/mcp-email-server`) ergänzt den Upstream `ai-zerolab/mcp-email-server`
um eine schmale Schicht von Tools, die eine Mail-Triage-Automation benötigt
(IMAP/IONOS-Konto via MCP, Triage-Workflow mit Folder-Verschiebung und Flag-Management).

Diese Datei dokumentiert **alle** Berührungspunkte mit Upstream-Code, damit ein
späterer Upstream-Sync möglich bleibt und damit jeder Patch entweder als
Upstream-PR-Kandidat identifiziert oder als Scher-spezifisch markiert ist.

## Upstream-Inventur (Stand: Branch `main`, Commit `40e7431`)

Im Upstream bereits vorhanden — **nicht neu zu bauen**:

| Tool                      | Status                                                                                                                                            |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_available_accounts` | upstream ✓                                                                                                                                        |
| `add_email_account`       | upstream ✓                                                                                                                                        |
| `list_emails_metadata`    | upstream ✓ (mit `seen`/`flagged`/`answered`/`subject`/`from_address`/`to_address`/`before`/`since`/`mailbox`/Pagination/Order — sehr vollständig) |
| `get_emails_content`      | upstream ✓                                                                                                                                        |
| `send_email`              | upstream ✓ (mit `in_reply_to`, `references`, `attachments`, auto Sent-Folder, `cc`, `bcc`, HTML)                                                  |
| `delete_emails`           | upstream ✓ (Bonus, war nicht im Briefing geplant)                                                                                                 |
| `move_emails`             | upstream ✓ (Commit `40e7431`, mit MOVE/COPY-Fallback und EXPUNGE — exakt wie im Briefing skizziert)                                               |
| `list_mailboxes`          | upstream ✓ (Commit `40e7431`, ersetzt das geplante `list_folders`)                                                                                |
| `download_attachment`     | upstream ✓ (mit `enable_attachment_download` Security-Toggle)                                                                                     |

Upstream-Architektur (relevant für Patches):

- `mcp_email_server/app.py` — `FastMCP` Instanz + alle `@mcp.tool` Definitionen.
- `mcp_email_server/emails/__init__.py` — abstrakte `EmailHandler`-Basisklasse.
- `mcp_email_server/emails/classic.py` — `EmailClient` (führt IMAP/SMTP aus) und `ClassicEmailHandler` (delegiert auf `EmailClient`).
- `mcp_email_server/emails/dispatcher.py` — `dispatch_handler(account_name)` Factory.

Wichtige Constraints:

- Async via `aioimaplib` + `aiosmtplib` — alle neuen IMAP-Operationen müssen async sein.
- IMAP-Befehle laufen über `imap.uid("...", ...)` (UID-basiert) — gleiche Konvention überall.
- Helpers `_quote_mailbox`, `_raise_for_imap_error`, `_imap_status` stehen schon zur Verfügung.

## Scher Extensions — was wir hinzufügen

### 1. Triage-Flags ohne Move

Briefing-Punkte 2/3 (`mark_seen`, `mark_unseen`) — **NEU**.

Notwendig, weil Upstream keinen Weg bietet, eine Mail nur als gelesen zu markieren,
ohne sie zu verschieben. Pfad "ignorieren / unklar" in der Triage braucht das.

- `mark_seen(account_name, email_ids, mailbox="INBOX")` — UID STORE +FLAGS (`\Seen`), kein EXPUNGE.
- `mark_unseen(account_name, email_ids, mailbox="INBOX")` — UID STORE -FLAGS (`\Seen`).
- Returns: `(succeeded_ids, failed_ids)`.

**Upstream-PR-Kandidat:** ja, beides generisch nützlich.

### 2. Idempotentes Folder-Erstellen

Briefing-Punkt 4 (`ensure_folder`) — **NEU**.

Skills müssen Folder wie `INBOX/Anfragen`, `INBOX/Auftraege`, `INBOX/Pending`
sicherstellen, bevor `move_emails` aufgerufen wird.

- `ensure_folder(account_name, folder)` — IMAP `CREATE`, ALREADYEXISTS toleriert, mit `LIST` verifiziert.
- Returns: `{folder, existed: bool, created: bool}`.

**Upstream-PR-Kandidat:** ja.

### 3. Folder auflisten

Briefing-Punkt 5 (`list_folders`) — **gestrichen**, durch `list_mailboxes` upstream abgedeckt.

### 4. Polling-Wrapper

Briefing-Punkt 6 (`poll_unseen`) — **gestrichen**.

`list_emails_metadata(account_name, seen=False, since=X, mailbox="INBOX")` plus
`get_emails_content(account_name, email_ids)` decken den Polling-Pfad bereits ab.
Skills nutzen die Upstream-Tools direkt. Eingespart: ein redundanter Wrapper.

### 5. Diag-Selbsttest

Briefing-Punkt 7 (`diag`) — **NEU**.

Selbsttest pro Account: ENV-Snapshot (Passwörter maskiert), TCP-Connect zu IMAP/SMTP-Host,
IMAP-Login + SELECT INBOX, SMTP-Login. Antwortet mit `{checks: [{name, ok, detail|error}]}`.

**Upstream-PR-Kandidat:** evtl. — wenn Format/Naming generisch genug.

### 6. `send_email` — Custom Message-ID

Briefing-Punkt 8 — **NEU**, Patch in bestehender Funktion.

- Neuer optionaler Kwarg `message_id: str | None = None` an drei Stellen:
  - `app.py::send_email` (MCP-Tool-Signatur)
  - `mcp_email_server/emails/__init__.py::EmailHandler.send_email` (abstrakt)
  - `mcp_email_server/emails/classic.py::ClassicEmailHandler.send_email`
  - `mcp_email_server/emails/classic.py::EmailClient.send_email`
- Helper `_normalize_msgid()` ergänzt `<...>` falls fehlt, strippt Whitespace.
- Default `None` → Verhalten unverändert (`email.utils.make_msgid` auto-generieren).

**Upstream-PR-Kandidat:** ja, klar generisch.

### 7. `send_email` — `MCP_EMAIL_SERVER_REDIRECT_TO`-ENV

Briefing-Punkt 9 — **NEU**, Patch in bestehender Funktion.

- ENV `MCP_EMAIL_SERVER_REDIRECT_TO` setzt envelope-`To` für ALLE ausgehenden Mails auf
  diese Adresse, leert `Cc`, schreibt Original-Empfänger in `X-Original-To` / `X-Original-Cc`.
- Implementiert in `EmailClient.send_email` (lokales `os.getenv`).
- Default ENV nicht gesetzt → Verhalten unverändert.

**Upstream-PR-Kandidat:** wahrscheinlich nein — sehr Scher-spezifisches Sicherheitsnetz
für Test-/Staging-Konfigurationen. Trotzdem klein und sauber, leicht herauspatchbar.

### 8. Move-Tool

Briefing-Punkt 1 (`move_emails`) — **gestrichen**, upstream bereits umgesetzt.

## Berührungspunkte mit Upstream-Code

Stand nach Implementierung der Patches (wird laufend aktualisiert):

| Datei                                 | Änderung                                                                                                                                                                                   | Grund             |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| `mcp_email_server/emails/__init__.py` | abstract `mark_seen`, `mark_unseen`, `ensure_folder`; `send_email`-Signatur um `message_id` erweitert                                                                                      | Handler-Interface |
| `mcp_email_server/emails/classic.py`  | `EmailClient.mark_seen`, `mark_unseen`, `ensure_folder`; `send_email` um `message_id` + `MCP_EMAIL_SERVER_REDIRECT_TO`-Logik erweitert; `ClassicEmailHandler` delegiert die neuen Methoden | Implementation    |
| `mcp_email_server/app.py`             | `send_email`-Tool-Signatur um `message_id` erweitert; eine Zeile `register_scher_tools(mcp)` am Modulende                                                                                  | Tool-Surface      |
| `mcp_email_server/scher_tools.py`     | **neue Datei** mit `mark_seen`, `mark_unseen`, `ensure_folder`, `diag`-Tool-Wrappern + `register_scher_tools()`-Funktion                                                                   | Scher Extensions  |
| `tests/test_scher_tools.py`           | **neue Datei** mit Mock-Tests für alle neuen Tools                                                                                                                                         | Testabdeckung     |
| `tests/test_send_email_extensions.py` | **neue Datei** mit Tests für `message_id` und `REDIRECT_TO`                                                                                                                                | Regression-Schutz |
| `pyproject.toml`                      | `name` → `mcp-email-server-scher`, Entry-Point angepasst                                                                                                                                   | Distribution      |
| `README.md`                           | Neue Sektion "Scher Extensions"                                                                                                                                                            | Doku              |

## Upstream-Sync-Strategie

- Branch `scher-extensions` ist der Single Source of Truth dieses Forks.
- Bei Upstream-Updates: `git fetch upstream && git rebase upstream/main`.
- Patches sind so klein, dass Rebase üblicherweise konfliktfrei läuft.
- Falls Konflikt: in genau einer Datei (siehe Tabelle oben) — Konfliktauflösung
  ist immer "Upstream-Code + unsere Additions".

## Upstream-PR-Kandidaten

Bei Gelegenheit als generische Features an `ai-zerolab/mcp-email-server` zurückspielen:

1. `mark_seen` / `mark_unseen` (klar generisch)
2. `ensure_folder` (klar generisch)
3. `send_email` mit `message_id` Param (klar generisch)
4. `diag`-Tool (evtl., wenn Format-Convention diskutiert)

Bewusst NICHT als PR (Scher-spezifisch):

- `MCP_EMAIL_SERVER_REDIRECT_TO`-ENV — Test-/Staging-Sicherheitsnetz, projektspezifisch.
