from __future__ import annotations

import asyncio
import logging
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
YANDEX_STAGING_TIMEOUT_SECONDS = 3.0
LOGGER = logging.getLogger(__name__)


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
    """Prefer Google for XLSX tables while staging a bounded Yandex copy."""

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
        google_error: Exception | None = None
        try:
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
        except Exception as exc:
            google_error = exc
            mirrored_files = []
            LOGGER.warning("google_drive_table_mirror_failed", extra={"error": str(exc)})

        mirrored_by_role = (
            {upload.role: drive_file_url(file) for upload, file in zip(xlsx_files, mirrored_files, strict=True)}
            if google_error is None
            else {}
        )
        google_urls = {
            upload.role: mirrored_by_role[upload.role] for upload in xlsx_files if upload.publish and mirrored_files
        }
        google_complete = google_error is None and bool(files) and len(xlsx_files) == len(files)
        enriched_callback = (
            None
            if callback is None
            else dict(callback) | ({GOOGLE_CALLBACK_URLS: google_urls} if mirrored_files else {})
        )
        prepared = [
            replace(
                file,
                google_mirrored=google_error is None and file.content_type == XLSX_MIME_TYPE,
                google_url=mirrored_by_role.get(file.role) if file.publish else None,
            )
            for file in files
        ]

        try:
            async with asyncio.timeout(YANDEX_STAGING_TIMEOUT_SECONDS):
                yandex_result = await self.delegate.submit(
                    prepared if google_error is None else files,
                    callback=enriched_callback,
                    before_invoke=None if google_complete else before_invoke,
                )
        except Exception as exc:
            refresh_has_persisted_links = bool(files) and all(file.overwrite for file in files)
            if not google_complete and not refresh_has_persisted_links:
                raise
            LOGGER.warning("yandex_disk_table_stage_skipped", extra={"error": str(exc)})
            yandex_result = None

        if google_complete:
            google_results = {f"google:{role}": url for role, url in mirrored_by_role.items()}
            published_results = {role: google_urls[role] for role in google_urls}
            return google_results | published_results | (yandex_result or {})
        return yandex_result

    async def delete_google_table(self, disk_path: str) -> None:
        if not disk_path.lower().endswith(".xlsx"):
            return
        await self.drive.delete_named_xlsx(self.folder_id, mirrored_table_name(disk_path))

    async def google_table_exists(self, disk_path: str) -> bool:
        return await self.drive.named_xlsx_exists(self.folder_id, mirrored_table_name(disk_path))
