from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import AsyncMock, Mock

import pytest

from mcp_email_server.adapters.reads import ClassicReadProvider
from mcp_email_server.application.reads import DownloadAttachmentCommand, GetEmailContentQuery, ReadAccountSnapshot
from mcp_email_server.emails import content_view, pgp
from mcp_email_server.emails.classic import ClassicEmailHandler
from tests import pgp_support


def _inner() -> EmailMessage:
    msg = EmailMessage(policy=policy.SMTP)
    msg["Subject"] = "Private inner subject"
    msg["From"] = "inner-forged@example.test"
    msg["Reply-To"] = "inner-forged@example.test"
    msg["References"] = "<private@example.test>"
    msg.set_content('<p>decrypted body <a href="https://example.test/tracker">label</a></p>', subtype="html")
    msg.add_attachment(b"private bytes", maintype="application", subtype="octet-stream", filename="encrypted.asc")
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "managed"])
async def test_only_stdio_decrypts_and_attachment_namespace_is_shared(
    email_settings, mock_imap, monkeypatch, pgp_envelope, mode
) -> None:
    inner = _inner()
    decrypt = AsyncMock(return_value=inner.as_bytes())
    monkeypatch.setattr(content_view, "decrypt_pgp", decrypt)
    handler = ClassicEmailHandler(email_settings)
    client = handler.incoming_client
    raw = pgp_envelope(b"ciphertext").as_bytes()
    fetch = AsyncMock(return_value=[f"1 FETCH (BODY[] {{{len(raw)}}}".encode(), raw, b")"])
    monkeypatch.setattr(client, "_connect_imap", AsyncMock(return_value=mock_imap))
    monkeypatch.setattr(client, "_fetch_email_with_formats", fetch)
    senders = AsyncMock(return_value={"1": "bob@example.test"})
    monkeypatch.setattr(client, "_batch_fetch_senders", senders)
    provider = ClassicReadProvider(handler)
    account = ReadAccountSnapshot("test_account", mode, ("bob@example.test",), True, True, True)
    query = GetEmailContentQuery("test_account", ("1",), include_headers=("Reply-To", "References"))
    get_attachment = DownloadAttachmentCommand("test_account", "1", "encrypted.asc")

    disabled = (await provider.get_content(query, account)).emails[0]
    assert disabled.pgp_status == "disabled" and disabled.body == ""
    assert (await provider.fetch_attachment(get_attachment, account)).content == b"ciphertext"
    decrypt.assert_not_awaited()

    token = content_view.local_stdio_read.set(True)
    try:
        disabled = (await provider.get_content(query, replace(account, enable_pgp_decryption=False))).emails[0]
        assert disabled.pgp_status == "disabled"
        decrypt.assert_not_awaited()
        body = (await provider.get_content(query, account)).emails[0]
        assert body.pgp_status == "decrypted"
        assert body.body.split() == ["decrypted", "body", "label"]
        assert body.links == []
        assert body.attachments == ["encrypted.asc"]
        assert body.subject == "PGP fixture" and body.sender == "bob@example.test"
        assert body.message_id == "<outer@example.test>" and body.references == "<thread@example.test>"
        assert [(item.name, item.value) for item in body.headers] == [
            ("References", "<thread@example.test>"),
            ("Reply-To", "outer-reply@example.test"),
        ]
        payload = await provider.fetch_attachment(get_attachment, account)
        assert payload.content == b"private bytes" and payload.decrypted
        decrypt.return_value = None
        unavailable = await provider.get_content(query, account)
        assert unavailable.emails[0].pgp_status == "unavailable" and unavailable.failed_ids == []
        with pytest.raises(ValueError, match="decryption_unavailable"):
            await provider.fetch_attachment(get_attachment, account)
        decrypt.reset_mock()
        fetch.reset_mock()
        senders.return_value = {"1": "blocked@example.test"}
        denied = await provider.get_content(query, account)
        assert denied.failed_ids == ["1"]
        decrypt.assert_not_awaited()
        fetch.assert_not_awaited()
    finally:
        content_view.local_stdio_read.reset(token)


