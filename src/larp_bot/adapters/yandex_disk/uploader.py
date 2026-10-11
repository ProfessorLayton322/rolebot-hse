from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from urllib.parse import quote
from uuid import uuid4

import httpx

from .repository import DiskUpload, UploadReadyCallback


class StagedYandexDiskUploader:
    """Stage generated files in Object Storage and enqueue the Go uploader.

    This adapter deliberately has no Yandex Disk API code. The asynchronous
    function invocation returns as soon as Yandex Cloud accepts the job; the
    Python caller never waits for a Disk transfer or publication request.
    """

    def __init__(
        self,
        *,
        bucket: str,
        function_url: str,
        iam_token: str | None,
        client: httpx.AsyncClient | None = None,
        storage_url: str = "https://storage.yandexcloud.net",
        prefix: str = "larp-bot/uploads/",
    ) -> None:
        self.bucket = bucket
        self.function_url = function_url.rstrip("/") + "?integration=async"
        self.storage_url = storage_url.rstrip("/")
        self.prefix = prefix
        self._iam_token = iam_token
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def set_iam_token(self, token: str) -> None:
        self._iam_token = token

    def _authorization(self) -> dict[str, str]:
        if not self._iam_token:
            raise RuntimeError("an IAM token is required to stage a Yandex Disk upload")
        return {"Authorization": f"Bearer {self._iam_token}"}

    def _object_url(self, key: str) -> str:
        return f"{self.storage_url}/{quote(self.bucket, safe='')}/{quote(key, safe='/')}"

    async def _stage(self, upload: DiskUpload, *, job_id: str, generation: str) -> dict[str, object]:
        extension = upload.disk_path.rsplit(".", 1)[-1].lower()
        object_key = f"{self.prefix}{job_id}/{upload.role}.{extension}"
        digest = hashlib.sha256(upload.content).hexdigest()
        response = await self._client.put(
            self._object_url(object_key),
            content=upload.content,
            headers=self._authorization()
            | {
                "Content-Type": upload.content_type,
                "X-Amz-Meta-Sha256": digest,
                "X-Amz-Meta-Generation": generation,
            },
        )
        response.raise_for_status()
        object_version = response.headers.get("X-Amz-Version-Id")
        if not object_version:
            raise RuntimeError("Object Storage did not return the staged object version")
        return {
            "role": upload.role,
            "object_key": object_key,
            "object_version": object_version,
            "disk_path": upload.disk_path,
            "content_type": upload.content_type,
            "sha256": digest,
            "generation": generation,
            "overwrite": upload.overwrite,
            "publish": upload.publish,
            "google_mirrored": upload.google_mirrored,
            "google_url": upload.google_url,
        }

    async def _delete_staged(self, files: Sequence[dict[str, object]]) -> None:
        async def delete(file: dict[str, object]) -> None:
            response = await self._client.delete(
                self._object_url(str(file["object_key"])),
                params={"versionId": str(file["object_version"])},
                headers=self._authorization(),
            )
            if response.status_code != 404:
                response.raise_for_status()

        await asyncio.gather(*(delete(file) for file in files), return_exceptions=True)

    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> None:
        if not files:
            raise ValueError("an upload job must contain at least one file")
        roles = [upload.role for upload in files]
        if len(roles) != len(set(roles)):
            raise ValueError("upload roles must be unique within a job")
        job_id = str(uuid4())
        generation = f"{time.time_ns():020d}-{job_id}"
        stage_results = await asyncio.gather(
            *(self._stage(upload, job_id=job_id, generation=generation) for upload in files),
            return_exceptions=True,
        )
        staged = [result for result in stage_results if isinstance(result, dict)]
        stage_error = next((result for result in stage_results if isinstance(result, BaseException)), None)
        if stage_error is not None:
            await self._delete_staged(staged)
            raise stage_error
        body = json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "bucket": self.bucket,
                "files": staged,
                "callback": callback,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if before_invoke is not None:
            try:
                await before_invoke()
            except BaseException:
                await self._delete_staged(staged)
                raise
        try:
            response = await self._client.post(
                self.function_url,
                content=body,
                headers=self._authorization() | {"Content-Type": "application/json"},
            )
        except BaseException:
            # The request may have reached the asynchronous invocation service.
            # Keep the staged versions for the uploader or the lifecycle backstop.
            raise
        if response.status_code != 202:
            await self._delete_staged(staged)
            response.raise_for_status()
            raise RuntimeError(f"asynchronous uploader returned HTTP {response.status_code}")
