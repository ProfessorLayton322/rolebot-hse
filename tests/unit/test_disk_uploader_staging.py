from __future__ import annotations

import json

import httpx
import pytest

from larp_bot.adapters.yandex_disk.repository import PPTX_MIME_TYPE, XLSX_MIME_TYPE, DiskUpload
from larp_bot.adapters.yandex_disk.uploader import StagedYandexDiskUploader


@pytest.mark.asyncio
async def test_python_stages_files_and_only_submits_an_async_go_invocation() -> None:
    requests: list[httpx.Request] = []
    sequence: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        sequence.append(request.url.host or "")
        if request.url.host == "storage.example":
            return httpx.Response(200, headers={"X-Amz-Version-Id": f"version-{len(requests)}"})
        if request.url.host == "functions.example":
            return httpx.Response(202)
        raise AssertionError(f"unexpected host: {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = StagedYandexDiskUploader(
            bucket="private-bucket",
            function_url="https://functions.example/uploader",
            iam_token="iam-token",
            client=client,
            storage_url="https://storage.example",
        )
        result = await uploader.submit(
            [
                DiskUpload(
                    role="achievement",
                    disk_path="disk:/larp-bot/achievements/event.pptx",
                    content=b"presentation",
                    content_type=PPTX_MIME_TYPE,
                    overwrite=False,
                    publish=True,
                ),
                DiskUpload(
                    role="pass",
                    disk_path="disk:/larp-bot/passes/event.xlsx",
                    content=b"workbook",
                    content_type=XLSX_MIME_TYPE,
                    overwrite=True,
                    publish=False,
                ),
            ],
            callback={"kind": "achievement"},
            before_invoke=lambda: _record_pending(sequence),
        )

    assert result is None
    assert [request.method for request in requests] == ["PUT", "PUT", "POST"]
    assert sequence == ["storage.example", "storage.example", "pending", "functions.example"]
    assert all(request.headers["Authorization"] == "Bearer iam-token" for request in requests)
    assert requests[0].url.path.startswith("/private-bucket/larp-bot/uploads/")
    assert requests[0].url.path.endswith("/achievement.pptx")
    assert requests[0].headers["Content-Type"] == PPTX_MIME_TYPE
    assert requests[0].content == b"presentation"
    invocation = requests[-1]
    assert invocation.url == httpx.URL("https://functions.example/uploader?integration=async")
    payload = json.loads(invocation.content)
    assert payload["callback"] == {"kind": "achievement"}
    assert [file["role"] for file in payload["files"]] == ["achievement", "pass"]
    assert [file["object_version"] for file in payload["files"]] == ["version-1", "version-2"]
    assert payload["files"][1]["overwrite"] is True
    assert payload["files"][1]["publish"] is False


@pytest.mark.asyncio
async def test_python_does_not_fall_back_to_yandex_disk_when_submission_fails() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            return httpx.Response(200, headers={"X-Amz-Version-Id": "staged-version"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = StagedYandexDiskUploader(
            bucket="private-bucket",
            function_url="https://functions.example/uploader",
            iam_token="iam-token",
            client=client,
            storage_url="https://storage.example",
        )
        with pytest.raises(httpx.HTTPStatusError):
            await uploader.submit(
                [
                    DiskUpload(
                        role="file",
                        disk_path="disk:/larp-bot/events/event.xlsx",
                        content=b"workbook",
                        content_type=XLSX_MIME_TYPE,
                        overwrite=False,
                        publish=True,
                    )
                ]
            )

    assert [(request.method, request.url.host) for request in requests] == [
        ("PUT", "storage.example"),
        ("POST", "functions.example"),
        ("DELETE", "storage.example"),
    ]
    assert requests[-1].url.params["versionId"] == "staged-version"


@pytest.mark.asyncio
async def test_python_removes_successfully_staged_versions_when_another_stage_fails() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/master.xlsx"):
            return httpx.Response(200, headers={"X-Amz-Version-Id": "master-version"})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = StagedYandexDiskUploader(
            bucket="private-bucket",
            function_url="https://functions.example/uploader",
            iam_token="iam-token",
            client=client,
            storage_url="https://storage.example",
        )
        with pytest.raises(httpx.HTTPStatusError):
            await uploader.submit(
                [
                    DiskUpload("master", "disk:/larp-bot/events/master.xlsx", b"master", XLSX_MIME_TYPE, False, True),
                    DiskUpload("public", "disk:/larp-bot/events/public.xlsx", b"public", XLSX_MIME_TYPE, False, True),
                ]
            )

    assert [request.method for request in requests].count("PUT") == 2
    deletes = [request for request in requests if request.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.path.endswith("/master.xlsx")
    assert deletes[0].url.params["versionId"] == "master-version"


@pytest.mark.asyncio
async def test_python_removes_staged_version_when_pending_notification_fails() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            return httpx.Response(200, headers={"X-Amz-Version-Id": "staged-version"})
        return httpx.Response(204)

    async def fail_pending() -> None:
        raise RuntimeError("notification failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = StagedYandexDiskUploader(
            bucket="private-bucket",
            function_url="https://functions.example/uploader",
            iam_token="iam-token",
            client=client,
            storage_url="https://storage.example",
        )
        with pytest.raises(RuntimeError, match="notification failed"):
            await uploader.submit(
                [DiskUpload("file", "disk:/larp-bot/events/event.xlsx", b"file", XLSX_MIME_TYPE, False, True)],
                before_invoke=fail_pending,
            )

    assert [request.method for request in requests] == ["PUT", "DELETE"]
    assert requests[-1].url.params["versionId"] == "staged-version"


async def _record_pending(sequence: list[str]) -> None:
    sequence.append("pending")