@pytest.mark.asyncio
async def test_malformed_and_wrapped_envelopes_never_launch_gpg(monkeypatch, pgp_envelope) -> None:
    decrypt = AsyncMock()
    monkeypatch.setattr(content_view, "decrypt_pgp", decrypt)
    outer = pgp_envelope(b"cipher")
    wrapped = EmailMessage()
    wrapped.make_mixed()
    wrapped.attach(outer)
    assert (await content_view.read_content_view(wrapped, enable_pgp_decryption=True)).pgp_status is None
    outer.attach(EmailMessage())
    assert (await content_view.read_content_view(outer, enable_pgp_decryption=True)).pgp_status == "unavailable"
    decrypt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("armor", "named_root"), [(True, False), (False, True)])
async def test_gpg_decrypts_real_unnamed_binary_or_armored_ciphertext(
    email_settings, pgp_encrypt, pgp_envelope, armor, named_root
) -> None:
    # Neither a nested signed subtree nor a named signed root lists the detached signature.
    signed = EmailMessage(policy=policy.SMTP)
    signed.set_type("multipart/signed")
    signed.set_param("protocol", "application/pgp-signature")
    signed.attach(_inner())
    sig = EmailMessage(policy=policy.SMTP)
    sig.set_content(b"not verified", maintype="application", subtype="pgp-signature", filename="signature.asc")
    signed.attach(sig)
    if named_root:
        signed.set_param("name", "signed.mime")
        inner = signed
    else:
        inner = EmailMessage(policy=policy.SMTP)
        inner.make_mixed()
        inner.attach(signed)
    envelope = pgp_envelope(pgp_encrypt(inner.as_bytes(), armor=armor), filename=None)
    raw = envelope.as_bytes()
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    view = await content_view.read_content_view(parsed, enable_pgp_decryption=True)
    assert view.pgp_status == "decrypted"
    client = ClassicEmailHandler(email_settings).incoming_client
    result = client._parse_email_data(raw, parsed=parsed, decrypted_content=view.content)
    assert result["body"].split() == ["decrypted", "body", "label"] and result["attachments"] == ["encrypted.asc"]
    attachment = client._find_attachment_part(view.content, "encrypted.asc", signed_content_only=True)
    assert attachment is not None and client._attachment_content(attachment) == b"private bytes"
    assert client._find_attachment_part(view.content, "signature.asc", signed_content_only=True) is None


@pytest.mark.parametrize("msys", [False, True], ids=["native-windows", "msys2"])
def test_gpg_fixture_homedir_uses_selected_binary_path_syntax(monkeypatch, msys) -> None:
    gpgconf = PureWindowsPath(r"C:\Program Files\GnuPG\bin\gpgconf.exe")
    home = PureWindowsPath(r"D:\Test Data\pgp-home")
    probe_output = b"/usr/bin\n" if msys else b"C:/Program Files/GnuPG/bin\r\n"
    converted = "/d/Test Data/pgp-home"
    run = Mock(side_effect=[Mock(stdout=probe_output), Mock(stdout=converted + "\r\n")])
    monkeypatch.setattr(pgp_support.subprocess, "run", run)

    assert pgp_support.gnupg_home_path(gpgconf, home) == (converted if msys else str(home))
    assert run.call_args_list[0].args[0] == (str(gpgconf), "--list-dirs", "bindir")
    assert run.call_count == (2 if msys else 1)
    if msys:
        assert run.call_args.args[0] == (str(gpgconf.with_name("cygpath.exe")), "-u", str(home))


