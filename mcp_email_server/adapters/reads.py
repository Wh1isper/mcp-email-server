from __future__ import annotations

import asyncio
import errno
import os
import secrets
import stat
from pathlib import Path

from mcp_email_server.adapters.authority import resolve_local_account
from mcp_email_server.application.limits import APPLICATION_LIMITS
from mcp_email_server.application.management import BindingRole
from mcp_email_server.application.metadata import RuntimeMode
from mcp_email_server.application.reads import (
    AttachmentPayload,
    DownloadAttachmentCommand,
    GetEmailContentQuery,
    ListMailboxesQuery,
    ReadAccountSnapshot,
    ReadProviderAccess,
    ReadProviderError,
)
from mcp_email_server.config import EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler
from mcp_email_server.emails.models import (
    EmailBodyResponse,
    EmailContentBatchResponse,
    MailboxInfo,
)


class ClassicReadProvider:
    """Adapt classic IMAP reads without consulting global settings."""

    def __init__(self, handler: ClassicEmailHandler) -> None:
        self._handler = handler

    async def list_mailboxes(self, query: ListMailboxesQuery) -> list[MailboxInfo]:
        try:
            return await self._handler.list_mailboxes(query.pattern, query.reference)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ReadProviderError("provider_failure: mailbox discovery failed") from None

    async def get_content(
        self,
        query: GetEmailContentQuery,
        account: ReadAccountSnapshot,
    ) -> EmailContentBatchResponse:
        emails: list[EmailBodyResponse] = []
        failed_ids: list[str] = []
        aggregate_body_bytes = 0
        aggregate_header_bytes = 0
        for email_id in query.email_ids:
            try:
                email_data = await self._handler.incoming_client.get_email_body_by_id(
                    email_id,
                    query.mailbox,
                    False,
                    allowed_senders=list(account.allowed_senders),
                    body_offset=query.body_offset,
                    max_body_length=query.max_body_length,
                )
                if not email_data:
                    failed_ids.append(email_id)
                    continue
                email = EmailBodyResponse(
                    email_id=email_data["email_id"],
                    message_id=email_data.get("message_id"),
                    in_reply_to=email_data.get("in_reply_to"),
                    references=email_data.get("references"),
                    subject=email_data["subject"],
                    sender=email_data["from"],
                    recipients=email_data["to"],
                    date=email_data["date"],
                    body=email_data["body"],
                    attachments=email_data["attachments"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failed_ids.append(email_id)
                continue
            body_bytes = len(email.body.encode("utf-8"))
            if body_bytes > APPLICATION_LIMITS.body_bytes:
                raise ReadProviderError(f"limit_exceeded: an email body exceeds {APPLICATION_LIMITS.body_bytes} bytes")
            aggregate_body_bytes += body_bytes
            if aggregate_body_bytes > APPLICATION_LIMITS.aggregate_body_bytes:
                raise ReadProviderError(
                    f"limit_exceeded: email bodies exceed {APPLICATION_LIMITS.aggregate_body_bytes} bytes in total"
                )
            thread_header_sizes = [
                len(value.encode("utf-8")) for value in (email.in_reply_to, email.references) if value is not None
            ]
            if any(size > APPLICATION_LIMITS.header_bytes for size in thread_header_sizes):
                raise ReadProviderError(
                    f"limit_exceeded: an email thread header exceeds {APPLICATION_LIMITS.header_bytes} bytes"
                )
            aggregate_header_bytes += (
                len(email.email_id.encode("utf-8"))
                + len((email.message_id or "").encode("utf-8"))
                + len((email.in_reply_to or "").encode("utf-8"))
                + len((email.references or "").encode("utf-8"))
                + len(email.subject.encode("utf-8"))
                + len(email.sender.encode("utf-8"))
                + sum(len(value.encode("utf-8")) for value in email.recipients)
                + sum(len(value.encode("utf-8")) for value in email.attachments)
            )
            if aggregate_header_bytes > APPLICATION_LIMITS.aggregate_header_bytes:
                raise ReadProviderError(
                    f"limit_exceeded: email headers exceed {APPLICATION_LIMITS.aggregate_header_bytes} bytes in total"
                )
            emails.append(email)
        return EmailContentBatchResponse(
            emails=emails,
            requested_count=len(query.email_ids),
            retrieved_count=len(emails),
            failed_ids=failed_ids,
        )

    async def fetch_attachment(
        self,
        command: DownloadAttachmentCommand,
        account: ReadAccountSnapshot,
    ) -> AttachmentPayload:
        try:
            result = await self._handler.incoming_client.fetch_attachment(
                command.email_id,
                command.attachment_name,
                command.mailbox,
                allowed_senders=list(account.allowed_senders),
            )
        except asyncio.CancelledError:
            raise
        except (PermissionError, ValueError):
            raise
        except Exception:
            raise ReadProviderError("provider_failure: attachment download failed") from None
        content = result["content"]
        if not isinstance(content, bytes):
            raise ReadProviderError("provider_failure: attachment content is invalid")
        return AttachmentPayload(
            email_id=result["email_id"],
            attachment_name=result["attachment_name"],
            mime_type=result["mime_type"],
            content=content,
        )


_SECURE_ATTACHMENT_WRITES_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "geteuid")
    and all(operation in os.supports_dir_fd for operation in (os.mkdir, os.open, os.rename, os.stat, os.unlink))
    and os.stat in os.supports_follow_symlinks
)


