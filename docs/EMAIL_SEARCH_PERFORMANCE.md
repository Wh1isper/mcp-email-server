# Email Search Performance & Filter Requirements

## Overview

The `list_emails_metadata()` method requires at least one search filter to prevent expensive mailbox scans that can timeout on large mailboxes. This document explains the reasoning and best practices.

## Why Filters Are Required

### The Problem: Unfiltered Searches

When calling `list_emails_metadata()` without any filters:

```python
# ❌ BAD: No filters
list_emails_metadata(account_name="Galaxia", page=1, page_size=5)
```

Despite requesting only 5 emails per page, the internal flow is:

```
1. IMAP uid_search("ALL")           ← Scans entire mailbox
2. Fetch dates for ALL emails       ← Could be 100,000+ emails
3. Sort ALL emails                  ← Memory intensive
4. Then paginate to get 5 emails    ← Finally!
```

**Pagination only applies AFTER the expensive operations.** This means:

- On a mailbox with 10,000 emails: seconds of delay
- On a mailbox with 100,000+ emails: minutes or timeout
- On enterprise mailboxes: can hang indefinitely

### IMAP Protocol Limitation

IMAP doesn't support "give me the first N emails" queries. The protocol requires:

1. **SEARCH** - Define criteria and get matching UIDs (returns ALL matches)
2. **FETCH** - Get data for specific UIDs
3. **SORT** - Order results (optional, server-dependent)

There's no built-in way to limit results before the search phase.

## Best Practices

### ✅ Fast Searches

#### 1. Date Range (Fastest)

```python
# Get last 30 days of emails
from datetime import datetime, timedelta
since = datetime.now() - timedelta(days=30)
result = list_emails_metadata(account_name="Galaxia", since=since)
```

**Why:** IMAP servers heavily index by date. Returns only recent emails.

#### 2. Text Search (Medium Speed)

```python
# Search for specific sender
result = list_emails_metadata(
    account_name="Galaxia",
    from_address="boss@company.com"
)
```

**Why:** Text searches use server indices, but could match many emails.

#### 3. Combined Filters (Fastest & Best)

```python
# Search for work emails from last month
since = datetime.now() - timedelta(days=30)
result = list_emails_metadata(
    account_name="Galaxia",
    subject="project",
    from_address="team@company.com",
    since=since
)
```

**Why:** Narrows search space at IMAP level (most efficient).

#### 4. Flag-Based Search

```python
# Get unread emails from the last 7 days
since = datetime.now() - timedelta(days=7)
result = list_emails_metadata(
    account_name="Galaxia",
    seen=False,  # Unread emails
    since=since
)
```

**Why:** Flag searches are fast; combining with date range is best.

## Performance Comparison

| Query                  | Mailbox Size     | Time         |
| ---------------------- | ---------------- | ------------ |
| `SEARCH ALL`           | 10,000 emails    | ~1 second    |
| `SEARCH ALL`           | 100,000 emails   | ~10+ seconds |
| `SEARCH ALL`           | 1,000,000 emails | **TIMEOUT**  |
| `SEARCH SINCE <date>`  | Any size         | ~100ms       |
| `SEARCH FROM "sender"` | 100,000 emails   | ~500ms       |
| `SEARCH SINCE + FROM`  | 100,000 emails   | ~100ms       |

## Error Message Explanation

When no filters are provided:

```
ValueError: At least one filter is required to prevent expensive searches
on large mailboxes. Recommended: combine a date range (since/before) with
optional text filters (subject/from/to).
Example: since=datetime(2026, 1, 1) or subject='work' + since=datetime(2025, 1, 1)
```

This error prevents:

- Silent performance degradation
- Unexplained timeouts
- User frustration with "why is this so slow?"

## Available Filters

All filters prevent full mailbox scans:

- **`since`** (datetime) - Emails after date (fastest)
- **`before`** (datetime) - Emails before date (fastest)
- **`subject`** (string) - Subject line text search
- **`from_address`** (string) - Sender email address
- **`to_address`** (string) - Recipient email address
- **`seen`** (bool) - Read/unread emails
- **`flagged`** (bool) - Starred/flagged emails
- **`answered`** (bool) - Emails with replies

## Recommendations

### For Applications

1. **Always provide a date range** - This is the fastest and most predictable
2. **Combine with text filters** - Narrow results further
3. **Handle pagination** - Combine with `page` and `page_size`
4. **Cache results** - Don't re-query immediately

### For Users

1. **Start with recent emails** - Last 30-90 days is usually sufficient
2. **Use specific searches** - If looking for something, add subject/from filters
3. **Be explicit** - Don't rely on defaults; always specify your intent

## Migration Guide

If you were using unfiltered searches before:

### Before (Would fail now)

```python
result = list_emails_metadata(account_name="Galaxia")
```

### After

```python
from datetime import datetime, timedelta

# Option 1: Last 30 days
since = datetime.now() - timedelta(days=30)
result = list_emails_metadata(account_name="Galaxia", since=since)

# Option 2: Search for specific sender
result = list_emails_metadata(
    account_name="Galaxia",
    from_address="colleague@company.com"
)

# Option 3: Combine filters (recommended)
since = datetime.now() - timedelta(days=90)
result = list_emails_metadata(
    account_name="Galaxia",
    subject="project",
    since=since
)
```

## FAQ

**Q: Can I see all my emails?**
A: Yes, use a large date range: `since=datetime(2000, 1, 1)`. On large mailboxes, this may take several seconds or timeout depending on server capacity.

**Q: Why is pagination alone not enough?**
A: IMAP requires a full search before paginating. Pagination only applies after results are returned, so it doesn't prevent the initial expensive scan.

**Q: What if my IMAP server is fast?**
A: Even fast servers struggle with "ALL" searches on mailboxes with 100,000+ emails. Date range filters are always safer.

**Q: Can I search my entire mailbox?**
A: Technically yes, but it's not recommended for mailboxes > 50,000 emails. Use: `since=datetime(2000, 1, 1)` and be patient. Consider pagination with small `page_size` values.

## See Also

- [IMAP RFC 3501](https://tools.ietf.org/html/rfc3501) - IMAP Protocol Specification
- [mcp-email-server Configuration Guide](../README.md)
