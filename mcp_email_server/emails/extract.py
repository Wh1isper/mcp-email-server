"""Regex-based verification/OTP code extraction (dependency-free).

Kept in its own module so it can power the ``extract_verification_code`` MCP
tool and be unit-tested in isolation. Uses only the standard library.
"""

import re

# Full-width colon (U+FF1A) is common in CJK emails. Defined via an escape so the
# source stays free of a character that is visually confusable with the ASCII colon.
_FW_COLON = "\uff1a"

# At least one explicit delimiter must follow the code keyword before the code
# itself: an ASCII or full-width colon, the word "is", or a common CJK particle.
# A mandatory delimiter prevents "verification code Your email" from capturing
# "Your" as an alphanumeric code.
_DELIM = rf"\s*(?:[:{_FW_COLON}]|\bis\b|是|为|です)[\s:{_FW_COLON}]*"

# Keyword groups that precede a verification code.
_CN_JA_KO_KW = r"验证码|认证码|确认码|認証コード|인증\s*코드|코드"
_EN_KW = r"verification\s*code|confirm(?:ation)?\s*code|security\s*code|passcode|OTP|pin\s*code"
_ALL_KW = rf"{_CN_JA_KO_KW}|{_EN_KW}"

# Keyword-guided patterns, numeric preferred then alphanumeric.
_KEYWORD_PATTERNS = [
    re.compile(rf"\bcode{_DELIM}(\d{{4,12}})\b", re.IGNORECASE),
    re.compile(rf"(?:{_ALL_KW}){_DELIM}(\d{{4,12}})\b", re.IGNORECASE),
    re.compile(rf"\bcode{_DELIM}([A-Za-z0-9]{{4,12}})\b", re.IGNORECASE),
    re.compile(rf"(?:{_ALL_KW}){_DELIM}([A-Za-z0-9]{{4,12}})\b", re.IGNORECASE),
]

# Fallback: a standalone digit run when no keyword-guided match is found.
_STANDALONE_PATTERN = re.compile(r"(?:^|\s)(\d{4,12})(?:\s|$|\.|,)", re.MULTILINE)


def _looks_like_date(digits: str) -> bool:
    """Return True if ``digits`` looks like a year (1900-2099) or a YYYYMMDD date."""
    if len(digits) == 4:
        return 1900 <= int(digits) <= 2099
    if len(digits) == 8:
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31
    return False


def extract_code(text: str) -> str | None:
    """Extract a verification/OTP code (4-12 chars) from ``text``.

    Prefers a code that follows an explicit keyword (``verification code``,
    ``OTP``, ``passcode`` and their Chinese / Japanese / Korean equivalents) with
    a mandatory delimiter. Falls back to a standalone 4-12 digit run, rejecting
    plausible years and ``YYYYMMDD`` dates to limit false positives. Returns
    ``None`` when nothing matches.
    """
    for pattern in _KEYWORD_PATTERNS:
        match = pattern.search(text)
        if match and not _looks_like_date(match.group(1)):
            return match.group(1)
    standalone = _STANDALONE_PATTERN.search(text)
    if standalone and not _looks_like_date(standalone.group(1)):
        return standalone.group(1)
    return None
