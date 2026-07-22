from __future__ import annotations

import asyncio
import errno
import os
import stat
from pathlib import Path

from mcp_email_server import config as config_module
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
from mcp_email_server.bootstrap import process_bootstrap
from mcp_email_server.config import EmailSettings, ProviderSettings, get_settings
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
                emails.append(
                    EmailBodyResponse(
                        email_id=email_data["email_id"],
                        message_id=email_data.get("message_id"),
                        subject=email_data["subject"],
                        sender=email_data["from"],
                        recipients=email_data["to"],
                        date=email_data["date"],
                        body=email_data["body"],
                        attachments=email_data["attachments"],
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failed_ids.append(email_id)
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


class LocalArtifactWriter:
    """Write only the explicitly requested artifact path with symlink protection."""

    @staticmethod
    def _prepare_fallback_destination(path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in (path.parent, *path.parent.parents):
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise PermissionError("Attachment destination parent is unsafe")
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("Attachment destination must be a regular file")

    @staticmethod
    def _open_posix_destination(path: Path) -> int:
        """Open through pinned no-follow directory descriptors."""
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_descriptor = os.open(path.anchor, directory_flags)
        try:
            relative_parent = path.parent.relative_to(Path(path.anchor))
            for component in relative_parent.parts:
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
                except OSError as exc:
                    raise PermissionError("Attachment destination parent is unsafe") from exc
                os.close(directory_descriptor)
                directory_descriptor = child_descriptor
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                return os.open(path.name, flags, 0o600, dir_fd=directory_descriptor)
            except OSError as exc:
                if exc.errno in {errno.EISDIR, errno.ELOOP, errno.ENODEV, errno.ENXIO}:
                    raise PermissionError("Attachment destination must be a regular file") from exc
                raise
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _prepare_open_file(descriptor: int) -> None:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("Attachment destination must be a regular file")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)

    @staticmethod
    def _write(save_path: str, payload: AttachmentPayload) -> str:
        path = Path(os.path.abspath(Path(save_path).expanduser()))
        try:
            if os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
                descriptor = LocalArtifactWriter._open_posix_destination(path)
            else:
                LocalArtifactWriter._prepare_fallback_destination(path)
                flags = os.O_WRONLY | os.O_CREAT
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                if hasattr(os, "O_NONBLOCK"):
                    flags |= os.O_NONBLOCK
                descriptor = os.open(path, flags, 0o600)
            try:
                LocalArtifactWriter._prepare_open_file(descriptor)
                with os.fdopen(descriptor, "wb") as destination:
                    descriptor = -1
                    destination.write(payload.content)
                    destination.flush()
                    os.fsync(destination.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except PermissionError:
            raise
        except OSError as exc:
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
        expected_mode: RuntimeMode | None = None,
    ) -> tuple[EmailSettings, ReadAccountSnapshot]:
        mode = process_bootstrap(config_module.CONFIG_PATH).mode
        if expected_mode is not None and mode != expected_mode:
            raise RuntimeError("Configuration mode changed; restart required")
        settings = get_settings(reload=mode == "managed")
        account = settings.get_account(account_name)
        if isinstance(account, ProviderSettings):
            raise NotImplementedError
        if not isinstance(account, EmailSettings):
            account_names = [item.account_name for item in settings.get_accounts()]
            raise ValueError(  # noqa: TRY004 - preserve public unknown-account compatibility
                f"Account {account_name} not found, available accounts: {account_names}"
            )
        return (
            account,
            ReadAccountSnapshot(
                account_name=account.account_name,
                mode=mode,
                allowed_senders=tuple(settings.allowed_senders),
                enable_attachment_download=settings.enable_attachment_download,
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
        account, snapshot = self._resolve(account_name, expected_mode=expected_mode)
        return ReadProviderAccess(
            account=snapshot,
            provider=ClassicReadProvider(ClassicEmailHandler(account)),
        )
