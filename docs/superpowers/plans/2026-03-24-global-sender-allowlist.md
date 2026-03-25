# Global Sender Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global `allowed_senders` allowlist to `mcp-email-server` so the MCP client only sees emails from trusted senders, shielding it from spam and prompt-injection attempts.

**Architecture:** Add `allowed_senders: list[str] = []` to top-level `Settings`, configurable via TOML and env var `MCP_EMAIL_SERVER_ALLOWED_SENDERS` (comma-separated). Patterns support globs (`*@domain.com`) via `fnmatch`. Filter applied in `app.py` after the IMAP handler returns results for `list_emails_metadata` and `get_emails_content`. Expose via new `list_allowed_senders` tool. Emails remain untouched in the mailbox — only the AI's view is restricted.

**Tech Stack:** Python 3.12+, FastMCP, pydantic-settings v2, TOML config, `fnmatch` + `email.utils` (stdlib), pytest + pytest-asyncio, uv

**Spec:** `docs/superpowers/specs/2026-03-24-sender-allowlist-design.md`

---

## Chunk 1: Branch Setup

### Task 1: Create a clean feature branch from upstream main

**Files:**

- Working directory: `/Users/constantin/Library/CloudStorage/Dropbox/2nd Brain/1 Projects/smtp-mcp/`

