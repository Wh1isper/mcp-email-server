from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ApplicationLimits:
    """Single immutable owner for direct, provider, and serialized work bounds."""

    mutation_uids: int = 100
    content_email_ids: int = 500
    configured_accounts: int = 1_000
    policy_entries: int = 1_000
    account_name_bytes: int = 256
    account_description_bytes: int = 4 * 1_024
    recipients: int = 100
    address_bytes: int = 1_024
    mailbox_bytes: int = 1_024
    query_bytes: int = 64 * 1_024
    mailboxes: int = 1_000
    mailbox_result_bytes: int = 1 * 1_024 * 1_024
    subject_bytes: int = 64 * 1_024
    body_bytes: int = 1 * 1_024 * 1_024
    aggregate_body_bytes: int = 50 * 1_024 * 1_024
    header_bytes: int = 64 * 1_024
    aggregate_header_bytes: int = 4 * 1_024 * 1_024
    metadata_candidates: int = 10_000
    metadata_snapshot_rows: int = 1_000
    attachments: int = 20
    attachment_path_bytes: int = 4_096
    flags: int = 100
    flag_bytes: int = 128
    attachment_bytes: int = 25 * 1_024 * 1_024
    total_attachment_bytes: int = 50 * 1_024 * 1_024
    maximum_imap_uid: int = 2**32 - 1
    credential_cleanup_rows: int = 100
    warning_items: int = 100
    error_detail_bytes: int = 4_096
    serialized_response_bytes: int = 8 * 1_024 * 1_024
    mcp_stdio_frame_bytes: int = 2 * 1_024 * 1_024
    spill_file_bytes: int = 64 * 1_024 * 1_024
    ui_json_body_bytes: int = 1 * 1_024 * 1_024
    provider_timeout_seconds: float = 60.0


APPLICATION_LIMITS = ApplicationLimits()
_CANONICAL_IMAP_UID = re.compile(r"[1-9][0-9]*\Z")
_MAX_METADATA_SNAPSHOT_ROWS = 100_000


def metadata_snapshot_row_limit() -> int:
    """Return the configured metadata-projection row ceiling.

    The built-in default remains 1,000. ``MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS``
    overrides it with a decimal integer from 1 through 100,000 so a mailbox
    larger than the default can still become a complete projection.
    """

    return _bounded_decimal_env(
        "MCP_EMAIL_SERVER_METADATA_SNAPSHOT_ROWS",
        default=APPLICATION_LIMITS.metadata_snapshot_rows,
        minimum=1,
        maximum=_MAX_METADATA_SNAPSHOT_ROWS,
    )


_MIN_METADATA_REFRESH_TIMEOUT_SECONDS = 1.0
_MAX_METADATA_REFRESH_TIMEOUT_SECONDS = 3_600.0
_MAX_METADATA_HEADER_BUDGET_BYTES = 64 * 1_024 * 1_024
_DEFAULT_METADATA_REFRESH_BATCH_SIZE = 100
_MAX_METADATA_REFRESH_BATCH_SIZE = 1_000
_DECIMAL_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _bounded_decimal_env(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    value = int(text)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def metadata_refresh_timeout_seconds() -> float:
    """Return the per-batch deadline for a metadata projection refresh.

    Unset keeps the historical 60-second provider deadline. The value is applied
    to each UID batch, not to the whole mailbox, so a large refresh can outlive
    one interval without losing the batches that already finished.
    """

    raw = os.getenv("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS")
    if raw is None:
        return APPLICATION_LIMITS.provider_timeout_seconds
    text = raw.strip()
    if _DECIMAL_SECONDS.fullmatch(text) is None:
        raise ValueError("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS must be a number of seconds from 1 to 3600")
    value = float(text)
    if not _MIN_METADATA_REFRESH_TIMEOUT_SECONDS <= value <= _MAX_METADATA_REFRESH_TIMEOUT_SECONDS:
        raise ValueError("MCP_EMAIL_SERVER_METADATA_REFRESH_TIMEOUT_SECONDS must be a number of seconds from 1 to 3600")
    return value


def metadata_header_budget_bytes() -> int:
    """Return the aggregate raw-header budget for one metadata fetch batch.

    The built-in default remains 4 MiB. A projection refresh applies it to each
    UID batch. A live filtered metadata query still applies it once to that query.
    """

    return _bounded_decimal_env(
        "MCP_EMAIL_SERVER_METADATA_HEADER_BUDGET_BYTES",
        default=APPLICATION_LIMITS.aggregate_header_bytes,
        minimum=APPLICATION_LIMITS.header_bytes,
        maximum=_MAX_METADATA_HEADER_BUDGET_BYTES,
    )


def metadata_refresh_batch_size() -> int:
    """Return the configured newest-first UID batch size for a projection refresh."""

    return _bounded_decimal_env(
        "MCP_EMAIL_SERVER_METADATA_REFRESH_BATCH_SIZE",
        default=_DEFAULT_METADATA_REFRESH_BATCH_SIZE,
        minimum=1,
        maximum=_MAX_METADATA_REFRESH_BATCH_SIZE,
    )


def metadata_refresh_batch_limit() -> int:
    """Return a batch size that cannot exceed the header budget at the per-message cap."""

    fitted = max(1, metadata_header_budget_bytes() // (APPLICATION_LIMITS.header_bytes + 1))
    return min(metadata_refresh_batch_size(), fitted)


def validate_imap_uid(value: object, *, field_name: str = "email_id") -> str:
    """Return one canonical positive decimal ASCII IMAP UID within RFC range."""

    if not isinstance(value, str) or _CANONICAL_IMAP_UID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical positive decimal IMAP UID")
    if int(value) > APPLICATION_LIMITS.maximum_imap_uid:
        raise ValueError(f"{field_name} exceeds the maximum IMAP UID")
    return value


def contains_c0_or_del(value: str) -> bool:
    """Return whether a string contains an ASCII C0 or DEL control."""

    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def validate_controlled_string(
    value: object,
    *,
    field_name: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str:
    """Validate a command/query string at the shared application boundary."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")  # noqa: TRY004 - validation contract
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if contains_c0_or_del(value):
        raise ValueError(f"{field_name} must not contain control characters")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field_name} exceeds {maximum_bytes} bytes")
    return value


def validate_optional_controlled_string(
    value: object | None,
    *,
    field_name: str,
    maximum_bytes: int,
) -> str | None:
    """Validate an optional controlled string while preserving empty compatibility."""

    if value is None:
        return None
    return validate_controlled_string(
        value,
        field_name=field_name,
        maximum_bytes=maximum_bytes,
        allow_empty=True,
    )


def validate_serialized_result(serialized: bytes) -> None:
    """Reject a canonically encoded application result above the shared ceiling."""

    if len(serialized) > APPLICATION_LIMITS.serialized_response_bytes:
        raise ValueError(f"serialized result exceeds {APPLICATION_LIMITS.serialized_response_bytes} bytes")
