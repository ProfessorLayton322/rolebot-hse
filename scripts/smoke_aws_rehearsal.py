from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from typing import Any
from uuid import uuid4

import boto3


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def signed_request(url: str, path: str, body: bytes, secret: str, request_id: str) -> tuple[int, bytes]:
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise RuntimeError("AWS gateway URL must be HTTPS")
    timestamp = str(int(time.time()))
    canonical = "\n".join((timestamp, request_id, "POST", path, hashlib.sha256(body).hexdigest()))
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(  # noqa: S310
        urllib.parse.urljoin(url.rstrip("/") + "/", path.lstrip("/")),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gateway-Request-Id": request_id,
            "X-Gateway-Timestamp": timestamp,
            "X-Gateway-Signature": signature,
            "X-Telegram-Inline-Deadline-Ms": str(int(time.time() * 1000) + 60_000),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def invoke(lambda_client: Any, function_name: str, payload: object) -> dict[str, Any]:
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, separators=(",", ":")).encode(),
    )
    body = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise RuntimeError(f"{function_name} failed: {body}")
    if not isinstance(body, dict):
        raise RuntimeError(f"{function_name} returned a non-object response")
    return body


def delete_all_versions(s3: Any, bucket: str, key: str) -> None:
    response = s3.list_object_versions(Bucket=bucket, Prefix=key)
    objects = [
        {"Key": item["Key"], "VersionId": item["VersionId"]}
        for item in [*response.get("Versions", []), *response.get("DeleteMarkers", [])]
        if item["Key"] == key
    ]
    if objects:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})


def delete_disk_file(path: str, token: str) -> None:
    disk_url = "https://cloud-api.yandex.net/v1/disk/resources?" + urllib.parse.urlencode({"path": path})
    request = urllib.request.Request(disk_url, method="DELETE", headers={"Authorization": f"OAuth {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            if response.status not in {202, 204}:
                raise RuntimeError("could not remove the rehearsal Yandex Disk file")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def main() -> None:
    gateway_url = required("AWS_GATEWAY_URL")
    ingress_secret = required("CF_TO_AWS_HMAC_SECRET")
    egress_secret = required("AWS_TO_CF_EGRESS_HMAC_SECRET")
    bucket = required("AWS_STORAGE_BUCKET")
    suffix = uuid4().hex
    synthetic_id = 8_000_000_000_000 + int(suffix[:8], 16)
    job_id = str(uuid4())
    object_key = f"uploads/{job_id}/rehearsal.txt"
    disk_path = f"disk:/larp-bot/migration-rehearsal/{job_id}.txt"
    state_key = f"larp-bot/upload-state/{hashlib.sha256(disk_path.encode()).hexdigest()}.txt"
    dynamodb = boto3.client("dynamodb")
    lambda_client = boto3.client("lambda")
    s3 = boto3.client("s3")

    def cleanup() -> None:
        failures: list[Exception] = []
        actions = (
            lambda: delete_disk_file(disk_path, required("YANDEX_DISK_TOKEN")),
            lambda: delete_all_versions(s3, bucket, object_key),
            lambda: delete_all_versions(s3, bucket, state_key),
            lambda: dynamodb.delete_item(
                TableName="larp-bot-rehearsal-tg-users", Key={"tg_id": {"S": str(synthetic_id)}}
            ),
        )
        for action in actions:
            try:
                action()
            except Exception as exc:  # Preserve best-effort cleanup of the remaining resources.
                failures.append(exc)
        if failures:
            raise RuntimeError(f"AWS rehearsal cleanup failed in {len(failures)} action(s)") from failures[0]

    atexit.register(cleanup)

    telegram_body = json.dumps(
        {
            "update_id": synthetic_id,
            "message": {
                "message_id": synthetic_id,
                "from": {"id": synthetic_id},
                "chat": {"id": synthetic_id, "type": "private"},
                "text": "/start",
            },
        },
        separators=(",", ":"),
    ).encode()
    status, response_body = signed_request(
        gateway_url,
        "/webhooks/telegram",
        telegram_body,
        ingress_secret,
        f"phase5-telegram-{suffix}",
    )
    if status != 200 or json.loads(response_body).get("delivery") not in {"inline", "deferred"}:
        raise RuntimeError(f"AWS Telegram rehearsal returned HTTP {status}")

    vk_body = json.dumps(
        {
            "type": "confirmation",
            "group_id": int(required("VK_GROUP_ID")),
            "secret": required("VK_CALLBACK_SECRET"),
        },
        separators=(",", ":"),
    ).encode()
    status, response_body = signed_request(
        gateway_url,
        "/webhooks/vk-cloudflare",
        vk_body,
        ingress_secret,
        f"phase5-vk-{suffix}",
    )
    if status != 200 or response_body.decode() != required("VK_CONFIRMATION_STRING"):
        raise RuntimeError(f"AWS VK rehearsal returned HTTP {status}")

    egress_url = required("TELEGRAM_EGRESS_URL").rstrip("/") + "/telegram/send"
    parsed_egress_url = urllib.parse.urlparse(egress_url)
    if parsed_egress_url.scheme != "https" or not parsed_egress_url.netloc:
        raise RuntimeError("Telegram egress URL must be HTTPS")
    egress_request_id = f"phase5-egress-{suffix}"
    egress_body = json.dumps(
        {"request_id": egress_request_id, "method": "getMe", "payload": {}}, separators=(",", ":")
    ).encode()
    timestamp = str(int(time.time()))
    canonical = "\n".join(
        (timestamp, egress_request_id, "POST", "/telegram/send", hashlib.sha256(egress_body).hexdigest())
    )
    signature = hmac.new(egress_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(  # noqa: S310
        egress_url,
        data=egress_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Request-Id": egress_request_id,
            "X-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            response_body = response.read()
            if response.status != 200 or json.loads(response_body).get("ok") is not True:
                raise RuntimeError("AWS-signed Cloudflare egress rehearsal failed")
    except urllib.error.HTTPError as exc:
        response_body = exc.read(512).decode(errors="replace")
        raise RuntimeError(
            f"AWS-signed Cloudflare egress rehearsal returned HTTP {exc.code}: {response_body!r}"
        ) from exc

    notifier_result = invoke(lambda_client, "larp-bot-prod-registration-notifier", {"source": "phase5-rehearsal"})
    if notifier_result.get("processed") is not False:
        raise RuntimeError("disabled scheduler target did not no-op during rehearsal")

    content = b"rolebot AWS upload rehearsal\n"
    put = s3.put_object(Bucket=bucket, Key=object_key, Body=BytesIO(content), ContentType="text/plain")
    version_id = put.get("VersionId")
    if not version_id:
        raise RuntimeError("versioned rehearsal upload returned no S3 version ID")
    generation = f"{time.time_ns():020d}-{job_id}"
    upload_result = invoke(
        lambda_client,
        "larp-bot-prod-disk-uploader",
        {
            "schema_version": 1,
            "job_id": job_id,
            "bucket": bucket,
            "files": [
                {
                    "role": "rehearsal",
                    "object_key": object_key,
                    "object_version": version_id,
                    "disk_path": disk_path,
                    "content_type": "text/plain",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "generation": generation,
                    "overwrite": True,
                    "publish": False,
                }
            ],
            "callback": None,
        },
    )
    if upload_result.get("processed") is not True:
        raise RuntimeError("AWS disk uploader did not process the rehearsal file")

    cleanup()
    atexit.unregister(cleanup)
    print("AWS rehearsal HTTP, VK, egress, scheduler target, and upload checks passed")


if __name__ == "__main__":
    main()