def test_gpg_fixture_homedir_keeps_posix_path_without_probing(monkeypatch) -> None:
    run = Mock()
    monkeypatch.setattr(pgp_support.subprocess, "run", run)
    home = PurePosixPath("/var/pgp-home")
    assert pgp_support.gnupg_home_path(PurePosixPath("/usr/bin/gpgconf"), home) == str(home)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_gpg_rejects_corrupted_or_missing_key(pgp_encrypt, pgp_home) -> None:
    plaintext = _inner().as_bytes()
    cipher = pgp_encrypt(plaintext)
    assert await pgp.decrypt_pgp(cipher) == plaintext
    assert await pgp.decrypt_pgp(cipher[:-1] + bytes([cipher[-1] ^ 1])) is None
    with pgp_home():
        assert await pgp.decrypt_pgp(cipher) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pgp_encrypt", ["fixture-passphrase"], indirect=True)
async def test_locked_gpg_key_returns_without_pinentry(pgp_encrypt) -> None:
    cipher = pgp_encrypt(_inner().as_bytes())
    assert await asyncio.wait_for(pgp.decrypt_pgp(cipher), timeout=5) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stdout", "stderr", "timeout", "cancel"])
async def test_gpg_limits_and_reaps_a_real_process(monkeypatch, case) -> None:
    monkeypatch.setattr(pgp, "_gpg_executable", lambda: Path(sys.executable))
    monkeypatch.setattr(pgp, "_OUTPUT_BYTES", 128)
    monkeypatch.setattr(pgp, "_STATUS_BYTES", 128)
    monkeypatch.setattr(pgp, "_TIMEOUT_SECONDS", 0.4 if case == "timeout" else 5)
    spawned = asyncio.Event()
    create = asyncio.create_subprocess_exec
    process = None

    async def spawn(executable, *args, **kwargs):
        nonlocal process
        assert args == (
            "--no-options",
            "--batch",
            "--no-tty",
            "--pinentry-mode",
            "error",
            "--max-output",
            "128",
            "--status-fd",
            "2",
            "--output",
            "-",
            "--decrypt",
        )
        spam = f"sys.{case}.buffer.write(b'x' * 2000000);sys.{case}.flush();" if case in {"stdout", "stderr"} else ""
        process = await create(executable, "-c", f"import sys,time;{spam}time.sleep(40)", **kwargs)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    call = asyncio.create_task(pgp.decrypt_pgp(b"ciphertext"))
    if case == "cancel":
        await asyncio.wait_for(spawned.wait(), timeout=3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=4)
    else:
        assert await asyncio.wait_for(call, timeout=4) is None
    assert process is not None and process.returncode is not None


def test_status_must_confirm_integrity() -> None:
    records = b"[GNUPG:] DECRYPTION_OKAY\n[GNUPG:] END_DECRYPTION\n"
    assert pgp._integrity_verified(b"[GNUPG:] DECRYPTION_INFO 2 9 0\n" + records)
    assert pgp._integrity_verified(b"[GNUPG:] DECRYPTION_INFO 0 9 2\n" + records)
    assert not pgp._integrity_verified(b"[GNUPG:] DECRYPTION_INFO 0 9 0\n" + records)
    assert not pgp._integrity_verified(b"[GNUPG:] DECRYPTION_INFO 2 9 0\n[GNUPG:] BADMDC\n" + records)


@pytest.mark.asyncio
async def test_cancel_reaps_gpg_even_if_helper_inherits_pipes(monkeypatch, tmp_path) -> None:
    ready = tmp_path / "child-pid"
    executable = Path(sys.executable)
    original_spawn = asyncio.create_subprocess_exec
    monkeypatch.setattr(pgp, "_gpg_executable", lambda: executable)
    source = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));time.sleep(6)"
    )

    async def spawn(command, *args, **kwargs):
        return await original_spawn(command, "-c", source, str(ready), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(pgp.decrypt_pgp(b"ciphertext"))
    try:
        async with asyncio.timeout(3):
            while not ready.is_file() or not ready.stat().st_size:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if ready.is_file() and (pid := ready.read_text()).isdigit():
            import os
            import signal
            from contextlib import suppress

            with suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGTERM)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