> We need a fresh branch based on upstream `main` so this PR is independent of the recipient allowlist PR (#139).

- [ ] **Step 1: Confirm git remotes are configured correctly**

```bash
git remote -v
```

Expected output: both `origin` (your fork, `git@github.com:zalez/mcp-email-server.git`) and `upstream` (`https://github.com/ai-zerolab/mcp-email-server.git`) listed. If `upstream` is missing: `git remote add upstream https://github.com/ai-zerolab/mcp-email-server.git`

- [ ] **Step 2: Fetch latest from upstream**

```bash
git fetch upstream
```

Expected: upstream refs updated with no errors.

- [ ] **Step 3: Create and switch to the new feature branch from upstream/main**

```bash
git checkout -b feat/global-sender-allowlist upstream/main
```

Expected: `Switched to a new branch 'feat/global-sender-allowlist'`

Note: this branch does **not** include the recipient allowlist changes from `feat/global-recipient-allowlist` — these are independent PRs. That is intentional.

- [ ] **Step 4: Verify the test baseline passes**

```bash
uv run python -m pytest tests/ --tb=short -q
```

Expected: all tests pass (140 total — the 13 new recipient-allowlist tests are not on this branch). Note the exact count. If any tests fail, investigate before proceeding.

- [ ] **Step 5: Verify tomli_w is available**

```bash
uv run python -c "import tomli_w; print('ok')"
```

Expected: `ok`. If `ModuleNotFoundError`: `uv pip install tomli-w`

---

## Chunk 2: Config Changes (TDD)

> **Prerequisite:** Chunk 1 complete. Branch is `feat/global-sender-allowlist`.

**Files:**

- Modify: `mcp_email_server/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_env_config_coverage.py`

### Task 2: Add `allowed_senders` field with TOML normalisation

- [ ] **Step 1: Write two failing config field tests**

Open `tests/test_config.py` and add at the end of the file:

```python
def test_allowed_senders_defaults_to_empty(tmp_path, monkeypatch):
    """allowed_senders is empty by default (allow-all)."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    config_file = tmp_path / "config.toml"
    config_file.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == []
    finally:
        config_module._settings = None


def test_allowed_senders_toml_normalised(tmp_path, monkeypatch):
    """Patterns from TOML are lowercased and deduplicated (globs preserved)."""
    import tomli_w
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    toml_data = {"allowed_senders": ["*@GLEZ.DE", "*@glez.de", "Alice@Example.COM"]}
    config_file = tmp_path / "config.toml"
    config_file.write_bytes(tomli_w.dumps(toml_data).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == ["*@glez.de", "alice@example.com"]
    finally:
        config_module._settings = None
```

> **Why `monkeypatch.setitem` not `monkeypatch.setattr`?** `SettingsConfigDict(toml_file=CONFIG_PATH)` bakes the path at class _definition_ time. Patching `CONFIG_PATH` after the fact has no effect on TOML reading. Patching `Settings.model_config["toml_file"]` directly is the only way to redirect pydantic-settings to a temp file.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_config.py::test_allowed_senders_defaults_to_empty tests/test_config.py::test_allowed_senders_toml_normalised -v
```

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'allowed_senders'`

- [ ] **Step 3: Add `allowed_senders` field to `Settings` in `config.py`**

Open `mcp_email_server/config.py`. Find the `Settings` class (search for `class Settings(BaseSettings)`). Add the field after `allowed_recipients`:

```python
class Settings(BaseSettings):
    emails: list[EmailSettings] = []
    providers: list[ProviderSettings] = []
    db_location: str = CONFIG_PATH.with_name("db.sqlite3").as_posix()
    enable_attachment_download: bool = False
    allowed_recipients: list[str] = []
    allowed_senders: list[str] = []   # ADD THIS LINE
```

- [ ] **Step 4: Add TOML normalisation to `Settings.__init__`**

In `Settings.__init__`, find the `allowed_recipients` env var block (it ends with setting `self.allowed_recipients`). Add directly after it:

```python
        # Normalise allowed_senders from TOML: lowercase and deduplicate (globs preserved)
        if self.allowed_senders:
            self.allowed_senders = list(
                dict.fromkeys(p.strip().lower() for p in self.allowed_senders if p.strip())
            )
```

- [ ] **Step 5: Run the config field tests to verify they pass**

```bash
uv run python -m pytest tests/test_config.py::test_allowed_senders_defaults_to_empty tests/test_config.py::test_allowed_senders_toml_normalised -v
```

Expected: PASS.

### Task 3: Add environment variable support for `allowed_senders`

- [ ] **Step 1: Write the three failing env var tests**

Open `tests/test_env_config_coverage.py` and add at the end of the file:

```python
def test_allowed_senders_from_env(tmp_path, monkeypatch):
    """MCP_EMAIL_SERVER_ALLOWED_SENDERS env var parsed as comma-separated list."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    config_file = tmp_path / "config.toml"
    config_file.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    monkeypatch.setenv(
        "MCP_EMAIL_SERVER_ALLOWED_SENDERS",
        "*@glez.de , Alice@EXAMPLE.COM , *@glez.de",
    )
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == ["*@glez.de", "alice@example.com"]
    finally:
        config_module._settings = None


def test_allowed_senders_env_empty_string(tmp_path, monkeypatch):
    """Empty MCP_EMAIL_SERVER_ALLOWED_SENDERS leaves list empty."""
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    config_file = tmp_path / "config.toml"
    config_file.write_text("")
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    monkeypatch.setenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS", "")
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == []
    finally:
        config_module._settings = None


def test_allowed_senders_env_overrides_toml(tmp_path, monkeypatch):
    """Env var takes precedence over TOML-configured allowed_senders."""
    import tomli_w
    import mcp_email_server.config as config_module
    from mcp_email_server.config import Settings

    toml_data = {"allowed_senders": ["toml@example.com"]}
    config_file = tmp_path / "config.toml"
    config_file.write_bytes(tomli_w.dumps(toml_data).encode())
    monkeypatch.setitem(Settings.model_config, "toml_file", config_file)
    monkeypatch.setenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS", "env@example.com")
    config_module._settings = None
    try:
        s = config_module.get_settings(reload=True)
        assert s.allowed_senders == ["env@example.com"]
    finally:
        config_module._settings = None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_env_config_coverage.py::test_allowed_senders_from_env tests/test_env_config_coverage.py::test_allowed_senders_env_empty_string tests/test_env_config_coverage.py::test_allowed_senders_env_overrides_toml -v
```

Expected: FAIL (field exists but env var parsing not implemented yet).

- [ ] **Step 3: Add env var parsing to `Settings.__init__` in `config.py`**

In `Settings.__init__`, directly after the TOML normalisation block you added in Task 2 Step 4, add:

```python
        # Parse allowed_senders from environment variable (comma-separated)
        # Env var takes precedence over TOML-configured value
        env_senders = os.getenv("MCP_EMAIL_SERVER_ALLOWED_SENDERS")
        if env_senders:
            self.allowed_senders = list(
                dict.fromkeys(p.strip().lower() for p in env_senders.split(",") if p.strip())
            )
```

- [ ] **Step 4: Run all env var tests to verify they pass**

```bash
uv run python -m pytest tests/test_env_config_coverage.py::test_allowed_senders_from_env tests/test_env_config_coverage.py::test_allowed_senders_env_empty_string tests/test_env_config_coverage.py::test_allowed_senders_env_overrides_toml -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Run all config tests to confirm nothing is broken**

```bash
uv run python -m pytest tests/test_config.py tests/test_env_config_coverage.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit config changes**

```bash
git add mcp_email_server/config.py tests/test_config.py tests/test_env_config_coverage.py
git commit -m "feat: add allowed_senders config field with env var support 🛡️

- Add allowed_senders: list[str] = [] to Settings (empty = allow all)
- Parse MCP_EMAIL_SERVER_ALLOWED_SENDERS env var (comma-separated)
- Env var takes precedence over TOML
- Normalise at parse time: lowercase + deduplicate, globs preserved
- Add 5 tests covering defaults, TOML normalisation, env var behaviour

Spam doesn't stand a chance 🚫📧

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Chunk 3: App Changes (TDD)

> **Prerequisite:** Chunk 2 complete. `Settings` must have `allowed_senders` or `app.py` will raise `AttributeError`.

**Files:**

- Modify: `mcp_email_server/app.py`
- Modify: `tests/test_mcp_tools.py`

### Task 4: Add `_sender_allowed` helper and `list_allowed_senders` tool

- [ ] **Step 1: Add imports and `_sender_allowed` to the test file's import block**

Open `tests/test_mcp_tools.py`. Add to the import from `mcp_email_server.app` (keep alphabetical):

```python
from mcp_email_server.app import (
    _sender_allowed,          # ADD — pure function, tested directly
    add_email_account,
    delete_emails,
    download_attachment,
    get_emails_content,
    list_allowed_recipients,
    list_allowed_senders,     # ADD
    list_available_accounts,
    list_emails_metadata,
    send_email,
)
```

- [ ] **Step 2: Write the failing `_sender_allowed` helper tests**

Add a new test class at the end of `tests/test_mcp_tools.py` (outside `TestMcpTools`):

```python
class TestSenderAllowed:
    """Tests for the _sender_allowed pure helper function."""

    def test_empty_patterns_allows_all(self):
        """Empty allowlist always returns True (allow all)."""
        assert _sender_allowed("anyone@evil.com", []) is True

    def test_exact_match(self):
        """Exact address pattern matches correctly."""
        assert _sender_allowed("alice@example.com", ["alice@example.com"]) is True

    def test_glob_domain(self):
        """Wildcard domain pattern matches any address at that domain."""
        assert _sender_allowed("bob@glez.de", ["*@glez.de"]) is True

    def test_name_addr_format(self):
        """'Name <addr>' format: address portion extracted and matched."""
        assert _sender_allowed("Alice <alice@example.com>", ["alice@example.com"]) is True

    def test_case_insensitive(self):
        """Matching is case-insensitive (patterns pre-lowercased, sender lowercased at match time)."""
        assert _sender_allowed("Alice@EXAMPLE.COM", ["alice@example.com"]) is True

    def test_no_match(self):
        """Address not in allowlist returns False."""
        assert _sender_allowed("spam@evil.com", ["*@glez.de"]) is False

    def test_matches_second_pattern_in_list(self):
        """Sender matching the second pattern returns True (any() traversal)."""
        assert _sender_allowed("bob@example.com", ["alice@example.com", "bob@example.com"]) is True

    def test_unparseable_sender_blocked(self):
        """Malformed From header that parseaddr cannot parse is treated as not allowed."""
        # parseaddr("not-an-email-at-all") returns ('', '') so fallback is the raw string,
        # which will not match any normal pattern like *@example.com
        assert _sender_allowed("not-an-email-at-all", ["*@example.com"]) is False
```

Also add the `list_allowed_senders` tests inside `TestMcpTools` at the end of that class:

```python
    @pytest.mark.asyncio
    async def test_list_allowed_senders_empty(self):
        """Returns empty list when no sender allowlist is configured."""
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            result = await list_allowed_senders()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_allowed_senders_returns_patterns(self):
        """Returns configured patterns verbatim (including globs)."""
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de", "alice@example.com"]

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            result = await list_allowed_senders()

        assert result == ["*@glez.de", "alice@example.com"]
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestSenderAllowed tests/test_mcp_tools.py::TestMcpTools::test_list_allowed_senders_empty tests/test_mcp_tools.py::TestMcpTools::test_list_allowed_senders_returns_patterns -v
```

Expected: FAIL with `ImportError: cannot import name '_sender_allowed'`

- [ ] **Step 4: Add imports and `_sender_allowed` helper to `app.py`**

Open `mcp_email_server/app.py`. Add to the stdlib imports at the top (keep alphabetical with existing imports):

```python
import fnmatch
from email import utils as email_utils
```

Then add the private helper function before the first `@mcp.tool` decorator (i.e., before `list_available_accounts`):

```python
def _sender_allowed(sender: str, patterns: list[str]) -> bool:
    """Return True if sender matches any pattern in the allowlist, or if the list is empty.

    Handles 'Name <addr>' format via email.utils.parseaddr. Matching is case-insensitive.
    Patterns support fnmatch globs (e.g. *@example.com).

    Unparseable sender strings (malformed From headers) are treated as not allowed
    when an allowlist is configured — the safe default for the threat model.
    """
    if not patterns:
        return True
    _, addr = email_utils.parseaddr(sender)  # handles "Name <addr>" and bare addresses
    addr = (addr or sender).lower()          # fallback to raw string if parse fails
    return any(fnmatch.fnmatch(addr, pattern) for pattern in patterns)
```

- [ ] **Step 5: Add `list_allowed_senders` tool to `app.py`**

Insert the new tool after the `list_allowed_recipients` tool and before `send_email`:

```python
@mcp.tool(
    description=(
        "List the globally allowed sender email address patterns. "
        "Returns an empty list if no allowlist is configured, meaning emails from all senders are visible. "
        "Patterns may include wildcards (e.g. *@example.com). "
        "Call this tool to understand which senders the MCP client is permitted to read."
    )
)
async def list_allowed_senders() -> list[str]:
    return get_settings().allowed_senders
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestSenderAllowed tests/test_mcp_tools.py::TestMcpTools::test_list_allowed_senders_empty tests/test_mcp_tools.py::TestMcpTools::test_list_allowed_senders_returns_patterns -v
```

Expected: all PASS.

### Task 5: Add sender filter to `list_emails_metadata`

- [ ] **Step 1: Write the failing filter tests for `list_emails_metadata`**

Add these tests inside `TestMcpTools` at the end of the class (after the `list_allowed_senders` tests):

```python
    @pytest.mark.asyncio
    async def test_list_emails_metadata_no_sender_allowlist(self):
        """All emails returned when sender allowlist is empty."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Hi",
                    sender="anyone@evil.com",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=1,
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(account_name="test")

        assert len(result.emails) == 1

    @pytest.mark.asyncio
    async def test_list_emails_metadata_filters_blocked_sender(self):
        """Emails from unlisted senders are removed; allowed senders remain."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
                EmailMetadata(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=2,
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(account_name="test")

        assert len(result.emails) == 1
        assert result.emails[0].email_id == "1"

    @pytest.mark.asyncio
    async def test_list_emails_metadata_total_unchanged_after_filter(self):
        """total reflects IMAP server count and is not adjusted post-filter."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        page = EmailMetadataPageResponse(
            page=1,
            page_size=10,
            before=None,
            since=None,
            subject=None,
            emails=[
                EmailMetadata(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
                EmailMetadata(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    attachments=[],
                ),
            ],
            total=50,  # large server-side total — left unchanged after filtering
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_metadata.return_value = page

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await list_emails_metadata(account_name="test")

        assert result.total == 50     # unchanged — reflects IMAP server count
        assert len(result.emails) == 1  # filtered in the response
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_no_sender_allowlist tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_filters_blocked_sender tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_total_unchanged_after_filter -v
```

Expected: the no-allowlist test may trivially pass (empty list = allow all), but the filter test FAILS (no filtering logic exists yet).

- [ ] **Step 3: Add the sender filter to `list_emails_metadata` in `app.py`**

Open `mcp_email_server/app.py`. Find `list_emails_metadata`. Replace the entire function body (from the return type annotation to the closing `)`):

```python
) -> EmailMetadataPageResponse:
    handler = dispatch_handler(account_name)

    return await handler.get_emails_metadata(
        page=page,
        page_size=page_size,
        before=before,
        since=since,
        subject=subject,
        from_address=from_address,
        to_address=to_address,
        order=order,
        mailbox=mailbox,
        seen=seen,
        flagged=flagged,
        answered=answered,
    )
```

With:

```python
) -> EmailMetadataPageResponse:
    # Read settings before the IMAP call to skip the round-trip when allowlist is empty
    allowed = get_settings().allowed_senders
    handler = dispatch_handler(account_name)

    result = await handler.get_emails_metadata(
        page=page,
        page_size=page_size,
        before=before,
        since=since,
        subject=subject,
        from_address=from_address,
        to_address=to_address,
        order=order,
        mailbox=mailbox,
        seen=seen,
        flagged=flagged,
        answered=answered,
    )
    if allowed:
        result.emails = [e for e in result.emails if _sender_allowed(e.sender, allowed)]
    # Note: result.total reflects the IMAP server-side count and is intentionally not adjusted.
    # See known limitations in the spec.
    return result
```

- [ ] **Step 4: Run all three filter tests to verify they pass**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_no_sender_allowlist tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_filters_blocked_sender tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_total_unchanged_after_filter -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Confirm existing `list_emails_metadata` tests still pass**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata tests/test_mcp_tools.py::TestMcpTools::test_list_emails_metadata_with_mailbox -v
```

Expected: both PASS. These tests don't patch `get_settings` — the real settings is loaded, returning `allowed_senders = []`, which means the `if allowed:` guard is skipped and the behaviour is unchanged.

### Task 6: Add sender filter to `get_emails_content`

- [ ] **Step 1: Write the failing filter tests for `get_emails_content`**

Add these tests inside `TestMcpTools`:

```python
    @pytest.mark.asyncio
    async def test_get_emails_content_no_sender_allowlist(self):
        """All emails returned when sender allowlist is empty."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = []
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Any",
                    sender="anyone@evil.com",
                    recipients=[],
                    date=now,
                    body="hello",
                    attachments=[],
                ),
            ],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1"])

        assert len(result.emails) == 1

    @pytest.mark.asyncio
    async def test_get_emails_content_filters_blocked_sender(self):
        """Emails from unlisted senders are silently dropped."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    body="hi",
                    attachments=[],
                ),
                EmailBodyResponse(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    body="buy now",
                    attachments=[],
                ),
            ],
            requested_count=2,
            retrieved_count=2,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1", "2"])

        assert len(result.emails) == 1
        assert result.emails[0].email_id == "1"

    @pytest.mark.asyncio
    async def test_get_emails_content_retrieved_count_adjusted(self):
        """retrieved_count is updated to reflect post-filter count."""
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Good",
                    sender="friend@glez.de",
                    recipients=[],
                    date=now,
                    body="hi",
                    attachments=[],
                ),
                EmailBodyResponse(
                    email_id="2",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    body="buy",
                    attachments=[],
                ),
            ],
            requested_count=2,
            retrieved_count=2,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1", "2"])

        assert result.retrieved_count == 1  # adjusted to post-filter count

    @pytest.mark.asyncio
    async def test_get_emails_content_blocked_not_in_failed_ids(self):
        """Blocked emails are silently dropped — NOT added to failed_ids.

        Adding a blocked email to failed_ids would reveal its existence to the AI,
        defeating the purpose of the allowlist.
        """
        now = datetime.now(timezone.utc)
        mock_settings = MagicMock()
        mock_settings.allowed_senders = ["*@glez.de"]
        batch = EmailContentBatchResponse(
            emails=[
                EmailBodyResponse(
                    email_id="1",
                    subject="Spam",
                    sender="spam@evil.com",
                    recipients=[],
                    date=now,
                    body="buy",
                    attachments=[],
                ),
            ],
            requested_count=1,
            retrieved_count=1,
            failed_ids=[],
        )
        mock_handler = AsyncMock()
        mock_handler.get_emails_content.return_value = batch

        with patch("mcp_email_server.app.get_settings", return_value=mock_settings):
            with patch("mcp_email_server.app.dispatch_handler", return_value=mock_handler):
                result = await get_emails_content(account_name="test", email_ids=["1"])

        assert result.failed_ids == []   # NOT populated with blocked email
        assert len(result.emails) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_filters_blocked_sender tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_retrieved_count_adjusted tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_blocked_not_in_failed_ids -v
```

Expected: `test_get_emails_content_filters_blocked_sender`, `test_get_emails_content_retrieved_count_adjusted`, and `test_get_emails_content_blocked_not_in_failed_ids` FAIL (no filtering logic yet). `test_get_emails_content_no_sender_allowlist` may trivially pass — this is expected, as an empty allowlist means the guard is never reached.

- [ ] **Step 3: Add the sender filter to `get_emails_content` in `app.py`**

Open `mcp_email_server/app.py`. Find `get_emails_content`. Replace:

```python
) -> EmailContentBatchResponse:
    handler = dispatch_handler(account_name)
    return await handler.get_emails_content(email_ids, mailbox)
```

With:

```python
) -> EmailContentBatchResponse:
    # Read settings before the IMAP call to skip the round-trip when allowlist is empty
    allowed = get_settings().allowed_senders
    handler = dispatch_handler(account_name)
    result = await handler.get_emails_content(email_ids, mailbox)
    if allowed:
        result.emails = [e for e in result.emails if _sender_allowed(e.sender, allowed)]
        result.retrieved_count = len(result.emails)
        # Blocked emails are silently dropped — NOT added to failed_ids.
        # Adding them would reveal their existence to the AI.
    return result
```

- [ ] **Step 4: Run all `get_emails_content` filter tests to verify they pass**

```bash
uv run python -m pytest tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_no_sender_allowlist tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_filters_blocked_sender tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_retrieved_count_adjusted tests/test_mcp_tools.py::TestMcpTools::test_get_emails_content_blocked_not_in_failed_ids -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Run the full test suite to confirm nothing is broken**

```bash
uv run python -m pytest tests/ --tb=short -q
```

Expected: all tests pass. Count should be baseline + new tests (8 `_sender_allowed` + 2 `list_allowed_senders` + 3 `list_emails_metadata` filter + 4 `get_emails_content` filter + 5 config = 22 new tests).

- [ ] **Step 6: Commit the app changes**

```bash
git add mcp_email_server/app.py tests/test_mcp_tools.py
git commit -m "feat: add sender allowlist filter and list_allowed_senders tool 📬🛡️

- Add _sender_allowed() helper: fnmatch glob matching with parseaddr extraction
- Add list_allowed_senders tool — returns configured patterns or []
- Filter list_emails_metadata results by sender allowlist (total unchanged)
- Filter get_emails_content results; adjust retrieved_count; blocked emails
  silently dropped (not added to failed_ids — avoids info leakage to AI)
- Empty allowlist = allow all (backwards-compatible default)

No more prompt injection through your inbox 🔐

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Chunk 4: PR Prep

### Task 7: Update README documentation

- [ ] **Step 1: Add `MCP_EMAIL_SERVER_ALLOWED_SENDERS` to the env var table in `README.md`**

Find the environment variable configuration table (search for `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`). Add a row immediately after the `allowed_recipients` row:

```markdown
| `MCP_EMAIL_SERVER_ALLOWED_SENDERS` | Comma-separated list of permitted sender address patterns. Supports wildcards (e.g. `*@example.com`). Empty = allow all (default). | - | No |
```

- [ ] **Step 2: Add a "Filtering Incoming Email (Sender Allowlist)" section to `README.md`**

Find the "Restricting Recipients (Allowlist)" section. Add a similar section directly after it:

````markdown
### Filtering Incoming Email (Sender Allowlist)

By default, the MCP client can read emails from any sender. You can restrict this to a
trusted set of senders using the `allowed_senders` option, protecting the AI from spam
and prompt-injection attempts via email.

**Option 1: Environment Variable**

```json
{
  "mcpServers": {
    "zerolib-email": {
      "command": "uvx",
      "args": ["mcp-email-server@latest", "stdio"],
      "env": {
        "MCP_EMAIL_SERVER_ALLOWED_SENDERS": "*@trusted-company.com,alice@example.com"
      }
    }
  }
}
```

**Option 2: TOML Configuration**

```toml
allowed_senders = ["*@trusted-company.com", "alice@example.com"]
```

> **Note:** This setting must be at the **top level** of the config file, not inside an
> `[[emails]]` block. If placed inside `[[emails]]`, it is silently ignored with no error.

When non-empty, `list_emails_metadata` and `get_emails_content` silently exclude emails
from unlisted senders. Patterns support wildcards (`*@example.com` matches any address at
that domain). Matching is case-insensitive. Use `list_allowed_senders` to let the MCP
client discover permitted senders.

Emails from filtered senders remain visible in your normal mail client — only the MCP
client's view is restricted.
````

- [ ] **Step 3: Commit the README update**

```bash
git add README.md
git commit -m "docs: document allowed_senders config in README 📖

- Add env var row to configuration table
- Add 'Filtering Incoming Email (Sender Allowlist)' section with examples
- Include TOML placement warning (top-level only)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

### Task 8: Open issue and submit pull request

- [ ] **Step 1: Run the full test suite one final time**

```bash
uv run python -m pytest tests/ --tb=short -q
```

Expected: all tests pass, no warnings beyond the pre-existing pydantic deprecation notices.

- [ ] **Step 2: Open a GitHub issue on the upstream repo**

```bash
ISSUE_URL=$(gh issue create \
  --repo ai-zerolab/mcp-email-server \
  --title "Feature: global sender allowlist for list_emails_metadata and get_emails_content" \
  --body "## Description

Add a global \`allowed_senders\` configuration option that restricts which emails the MCP client can read, based on sender address patterns.

## Use case

When using an MCP-capable AI assistant with \`mcp-email-server\`, the assistant has unrestricted access to read emails from any sender. A configurable sender allowlist protects against:
- **Prompt injection**: malicious actors sending emails crafted to manipulate the AI
- **Spam influence**: unwanted emails affecting AI behaviour or filling context

## Proposed behaviour

- New top-level config field: \`allowed_senders = [\"*@trusted.com\", \"alice@example.com\"]\` in TOML
- New env var: \`MCP_EMAIL_SERVER_ALLOWED_SENDERS=*@trusted.com,alice@example.com\`
- Patterns support fnmatch globs (\`*@domain.com\` matches any address at that domain)
- Empty list (default) = allow all (fully backwards-compatible)
- When non-empty: \`list_emails_metadata\` and \`get_emails_content\` silently exclude emails from unlisted senders
- New \`list_allowed_senders\` tool so the MCP client can discover permitted senders
- Emails remain untouched in the mailbox — only the AI's view is restricted
- Matching is case-insensitive; patterns normalised to lowercase at parse time") && \
ISSUE_NUM=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$') && \
echo "Issue #$ISSUE_NUM created at $ISSUE_URL"
```

Expected: `Issue #NNN created at https://github.com/ai-zerolab/mcp-email-server/issues/NNN`

Note the issue number — you'll need it for the PR body (`$ISSUE_NUM`).

- [ ] **Step 3: Push the feature branch to your fork**

```bash
git push -u origin feat/global-sender-allowlist
```

Expected: branch pushed with tracking ref set.

- [ ] **Step 4: Open the pull request**

Replace `NNN` with the actual issue number from Step 2:

```bash
gh pr create \
  --repo ai-zerolab/mcp-email-server \
  --title "feat: add global sender allowlist for list_emails_metadata and get_emails_content" \
  --body "## Summary

- Adds \`allowed_senders: list[str] = []\` to \`Settings\` — empty means allow all (backwards-compatible)
- Configurable via TOML (\`allowed_senders = [\"*@glez.de\", \"alice@example.com\"]\`) or env var (\`MCP_EMAIL_SERVER_ALLOWED_SENDERS=*@glez.de,alice@example.com\`)
- **Note:** the TOML setting must be at the top level of the config file, not nested inside an \`[[emails]]\` block (silently ignored otherwise)
- Patterns support fnmatch globs (\`*@domain.com\` matches any address at that domain); matching is case-insensitive
- \`list_emails_metadata\` and \`get_emails_content\` silently exclude emails from unlisted senders
- \`retrieved_count\` in \`get_emails_content\` is adjusted to reflect the post-filter count; blocked emails are not added to \`failed_ids\` (avoids leaking their existence to the AI)
- \`total\` in \`list_emails_metadata\` is intentionally not adjusted (reflects IMAP server count; correcting it would require fetching all pages)
- New \`list_allowed_senders\` tool so the MCP client can discover permitted sender patterns
- Emails remain untouched in the mailbox — only the MCP client's view is restricted
- README updated with env var table entry and usage section

## Motivation

When using an MCP-capable AI assistant with \`mcp-email-server\`, the assistant has unrestricted access to read emails from any sender. This creates two risks:
1. **Prompt injection**: a malicious actor sends a crafted email to manipulate the AI's behaviour
2. **Spam influence**: unwanted emails polluting the AI's context

A sender allowlist mitigates both by restricting which emails the AI can see, without touching the mailbox.

## Test plan

- [x] \`allowed_senders = []\` → all emails visible (default, backwards-compatible)
- [x] Listed sender (exact address) → email visible
- [x] Listed sender (glob: \`*@glez.de\`) → email visible
- [x] \`\"Name <addr>\"\` format → address extracted and matched correctly
- [x] Case-insensitive match (\`Alice@EXAMPLE.COM\` matches pattern \`alice@example.com\`)
- [x] Unlisted sender → silently excluded from \`list_emails_metadata\` and \`get_emails_content\`
- [x] \`total\` in \`list_emails_metadata\` unchanged after filtering
- [x] \`retrieved_count\` in \`get_emails_content\` adjusted; blocked email not in \`failed_ids\`
- [x] Malformed From header treated as not allowed (safe default)
- [x] Env var parsed correctly; whitespace stripped; duplicates removed
- [x] \`list_allowed_senders\` returns \`[]\` or the configured patterns
- [x] Full existing test suite passes unchanged
- [x] End-to-end tested with Claude Desktop: unlisted sender correctly excluded, listed sender visible, \`list_allowed_senders\` returned configured patterns

Closes #NNN

## Transparency note

This code was written by [Claude Code](https://claude.ai/claude-code) using the [Superpowers plugin](https://github.com/superpowers-ai/claude-plugins), following a spec and implementation plan designed collaboratively with the author. All code was reviewed and end-to-end tested by the author before submission. Commits include \`Co-Authored-By: Claude Sonnet 4.6\` to reflect this.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 5: Verify the PR was created correctly**

```bash
gh pr view --repo ai-zerolab/mcp-email-server
```

Confirm: correct title, base branch is `main`, head is your fork's `feat/global-sender-allowlist`, issue reference is present.
