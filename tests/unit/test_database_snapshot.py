from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from larp_bot.adapters.database_snapshot import (
    GOOGLE_BOOTSTRAP_NAME,
    GOOGLE_SNAPSHOT_NAME,
    NTFY_URL,
    YANDEX_SNAPSHOT_PATH,
    GoogleDriveSnapshotUploader,
    NtfySnapshotAlertPublisher,
    YandexDiskSnapshotUploader,
)
from larp_bot.adapters.google_drive.statistics import JSON_MIME_TYPE, XLSX_MIME_TYPE, DriveFile
from larp_bot.application.database_snapshot import DatabaseSnapshotError, DatabaseSnapshotService, render_snapshot
from larp_bot.migration.canonical import build_document

SNAPSHOT_TIME = datetime(2026, 10, 13, 9, 8, 7, 654321, tzinfo=UTC)


def empty_document() -> Any:
    return build_document(
        {
            "tg_users": [],
            "vk_users": [],
            "events": [],
            "event_leaders": [],
            "registrations": [],
        },
        source="dynamodb",
        exported_at=datetime(2026, 10, 13, tzinfo=UTC),
    )


def test_snapshot_is_the_migration_json_format_with_only_utc_plus_three_export_time() -> None:
    migration = empty_document()

    content, timestamp = render_snapshot(migration, exported_at=SNAPSHOT_TIME)

    snapshot = json.loads(content)
    assert timestamp == "2026-10-13T12:08:07.654321+03:00"
    assert snapshot == migration | {"exported_at": timestamp}
    assert content == (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


class FakeExporter:
    async def export_document(self) -> Any:
        return empty_document()


class FakeDestination:
    def __init__(self, url: str, *, error: Exception | None = None) -> None:
        self.url = url
        self.error = error
        self.expected_urls: list[str | None] = []
        self.contents: list[bytes] = []

    async def upload(self, content: bytes, *, expected_url: str | None) -> str:
        self.expected_urls.append(expected_url)
        self.contents.append(content)
        if self.error is not None:
            raise self.error
        return self.url


class FakeLinks:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.created: dict[str, str] = {}

    async def get(self, destination: str) -> str | None:
        return self.values.get(destination)

    async def create(self, destination: str, url: str) -> None:
        self.created[destination] = url


class FakeAlerts:
    def __init__(self) -> None:
        self.failures: list[tuple[tuple[str, ...], str]] = []

    async def upload_failed(self, destinations: tuple[str, ...], exported_at: str) -> None:
        self.failures.append((destinations, exported_at))


@pytest.mark.asyncio
async def test_snapshot_uploads_both_destinations_and_persists_first_links() -> None:
    google = FakeDestination("https://drive.example/stable")
    yandex = FakeDestination("https://disk.example/stable")
    links = FakeLinks()
    alerts = FakeAlerts()
    service = DatabaseSnapshotService(
        FakeExporter(),
        {"google_drive": google, "yandex_disk": yandex},
        links,
        alerts,
        clock=lambda: SNAPSHOT_TIME,
    )

    result = await service.run()

    assert result.exported_at.endswith("+03:00")
    assert result.urls == {
        "google_drive": "https://drive.example/stable",
        "yandex_disk": "https://disk.example/stable",
    }
    assert google.contents == yandex.contents
    assert links.created == result.urls
    assert alerts.failures == []


@pytest.mark.asyncio
async def test_one_destination_failure_does_not_skip_the_other_and_sends_ntfy_alarm() -> None:
    google = FakeDestination("https://drive.example/stable", error=RuntimeError("unavailable"))
    yandex = FakeDestination("https://disk.example/stable")
    links = FakeLinks()
    alerts = FakeAlerts()
    service = DatabaseSnapshotService(
        FakeExporter(),
        {"google_drive": google, "yandex_disk": yandex},
        links,
        alerts,
        clock=lambda: SNAPSHOT_TIME,
    )

    with pytest.raises(DatabaseSnapshotError, match="google_drive"):
        await service.run()

    assert len(google.contents) == len(yandex.contents) == 1
    assert links.created == {"yandex_disk": "https://disk.example/stable"}
    assert alerts.failures == [(("google_drive",), "2026-10-13T12:08:07.654321+03:00")]


@pytest.mark.asyncio
async def test_optional_yandex_failure_alarms_without_blocking_google_readiness() -> None:
    google = FakeDestination("https://drive.example/stable")
    yandex = FakeDestination("https://disk.example/stable", error=RuntimeError("unavailable"))
    links = FakeLinks({"yandex_disk": "https://disk.example/stable"})
    alerts = FakeAlerts()
    service = DatabaseSnapshotService(
        FakeExporter(),
        {"google_drive": google, "yandex_disk": yandex},
        links,
        alerts,
        required_destinations=frozenset({"google_drive"}),
        clock=lambda: SNAPSHOT_TIME,
    )

    result = await service.run()

    assert result.urls == {
        "google_drive": "https://drive.example/stable",
        "yandex_disk": "https://disk.example/stable",
    }
    assert links.created == {"google_drive": "https://drive.example/stable"}
    assert alerts.failures == [(("yandex_disk",), "2026-10-13T12:08:07.654321+03:00")]


@pytest.mark.asyncio
async def test_changed_destination_link_fails_closed_and_alarms() -> None:
    links = FakeLinks({"google_drive": "https://drive.example/original"})
    alerts = FakeAlerts()
    service = DatabaseSnapshotService(
        FakeExporter(),
        {"google_drive": FakeDestination("https://drive.example/replacement")},
        links,
        alerts,
        clock=lambda: SNAPSHOT_TIME,
    )

    with pytest.raises(DatabaseSnapshotError, match="google_drive"):
        await service.run()

    assert links.created == {}
    assert alerts.failures[0][0] == ("google_drive",)


class FakeDrive:
    def __init__(self, files: list[DriveFile]) -> None:
        self.files = files
        self.replacements: list[tuple[str, bytes, str]] = []
        self.metadata_replacements: list[tuple[str, str, str, bytes]] = []
        self.published: list[str] = []

    async def find_named_files(self, folder_id: str, name: str) -> list[DriveFile]:
        assert folder_id == "folder"
        assert name == GOOGLE_SNAPSHOT_NAME
        return self.files

    async def replace_media(self, file_id: str, content: bytes, mime_type: str) -> None:
        self.replacements.append((file_id, content, mime_type))

    async def replace_with_metadata(self, file_id: str, *, name: str, mime_type: str, content: bytes) -> DriveFile:
        self.metadata_replacements.append((file_id, name, mime_type, content))
        return DriveFile(id=file_id, name=name, mimeType=mime_type)

    async def publish_file(self, file_id: str) -> None:
        self.published.append(file_id)


class FakeBootstrap:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []

    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        self.uploads.append((folder_id, name, content))
        return DriveFile(id="permanent-id", name=name, mimeType=XLSX_MIME_TYPE)


@pytest.mark.asyncio
async def test_google_snapshot_bootstraps_owner_file_then_keeps_its_id() -> None:
    drive = FakeDrive([])
    bootstrap = FakeBootstrap()
    uploader = GoogleDriveSnapshotUploader(drive, bootstrap, "folder")  # type: ignore[arg-type]

    url = await uploader.upload(b'{"snapshot":true}', expected_url=None)

    assert url == "https://drive.google.com/file/d/permanent-id/view"
    assert bootstrap.uploads[0][1] == GOOGLE_BOOTSTRAP_NAME
    assert drive.metadata_replacements == [("permanent-id", GOOGLE_SNAPSHOT_NAME, JSON_MIME_TYPE, b'{"snapshot":true}')]
    assert drive.published == ["permanent-id"]


@pytest.mark.asyncio
async def test_google_snapshot_replaces_existing_json_without_changing_id() -> None:
    file = DriveFile(id="permanent-id", name=GOOGLE_SNAPSHOT_NAME, mimeType=JSON_MIME_TYPE)
    drive = FakeDrive([file])
    uploader = GoogleDriveSnapshotUploader(drive, FakeBootstrap(), "folder")  # type: ignore[arg-type]
    expected = "https://drive.google.com/file/d/permanent-id/view"

    assert await uploader.upload(b"replacement", expected_url=expected) == expected
    assert drive.replacements == [("permanent-id", b"replacement", JSON_MIME_TYPE)]


@pytest.mark.asyncio
async def test_yandex_snapshot_overwrites_fixed_path_and_returns_published_link() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/resources/upload"):
            return httpx.Response(200, json={"href": "https://upload.example/stable"})
        if request.url.host == "upload.example":
            return httpx.Response(201)
        if request.url.path.endswith("/resources/publish"):
            return httpx.Response(200)
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"public_url": "https://disk.example/stable"})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        uploader = YandexDiskSnapshotUploader("token", client)
        url = await uploader.upload(b"snapshot", expected_url="https://disk.example/stable")

    assert url == "https://disk.example/stable"
    upload_link_request = next(request for request in requests if request.url.path.endswith("/resources/upload"))
    assert upload_link_request.url.params["path"] == YANDEX_SNAPSHOT_PATH
    assert upload_link_request.url.params["overwrite"] == "true"
    content_request = next(request for request in requests if request.url.host == "upload.example")
    assert content_request.content == b"snapshot"
    assert content_request.headers["content-type"] == JSON_MIME_TYPE


