from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from larp_bot.adapters.google_drive import tables
from larp_bot.adapters.google_drive.statistics import DriveFile
from larp_bot.adapters.google_drive.tables import (
    GOOGLE_CALLBACK_URLS,
    GoogleDriveMirroredUploader,
    GoogleDriveTableStorage,
)
from larp_bot.adapters.yandex_disk.repository import (
    PPTX_MIME_TYPE,
    XLSX_MIME_TYPE,
    DiskUpload,
    UploadReadyCallback,
)


class MemoryTableDrive:
    def __init__(self) -> None:
        self.contents: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.published: list[str] = []

    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        self.contents[(folder_id, name)] = content
        identifier = name.replace(" — ", "-")
        return DriveFile(
            id=identifier,
            name=name,
            mimeType=XLSX_MIME_TYPE,
            webViewLink=f"https://drive.example/{identifier}",
        )

    async def delete_named_xlsx(self, folder_id: str, name: str) -> None:
        self.contents.pop((folder_id, name), None)
        self.deleted.append((folder_id, name))

    async def named_xlsx_exists(self, folder_id: str, name: str) -> bool:
        return (folder_id, name) in self.contents

    async def publish_file(self, file_id: str) -> None:
        self.published.append(file_id)


class RecordingUploader:
    def __init__(self) -> None:
        self.files: Sequence[DiskUpload] = ()
        self.callback: dict[str, object] | None = None

    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> None:
        self.files = files
        self.callback = callback
        if before_invoke is not None:
            await before_invoke()


class FailingUploader(RecordingUploader):
    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> None:
        await super().submit(files, callback=callback, before_invoke=before_invoke)
        raise RuntimeError("Yandex is unavailable")


class FailingDrive(MemoryTableDrive):
    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        del folder_id, name, content
        raise RuntimeError("Google Drive is unavailable")


class HangingUploader(RecordingUploader):
    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> None:
        await super().submit(files, callback=callback, before_invoke=before_invoke)
        await asyncio.Event().wait()


class DirectTableDrive:
    def __init__(self, files: dict[str, DriveFile] | None = None) -> None:
        self.files = files or {}
        self.replacements: list[tuple[str, bytes]] = []

    async def find_named_files(self, folder_id: str, name: str) -> list[DriveFile]:
        assert folder_id == "tables-folder"
        file = self.files.get(name)
        return [] if file is None else [file]

    async def replace(self, file_id: str, content: bytes) -> None:
        self.replacements.append((file_id, content))


class OwnerTableDrive(MemoryTableDrive):
    def __init__(self, direct: DirectTableDrive) -> None:
        super().__init__()
        self.direct = direct
        self.active_creations = 0
        self.maximum_active_creations = 0

    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        self.active_creations += 1
        self.maximum_active_creations = max(self.maximum_active_creations, self.active_creations)
        await asyncio.sleep(0)
        file = await super().upsert_xlsx(folder_id, name, content)
        self.direct.files[name] = file
        self.active_creations -= 1
        return file


@pytest.mark.asyncio
async def test_existing_google_table_is_updated_without_the_owner_gateway() -> None:
    name = "events — event-id — master.xlsx"
    existing = DriveFile(
        id="existing-file",
        name=name,
        mimeType=XLSX_MIME_TYPE,
        webViewLink="https://drive.example/existing-file",
    )
    direct = DirectTableDrive({name: existing})
    owner = OwnerTableDrive(direct)
    storage = GoogleDriveTableStorage(direct, owner)

    result = await storage.upsert_xlsx("tables-folder", name, b"updated")

    assert result == existing
    assert direct.replacements == [("existing-file", b"updated")]
    assert owner.contents == {}


@pytest.mark.asyncio
async def test_missing_google_tables_are_created_serially_by_the_owner_gateway() -> None:
    direct = DirectTableDrive()
    owner = OwnerTableDrive(direct)
    storage = GoogleDriveTableStorage(direct, owner)

    master, public = await asyncio.gather(
        storage.upsert_xlsx("tables-folder", "events — event-id — master.xlsx", b"master"),
        storage.upsert_xlsx("tables-folder", "events — event-id — public.xlsx", b"public"),
    )

    assert master.name.endswith("master.xlsx")
    assert public.name.endswith("public.xlsx")
    assert owner.maximum_active_creations == 1


@pytest.mark.asyncio
async def test_all_xlsx_files_are_mirrored_before_the_best_effort_yandex_job() -> None:
    drive = MemoryTableDrive()
    delegate = RecordingUploader()
    uploader = GoogleDriveMirroredUploader(delegate, drive, "tables-folder")
    pending_saw_drive_files = False

    async def pending() -> None:
        nonlocal pending_saw_drive_files
        pending_saw_drive_files = len(drive.contents) == 2

    await uploader.submit(
        [
            DiskUpload(
                "master",
                "disk:/larp-bot/events/event-id/master.xlsx",
                b"master",
                XLSX_MIME_TYPE,
                False,
                True,
            ),
            DiskUpload(
                "public",
                "disk:/larp-bot/events/event-id/public.xlsx",
                b"public",
                XLSX_MIME_TYPE,
                True,
                False,
            ),
            DiskUpload(
                "achievement",
                "disk:/larp-bot/achievements/event-id.pptx",
                b"slides",
                PPTX_MIME_TYPE,
                False,
                True,
            ),
        ],
        callback={"kind": "event_created"},
        before_invoke=pending,
    )

    assert pending_saw_drive_files
    assert drive.contents == {
        ("tables-folder", "events — event-id — master.xlsx"): b"master",
        ("tables-folder", "events — event-id — public.xlsx"): b"public",
    }
    assert delegate.callback is not None
    assert delegate.callback[GOOGLE_CALLBACK_URLS] == {"master": "https://drive.example/events-event-id-master.xlsx"}
    master, public, achievement = delegate.files
    assert master.google_mirrored and master.google_url is not None
    assert public.google_mirrored and public.google_url is None
    assert not achievement.google_mirrored and achievement.google_url is None
    assert drive.published == ["events-event-id-master.xlsx", "events-event-id-public.xlsx"]

    assert await uploader.google_table_exists(master.disk_path)
    await uploader.delete_google_table(master.disk_path)
    assert not await uploader.google_table_exists(master.disk_path)


