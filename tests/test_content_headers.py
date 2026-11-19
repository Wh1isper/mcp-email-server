from dataclasses import replace
from email.parser import BytesParser
from email.policy import default

import pytest

from mcp_email_server.emails import headers
from mcp_email_server.emails.headers import DEFAULT_CONTENT_HEADERS, ContentHeaderLimitError, select_content_headers
from mcp_email_server.emails.models import EmailHeaderObservation


def test_select_content_headers_preserves_order_repeats_and_folds() -> None:
    message = BytesParser(policy=default).parsebytes(
        b"Received: newest\r\n"
        b"Authentication-Results: mx.example.test;\r\n\tdmarc=pass\r\n"
        b"received: older\r\n"
        b"Authentication-Results: upstream.example.test; dkim=pass\r\n"
        b"X-Example: =?utf-8?b?YQ==?=\r\n"
        b"Content-Type: message/rfc822\r\n\r\n"
        b"Received: nested\r\n\r\nInner body\r\n"
    )

    assert select_content_headers(message, DEFAULT_CONTENT_HEADERS) == [
        EmailHeaderObservation(name="Received", value="newest"),
        EmailHeaderObservation(name="Authentication-Results", value="mx.example.test;\tdmarc=pass"),
        EmailHeaderObservation(name="received", value="older"),
        EmailHeaderObservation(name="Authentication-Results", value="upstream.example.test; dkim=pass"),
    ]
    assert select_content_headers(message, ("x-example", "X-Example")) == [
        EmailHeaderObservation(name="X-Example", value="=?utf-8?b?YQ==?=")
    ]
    assert select_content_headers(message, ()) == []


def test_select_content_headers_enforces_byte_and_occurrence_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    message = BytesParser(policy=default).parsebytes(b"X-Test: \xc3\xa9\r\nX-Test: \xff\r\n\r\n")
    selected = ("x-test",)
    assert [header.value for header in select_content_headers(message, selected)] == ["é", "\ufffd"]
    monkeypatch.setattr(headers, "APPLICATION_LIMITS", replace(headers.APPLICATION_LIMITS, header_bytes=8))
    with pytest.raises(ContentHeaderLimitError, match="exceeds 8 bytes"):
        select_content_headers(message, selected)
    monkeypatch.setattr(
        headers, "APPLICATION_LIMITS", replace(headers.APPLICATION_LIMITS, header_bytes=9, content_header_occurrences=1)
    )
    with pytest.raises(ContentHeaderLimitError, match="exceed 1 occurrences"):
        select_content_headers(message, selected)
