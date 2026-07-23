from __future__ import annotations

from dataclasses import replace

import pytest

from mcp_email_server.application import limits


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("aaa", True),
        ("éé", True),
        ("ééa", False),
    ],
)
def test_controlled_string_limit_uses_utf8_bytes_at_limit_minus_one_limit_and_plus_one(
    value: str,
    valid: bool,
) -> None:
    if valid:
        assert (
            limits.validate_controlled_string(
                value,
                field_name="query",
                maximum_bytes=4,
            )
            == value
        )
    else:
        with pytest.raises(ValueError, match="exceeds 4 bytes"):
            limits.validate_controlled_string(
                value,
                field_name="query",
                maximum_bytes=4,
            )


@pytest.mark.parametrize("control", ["\x00", "\x1f", "\x7f"])
def test_controlled_string_rejects_c0_and_del(control: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        limits.validate_controlled_string(
            f"safe{control}unsafe",
            field_name="query",
            maximum_bytes=100,
        )


@pytest.mark.parametrize(
    ("payload", "valid"),
    [(b"aaa", True), (b"aaaa", True), (b"aaaaa", False)],
)
def test_serialized_result_limit_accepts_limit_minus_one_and_limit_only(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        limits,
        "APPLICATION_LIMITS",
        replace(limits.APPLICATION_LIMITS, serialized_response_bytes=4),
    )

    if valid:
        limits.validate_serialized_result(payload)
    else:
        with pytest.raises(ValueError, match="serialized result exceeds 4 bytes"):
            limits.validate_serialized_result(payload)
