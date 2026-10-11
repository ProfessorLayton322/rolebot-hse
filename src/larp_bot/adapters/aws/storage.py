from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from larp_bot.adapters.yandex_disk.repository import DiskUpload, UploadReadyCallback


class AwsStagedYandexDiskUploader:
    """Stage files in private S3 and asynchronously invoke the AWS Go uploader."""

    def __init__(
        self,
        *,
        s3_client: Any,
        lambda_client: Any,
        bucket: str,
        function_name: str,
        prefix: str = "uploads/",
    ) -> None:
        self.s3 = s3_client
        self.lambda_client = lambda_client
        self.bucket = bucket
        self.function_name = function_name
        self.prefix = prefix

    def set_iam_token(self, token: str) -> None:
        del token

    async def _stage(self, upload: DiskUpload, *, job_id: str, generation: str) -> dict[str, object]:
        extension = upload.disk_path.rsplit(".", 1)[-1].lower()
        object_key = f"{self.prefix}{job_id}/{upload.role}.{extension}"
        digest = hashlib.sha256(upload.content).hexdigest()
        response = await asyncio.to_thread(
            self.s3.put_object,
            Bucket=self.bucket,
            Key=object_key,
            Body=upload.content,
            ContentType=upload.content_type,
            Metadata={"sha256": digest, "generation": generation},
        )
        version = response.get("VersionId")
        if not isinstance(version, str) or not version:
            raise RuntimeError("S3 did not return the staged object version")
        return {
            "role": upload.role,
            "object_key": object_key,
            "object_version": version,
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
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.s3.delete_object,
                    Bucket=self.bucket,
                    Key=str(file["object_key"]),
                    VersionId=str(file["object_version"]),
                )
                for file in files
            ),
            return_exceptions=True,
        )

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
        results = await asyncio.gather(
            *(self._stage(upload, job_id=job_id, generation=generation) for upload in files),
            return_exceptions=True,
        )
        staged = [result for result in results if isinstance(result, dict)]
        error = next((result for result in results if isinstance(result, BaseException)), None)
        if error is not None:
            await self._delete_staged(staged)
            raise error
        payload = json.dumps(
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
        response = await asyncio.to_thread(
            self.lambda_client.invoke,
            FunctionName=self.function_name,
            InvocationType="Event",
            Payload=payload,
        )
        if response.get("StatusCode") != 202:
            await self._delete_staged(staged)
            raise RuntimeError("AWS disk uploader did not accept the asynchronous invocation")
