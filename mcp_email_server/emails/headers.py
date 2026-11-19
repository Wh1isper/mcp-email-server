"""Select bounded header observations from an already parsed message."""

import re
from email.message import Message

from mcp_email_server.application.limits import APPLICATION_LIMITS, ApplicationLimits
from mcp_email_server.emails.models import EmailHeaderObservation

DEFAULT_CONTENT_HEADERS = ("Authentication-Results", "Received", "Reply-To")
HEADER_NAME_PATTERN = r"^[!-9;-~]+$"
_FOLD = re.compile(r"(?:\r\n|\r|\n)(?=[ \t])")


class ContentHeaderLimitError(ValueError):
    """The selected header observations cannot be returned intact."""


def _check_count(count: int, limits: ApplicationLimits) -> None:
    if count > limits.content_header_occurrences:
        raise ContentHeaderLimitError(
            f"limit_exceeded: selected email headers exceed {limits.content_header_occurrences} occurrences per message"
        )


def _header_size(header: EmailHeaderObservation, limits: ApplicationLimits) -> int:
    size = len(header.name.encode("utf-8")) + len(header.value.encode("utf-8"))
    if size > limits.header_bytes:
        raise ContentHeaderLimitError(f"limit_exceeded: a selected email header exceeds {limits.header_bytes} bytes")
    return size


def content_headers_size(headers: list[EmailHeaderObservation], limits: ApplicationLimits) -> int:
    _check_count(len(headers), limits)
    return sum(_header_size(header, limits) for header in headers)


def select_content_headers(message: Message, names: tuple[str, ...]) -> list[EmailHeaderObservation]:
    if not names:
        return []
    requested = {name.lower() for name in names}
    limits = APPLICATION_LIMITS
    observed: list[EmailHeaderObservation] = []
    total = 0
    for name, raw_value in message.raw_items():
        if name.lower() not in requested:
            continue
        _check_count(len(observed) + 1, limits)
        # BytesParser represents non-ASCII bytes with surrogate escapes. Return
        # valid UTF-8 without RFC 2047 decoding or header-type rewriting.
        value = _FOLD.sub("", raw_value.encode("utf-8", "surrogateescape").decode("utf-8", "replace"))
        header = EmailHeaderObservation(name=name, value=value)
        total += _header_size(header, limits)
        if total > limits.aggregate_header_bytes:
            raise ContentHeaderLimitError(
                f"limit_exceeded: email headers exceed {limits.aggregate_header_bytes} bytes in total"
            )
        observed.append(header)
    return observed