@pytest.mark.asyncio
async def test_background_refresh_returns_google_link_for_legacy_event_backfill() -> None:
    drive = MemoryTableDrive()
    uploader = GoogleDriveMirroredUploader(RecordingUploader(), drive, "tables-folder")

    result = await uploader.submit(
        [
            DiskUpload(
                "public",
                "disk:/larp-bot/events/legacy/public.xlsx",
                b"refreshed",
                XLSX_MIME_TYPE,
                True,
                False,
            )
        ]
    )

    assert result == {"google:public": "https://drive.example/events-legacy-public.xlsx"}
    assert drive.published == ["events-legacy-public.xlsx"]


@pytest.mark.asyncio
async def test_all_xlsx_job_returns_google_links_without_waiting_for_yandex_completion() -> None:
    drive = MemoryTableDrive()
    delegate = RecordingUploader()
    uploader = GoogleDriveMirroredUploader(delegate, drive, "tables-folder")
    pending_called = False

    async def pending() -> None:
        nonlocal pending_called
        pending_called = True

    result = await uploader.submit(
        [
            DiskUpload(
                "master",
                "disk:/larp-bot/events/event-id/master.xlsx",
                b"master",
                XLSX_MIME_TYPE,
                False,
                True,
            ),
            DiskUpload(
                "public",
                "disk:/larp-bot/events/event-id/public.xlsx",
                b"public",
                XLSX_MIME_TYPE,
                False,
                True,
            ),
        ],
        callback={"kind": "event_created"},
        before_invoke=pending,
    )

    assert result == {
        "google:master": "https://drive.example/events-event-id-master.xlsx",
        "google:public": "https://drive.example/events-event-id-public.xlsx",
        "master": "https://drive.example/events-event-id-master.xlsx",
        "public": "https://drive.example/events-event-id-public.xlsx",
    }
    assert [file.role for file in delegate.files] == ["master", "public"]
    assert delegate.callback is not None
    assert delegate.callback[GOOGLE_CALLBACK_URLS] == {
        "master": "https://drive.example/events-event-id-master.xlsx",
        "public": "https://drive.example/events-event-id-public.xlsx",
    }
    assert not pending_called


@pytest.mark.asyncio
async def test_yandex_failure_cannot_fail_an_authoritative_google_table_upload() -> None:
    drive = MemoryTableDrive()
    uploader = GoogleDriveMirroredUploader(FailingUploader(), drive, "tables-folder")

    result = await uploader.submit(
        [
            DiskUpload(
                "pass",
                "disk:/larp-bot/passes/event-id.xlsx",
                b"passes",
                XLSX_MIME_TYPE,
                True,
                True,
            )
        ],
        callback={"kind": "event_tables"},
    )

    assert result == {
        "google:pass": "https://drive.example/passes-event-id.xlsx",
        "pass": "https://drive.example/passes-event-id.xlsx",
    }


@pytest.mark.asyncio
async def test_google_failure_prevents_yandex_only_table_delivery() -> None:
    delegate = RecordingUploader()
    uploader = GoogleDriveMirroredUploader(delegate, FailingDrive(), "tables-folder")
    pending_called = False

    async def pending() -> None:
        nonlocal pending_called
        pending_called = True

    with pytest.raises(RuntimeError, match="Google Drive is unavailable"):
        await uploader.submit(
            [
                DiskUpload(
                    "public",
                    "disk:/larp-bot/events/event-id/public.xlsx",
                    b"public",
                    XLSX_MIME_TYPE,
                    True,
                    True,
                )
            ],
            callback={"kind": "event_tables"},
            before_invoke=pending,
        )

    assert delegate.files == ()
    assert delegate.callback is None
    assert not pending_called


@pytest.mark.asyncio
async def test_non_table_yandex_failure_is_not_hidden() -> None:
    uploader = GoogleDriveMirroredUploader(FailingUploader(), FailingDrive(), "tables-folder")

    with pytest.raises(RuntimeError, match="Yandex is unavailable"):
        await uploader.submit(
            [
                DiskUpload(
                    "achievement",
                    "disk:/larp-bot/achievements/event-id.pptx",
                    b"achievement",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    True,
                    False,
                )
            ]
        )


@pytest.mark.asyncio
async def test_slow_yandex_staging_does_not_delay_a_google_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tables, "YANDEX_STAGING_TIMEOUT_SECONDS", 0.01)
    uploader = GoogleDriveMirroredUploader(HangingUploader(), MemoryTableDrive(), "tables-folder")

    result = await uploader.submit(
        [
            DiskUpload(
                "pass",
                "disk:/larp-bot/passes/event-id.xlsx",
                b"passes",
                XLSX_MIME_TYPE,
                True,
                True,
            )
        ],
        callback={"kind": "event_tables"},
    )

    assert result == {
        "google:pass": "https://drive.example/passes-event-id.xlsx",
        "pass": "https://drive.example/passes-event-id.xlsx",
    }
