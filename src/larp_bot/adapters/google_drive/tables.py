from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol
from urllib.parse import quote

from larp_bot.adapters.yandex_disk.repository import (
    XLSX_MIME_TYPE,
    DiskUpload,
    DiskUploader,
    UploadReadyCallback,
)

from .statistics import DriveFile

GOOGLE_CALLBACK_URLS = "google_public_urls"


class GoogleTableStorage(Protocol):
    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile: ...

    async def delete_named_xlsx(self, folder_id: str, name: str) -> None: ...

    async def named_xlsx_exists(self, folder_id: str, name: str) -> bool: ...

    async def publish_file(self, file_id: str) -> None: ...


def mirrored_table_name(disk_path: str) -> str:
    prefix = "disk:/larp-bot/"
    if not disk_path.startswith(prefix) or not disk_path.lower().endswith(".xlsx"):
        raise ValueError("mirrored table path must be an XLSX below disk:/larp-bot/")
    return disk_path.removeprefix(prefix).replace("/", " — ")


def drive_file_url(file: DriveFile) -> str:
    return file.web_view_link or f"https://drive.google.com/file/d/{quote(file.file_id, safe='')}/view"


class GoogleDriveMirroredUploader:
    """Write every XLSX to Drive before making the optional Yandex attempt."""

    def __init__(self, delegate: DiskUploader, drive: GoogleTableStorage, folder_id: str) -> None:
        if not folder_id.strip():
            raise ValueError("Google Drive folder ID must not be empty")
        self.delegate = delegate
        self.drive = drive
        self.folder_id = folder_id.strip()

    def set_iam_token(self, token: str) -> None:
        setter = getattr(self.delegate, "set_iam_token", None)
        if setter is not None:
            setter(token)

    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> dict[str, str] | None:
        xlsx_files = [file for file in files if file.content_type == XLSX_MIME_TYPE]
        mirrored_files = await asyncio.gather(
            *(
                self.drive.upsert_xlsx(
                    self.folder_id,
                    mirrored_table_name(file.disk_path),
                    file.content,
                )
                for file in xlsx_files
            )
        )
        await asyncio.gather(*(self.drive.publish_file(file.file_id) for file in mirrored_files))
        google_urls = {
            upload.role: drive_file_url(file)
            for upload, file in zip(xlsx_files, mirrored_files, strict=True)
            if upload.publish
        }
        enriched_callback = None if callback is None else dict(callback) | {GOOGLE_CALLBACK_URLS: google_urls}
        mirrored_by_role = {
            upload.role: drive_file_url(file) for upload, file in zip(xlsx_files, mirrored_files, strict=True)
        }
        prepared = [
            replace(
                file,
                google_mirrored=file.content_type == XLSX_MIME_TYPE,
                google_url=mirrored_by_role.get(file.role) if file.publish else None,
            )
            for file in files
        ]
        result = await self.delegate.submit(
            prepared,
            callback=enriched_callback,
            before_invoke=before_invoke,
        )
        if result is None:
            if callback is None:
                return {f"google:{role}": url for role, url in mirrored_by_role.items()}
            return None
        return result

    async def delete_google_table(self, disk_path: str) -> None:
        if not disk_path.lower().endswith(".xlsx"):
            return
        await self.drive.delete_named_xlsx(self.folder_id, mirrored_table_name(disk_path))

    async def google_table_exists(self, disk_path: str) -> bool:
        return await self.drive.named_xlsx_exists(self.folder_id, mirrored_table_name(disk_path))
