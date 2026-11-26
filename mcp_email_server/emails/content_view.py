"""Request-local MIME content views; the original envelope remains authoritative."""

from contextvars import ContextVar
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from typing import Literal

from mcp_email_server.emails.pgp import decrypt_pgp

PGPStatus = Literal["decrypted", "disabled", "unavailable"]

# Only the bounded stdio adapter sets this in the server task and its children.
local_stdio_read: ContextVar[bool] = ContextVar("local_stdio_read", default=False)


@dataclass(frozen=True)
class ContentView:
    content: Message
    pgp_status: PGPStatus | None = None


def _encrypted_payload(envelope: Message) -> bytes | None:
    parts = envelope.get_payload()
    if envelope.defects or not isinstance(parts, list) or len(parts) != 2:
        return None
    control, encrypted = parts
    if not isinstance(control, Message) or not isinstance(encrypted, Message):
        return None
    for part, expected_type in ((control, "application/pgp-encrypted"), (encrypted, "application/octet-stream")):
        if part.get_content_type() != expected_type or part.defects:
            return None
        if len(part.get_all("Content-Type", [])) != 1 or len(part.get_all("Content-Transfer-Encoding", [])) > 1:
            return None
    version = control.get_payload(decode=True)
    if not isinstance(version, bytes) or version.strip() != b"Version: 1" or control.defects:
        return None
    payload = encrypted.get_payload(decode=True)
    return payload if isinstance(payload, bytes) and not encrypted.defects else None


async def read_content_view(envelope: Message, *, enable_pgp_decryption: bool) -> ContentView:
    protocol = envelope.get_param("protocol")
    if (
        envelope.get_content_type() != "multipart/encrypted"
        or not isinstance(protocol, str)
        or protocol.lower() != "application/pgp-encrypted"
    ):
        return ContentView(envelope)
    if not enable_pgp_decryption:
        return ContentView(envelope, "disabled")
    ciphertext = _encrypted_payload(envelope) if len(envelope.get_all("Content-Type", [])) == 1 else None
    plaintext = await decrypt_pgp(ciphertext) if ciphertext else None
    if plaintext is not None:
        try:
            root = BytesParser(policy=default).parsebytes(plaintext)
        except RecursionError:
            pass
        else:
            if not root.defects:
                return ContentView(root, "decrypted")
    return ContentView(envelope, "unavailable")