@pytest.mark.asyncio
async def test_yandex_snapshot_refuses_to_overwrite_a_changed_public_link() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"public_url": "https://disk.example/replacement"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        uploader = YandexDiskSnapshotUploader("token", client)
        with pytest.raises(RuntimeError, match="public URL changed"):
            await uploader.upload(b"snapshot", expected_url="https://disk.example/stable")

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_yandex_snapshot_retries_transient_upload_failure() -> None:
    upload_attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal upload_attempts
        if request.url.path.endswith("/resources/upload"):
            return httpx.Response(200, json={"href": "https://upload.example/stable"})
        if request.url.host == "upload.example":
            upload_attempts += 1
            return httpx.Response(503 if upload_attempts == 1 else 201)
        if request.url.path.endswith("/resources/publish"):
            return httpx.Response(200)
        return httpx.Response(200, json={"public_url": "https://disk.example/stable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        uploader = YandexDiskSnapshotUploader("token", client)
        url = await uploader.upload(b"snapshot", expected_url="https://disk.example/stable")

    assert url == "https://disk.example/stable"
    assert upload_attempts == 2


@pytest.mark.asyncio
async def test_yandex_snapshot_error_does_not_expose_upload_url() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resources/upload"):
            return httpx.Response(200, json={"href": "https://upload.example/stable?secret=sensitive"})
        return httpx.Response(403, json={"description": "upload denied"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        uploader = YandexDiskSnapshotUploader("token", client)
        with pytest.raises(RuntimeError, match="content upload failed with HTTP 403: upload denied") as caught:
            await uploader.upload(b"snapshot", expected_url=None)

    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_ntfy_alarm_names_failed_snapshot_destinations() -> None:
    captured: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == NTFY_URL
        captured.append(json.loads(request.content))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        alerts = NtfySnapshotAlertPublisher("topic", client)
        await alerts.upload_failed(("google_drive", "yandex_disk"), "2026-10-13T12:00:00.000000+03:00")

    assert captured[0]["priority"] == 5
    assert "Google Drive, Yandex Disk" in captured[0]["message"]
