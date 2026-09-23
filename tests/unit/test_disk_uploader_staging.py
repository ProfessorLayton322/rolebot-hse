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
            return httpx.Response(200)
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
    assert payload["files"][1]["overwrite"] is True
    assert payload["files"][1]["publish"] is False


@pytest.mark.asyncio
async def test_python_does_not_fall_back_to_yandex_disk_when_submission_fails() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(200 if request.method == "PUT" else 503)

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

    assert hosts == ["storage.example", "functions.example"]


async def _record_pending(sequence: list[str]) -> None:
    sequence.append("pending")