class LocalArtifactWriter:
    """Write an exact caller path only when pinned POSIX traversal is available."""

    @staticmethod
    def _validate_directory(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("Attachment destination parent is unsafe")
        writable_by_others = stat.S_IMODE(metadata.st_mode) & 0o022
        if writable_by_others and not (metadata.st_mode & stat.S_ISVTX):
            raise PermissionError("Attachment destination parent permissions are unsafe")
        current_uid = os.geteuid()
        if metadata.st_uid not in {0, current_uid}:
            raise PermissionError("Attachment destination parent ownership is unsafe")

    @staticmethod
    def _open_posix_parent(path: Path) -> int:
        """Open every parent through pinned no-follow directory descriptors."""
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_descriptor = os.open(path.anchor, directory_flags)
        try:
            LocalArtifactWriter._validate_directory(directory_descriptor)
            relative_parent = path.parent.relative_to(Path(path.anchor))
            for component in relative_parent.parts:
                child_descriptor = -1
                try:
                    try:
                        child_descriptor = os.open(
                            component,
                            directory_flags,
                            dir_fd=directory_descriptor,
                        )
                    except FileNotFoundError:
                        os.mkdir(component, mode=0o700, dir_fd=directory_descriptor)
                        child_descriptor = os.open(
                            component,
                            directory_flags,
                            dir_fd=directory_descriptor,
                        )
                    LocalArtifactWriter._validate_directory(child_descriptor)
                except PermissionError:
                    if child_descriptor >= 0:
                        os.close(child_descriptor)
                    raise
                except OSError as exc:
                    if child_descriptor >= 0:
                        os.close(child_descriptor)
                    raise PermissionError("Attachment destination parent is unsafe") from exc
                os.close(directory_descriptor)
                directory_descriptor = child_descriptor
            result = directory_descriptor
            directory_descriptor = -1
            return result
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

    @staticmethod
    def _validate_existing_target(parent_descriptor: int, name: str) -> None:
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("Attachment destination must be a regular file")
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
            raise PermissionError("Attachment destination identity is unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("Attachment destination permissions are unsafe")

    @staticmethod
    def _validate_written_file(metadata: os.stat_result, *, expected_size: int) -> None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PermissionError("Attachment destination must be a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("Attachment destination identity is unsafe")
        if metadata.st_size != expected_size:
            raise PermissionError("Attachment destination size verification failed")

    @staticmethod
    def _write_posix(path: Path, content: bytes) -> None:
        parent_descriptor = LocalArtifactWriter._open_posix_parent(path)
        temporary_name = f".mcp-email-attachment-{secrets.token_hex(16)}.tmp"
        descriptor = -1
        temporary_identity: tuple[int, int] | None = None
        try:
            LocalArtifactWriter._validate_existing_target(parent_descriptor, path.name)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            temporary_identity = (opened.st_dev, opened.st_ino)
            LocalArtifactWriter._validate_written_file(opened, expected_size=0)
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
                written = os.fstat(destination.fileno())
            LocalArtifactWriter._validate_written_file(written, expected_size=len(content))
            # Re-check overwrite authority immediately before the atomic replace.
            LocalArtifactWriter._validate_existing_target(parent_descriptor, path.name)
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            final = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (final.st_dev, final.st_ino) != temporary_identity:
                raise PermissionError("Attachment destination identity changed during write")
            LocalArtifactWriter._validate_written_file(final, expected_size=len(content))
            os.fsync(parent_descriptor)
            temporary_identity = None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_identity is not None:
                try:
                    current = os.stat(temporary_name, dir_fd=parent_descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == temporary_identity:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
            os.close(parent_descriptor)

    @staticmethod
    def _write(save_path: str, payload: AttachmentPayload) -> str:
        path = Path(os.path.abspath(Path(save_path).expanduser()))
        if not _SECURE_ATTACHMENT_WRITES_SUPPORTED:
            raise PermissionError(
                "Attachment download is unavailable because this platform cannot enforce secure destination traversal"
            )
        try:
            LocalArtifactWriter._write_posix(path, payload.content)
        except PermissionError:
            raise
        except OSError as exc:
            if exc.errno in {errno.EISDIR, errno.ELOOP, errno.ENODEV, errno.ENXIO}:
                raise PermissionError("Attachment destination must be a regular file") from exc
            raise PermissionError("Attachment destination could not be written") from exc
        return path.as_posix()

    async def write(self, save_path: str, payload: AttachmentPayload) -> str:
        return await asyncio.to_thread(self._write, save_path, payload)


class LocalReadBackend:
    """Resolve fresh selected authority and construct read providers."""

    @staticmethod
    def _resolve(
        account_name: str,
        *,
        roles: tuple[BindingRole, ...] = (),
        expected_mode: RuntimeMode | None = None,
    ) -> tuple[EmailSettings, ReadAccountSnapshot]:
        resolved = (
            resolve_local_account(account_name, roles=roles, expected_mode=expected_mode)
            if roles
            else resolve_local_account(account_name, expected_mode=expected_mode)
        )
        return (
            resolved.account,
            ReadAccountSnapshot(
                account_name=resolved.account.account_name,
                mode=resolved.mode,
                allowed_senders=tuple(resolved.settings.allowed_senders),
                enable_attachment_download=resolved.settings.enable_attachment_download,
            ),
        )

    def resolve(
        self,
        account_name: str,
        *,
        expected_mode: RuntimeMode | None = None,
    ) -> ReadAccountSnapshot:
        return self._resolve(account_name, expected_mode=expected_mode)[1]

    def open(self, account_name: str, *, expected_mode: RuntimeMode) -> ReadProviderAccess:
        account, snapshot = self._resolve(
            account_name,
            roles=("incoming",),
            expected_mode=expected_mode,
        )
        return ReadProviderAccess(
            account=snapshot,
            provider=ClassicReadProvider(ClassicEmailHandler(account)),
        )
