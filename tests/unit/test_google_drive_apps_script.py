from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from uuid import UUID

import pytest

from larp_bot.adapters.google_drive.apps_script import GoogleDriveAppsScriptClient
from larp_bot.adapters.google_drive.statistics import XLSX_MIME_TYPE

ENDPOINT = "https://script.google.com/macros/s/deployment-id/exec"
SECRET = "a" * 64
FOLDER_ID = "roleclub-folder"
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000001")


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class RecordingPost:
    def __init__(self, response_fields: dict[str, object]) -> None:
        self.response_fields = response_fields
        self.url = ""
        self.kwargs: dict[str, Any] = {}

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.url = url
        self.kwargs = kwargs
        request = kwargs["json"]
        return FakeResponse(
            {
                "ok": True,
                "operation": request["operation"],
                "request_id": request["request_id"],
            }
            | self.response_fields
        )


def client(post: RecordingPost) -> GoogleDriveAppsScriptClient:
    return GoogleDriveAppsScriptClient(
        ENDPOINT,
        SECRET,
        FOLDER_ID,
        post=post,
        clock=lambda: 1_700_000_000.9,
        request_id_factory=lambda: REQUEST_ID,
    )


@pytest.mark.asyncio
async def test_signed_apps_script_upload_returns_drive_file() -> None:
    post = RecordingPost(
        {
            "exists": True,
            "created": True,
            "name": "events — game — master.xlsx",
            "file_id": "drive-file-id",
            "url": "https://drive.google.com/file/d/drive-file-id/view",
        }
    )

    file = await client(post).upsert_xlsx(FOLDER_ID, "events — game — master.xlsx", b"xlsx-content")

    assert file.file_id == "drive-file-id"
    assert file.mime_type == XLSX_MIME_TYPE
    assert post.url == ENDPOINT
    assert post.kwargs["timeout"] == 30
    assert post.kwargs["allow_redirects"] is True
    request = post.kwargs["json"]
    assert request["timestamp"] == 1_700_000_000
    assert request["request_id"] == str(REQUEST_ID)
    assert request["content_base64"] == base64.b64encode(b"xlsx-content").decode("ascii")
    content_hash = hashlib.sha256(b"xlsx-content").hexdigest()
    canonical = "\n".join(
        (
            "1",
            "1700000000",
            str(REQUEST_ID),
            "upsert_xlsx",
            "events — game — master.xlsx",
            content_hash,
        )
    )
    expected_signature = base64.urlsafe_b64encode(hmac.digest(SECRET.encode(), canonical.encode(), "sha256")).rstrip(
        b"="
    )
    assert request["signature"] == expected_signature.decode("ascii")


@pytest.mark.asyncio
async def test_signed_apps_script_health_checks_fixed_folder() -> None:
    post = RecordingPost({"folder_id": FOLDER_ID, "folder_name": "roleclub"})

    assert await client(post).validate_target() == "roleclub"
    request = post.kwargs["json"]
    assert request["operation"] == "health"
    assert request["name"] == ""
    assert request["content_sha256"] == hashlib.sha256(b"").hexdigest()
    assert request["content_base64"] == ""


@pytest.mark.asyncio
async def test_signed_apps_script_exists_and_trash_are_mapped() -> None:
    exists_post = RecordingPost({"exists": False, "name": "passes — game.xlsx"})
    assert not await client(exists_post).named_xlsx_exists(FOLDER_ID, "passes — game.xlsx")

    trash_post = RecordingPost({"existed": True, "name": "passes — game.xlsx", "file_id": "file-id"})
    await client(trash_post).delete_named_xlsx(FOLDER_ID, "passes — game.xlsx")
    assert trash_post.kwargs["json"]["operation"] == "trash_xlsx"


@pytest.mark.asyncio
async def test_signed_apps_script_rejection_fails_closed() -> None:
    class RejectingPost:
        def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
            del url
            request = kwargs["json"]
            return FakeResponse(
                {
                    "ok": False,
                    "request_id": request["request_id"],
                    "error": "authentication_failed",
                    "message": "The request signature is invalid.",
                }
            )

    gateway = GoogleDriveAppsScriptClient(
        ENDPOINT,
        SECRET,
        FOLDER_ID,
        post=RejectingPost(),
        request_id_factory=lambda: REQUEST_ID,
    )

    with pytest.raises(RuntimeError, match="authentication_failed"):
        await gateway.named_xlsx_exists(FOLDER_ID, "events — game.xlsx")


@pytest.mark.parametrize(
    "url",
    [
        "http://script.google.com/macros/s/id/exec",
        "https://example.com/macros/s/id/exec",
        "https://script.google.com/macros/s/id/dev",
        "https://script.google.com/macros/s/id/extra/exec",
        "https://user@script.google.com/macros/s/id/exec",
        "https://script.google.com/macros/s/id/exec?secret=leak",
    ],
)
def test_signed_apps_script_rejects_untrusted_endpoint_urls(url: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        GoogleDriveAppsScriptClient(url, SECRET, FOLDER_ID)


@pytest.mark.asyncio
async def test_signed_apps_script_rejects_wrong_runtime_folder() -> None:
    with pytest.raises(RuntimeError, match="unexpected folder ID"):
        await client(RecordingPost({})).named_xlsx_exists("other-folder", "events — game.xlsx")
