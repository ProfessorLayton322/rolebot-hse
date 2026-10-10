from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus

import boto3

from larp_bot.functions.aws_bootstrap import build_aws_container
from larp_bot.functions.container import AppContainer
from larp_bot.functions.runtime_mode import RuntimeGate

_container: AppContainer | None = None
_runner = asyncio.Runner()


async def _completion_payloads(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records = event.get("Records")
    if records is None:
        return [event]
    if not isinstance(records, list) or not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("upload completion must contain S3 records")
    client = boto3.client("s3")
    payloads: list[Mapping[str, Any]] = []
    for record in records:
        s3 = record.get("s3")
        if not isinstance(s3, dict):
            raise ValueError("upload completion record has no S3 payload")
        bucket = s3.get("bucket")
        object_info = s3.get("object")
        if not isinstance(bucket, dict) or not isinstance(object_info, dict):
            raise ValueError("upload completion record has invalid S3 metadata")
        bucket_name = bucket.get("name")
        object_key = object_info.get("key")
        if not isinstance(bucket_name, str) or not isinstance(object_key, str):
            raise ValueError("upload completion record is missing its bucket or key")
        response = await asyncio.to_thread(
            client.get_object,
            Bucket=bucket_name,
            Key=unquote_plus(object_key),
        )
        body = await asyncio.to_thread(response["Body"].read)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("upload completion object must contain a JSON object")
        payloads.append(payload)
    return payloads


async def _run(event: Mapping[str, Any]) -> dict[str, bool]:
    global _container
    if not RuntimeGate.from_environment().accepts_application_work:
        return {"processed": False}
    if _container is None:
        _container = await build_aws_container()
    for payload in await _completion_payloads(event):
        await _container.upload_completion.handle(payload)
    return {"processed": True}


def handler(event: Mapping[str, Any], context: Any) -> dict[str, bool]:
    del context
    return _runner.run(_run(event))
