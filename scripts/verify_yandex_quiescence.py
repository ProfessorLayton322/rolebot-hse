from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import boto3

SQS_ENDPOINT = "https://message-queue.api.cloud.yandex.net"
STORAGE_ENDPOINT = "https://storage.yandexcloud.net"
QUEUE_ATTRIBUTES = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
PENDING_PREFIXES = ("larp-bot/uploads/", "larp-bot/upload-locks/")


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def queue_depth(client: Any, queue_url: str) -> int:
    response = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=list(QUEUE_ATTRIBUTES))
    return sum(int(response.get("Attributes", {}).get(name, "0")) for name in QUEUE_ATTRIBUTES)


def visible_objects(client: Any, bucket: str, prefix: str) -> int:
    paginator = client.get_paginator("list_objects_v2")
    return sum(page.get("KeyCount", 0) for page in paginator.paginate(Bucket=bucket, Prefix=prefix))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.timeout_seconds < 30 or args.timeout_seconds > 1800:
        raise SystemExit("timeout must be between 30 and 1800 seconds")

    access_key = required_environment("YMQ_ACCESS_KEY_ID")
    secret_key = required_environment("YMQ_SECRET_ACCESS_KEY")
    fifo_url = required_environment("YMQ_FIFO_URL")
    kick_url = required_environment("YMQ_KICK_URL")
    bucket = required_environment("YANDEX_STORAGE_BUCKET")
    sqs = boto3.client(
        "sqs",
        endpoint_url=SQS_ENDPOINT,
        region_name="ru-central1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    storage = boto3.client("s3", endpoint_url=STORAGE_ENDPOINT)
    deadline = time.monotonic() + args.timeout_seconds
    consecutive_empty = 0
    last_wake = 0.0

    while time.monotonic() < deadline:
        fifo_depth = queue_depth(sqs, fifo_url)
        kick_depth = queue_depth(sqs, kick_url)
        pending_uploads = sum(visible_objects(storage, bucket, prefix) for prefix in PENDING_PREFIXES)
        print(
            f"Yandex quiescence: fifo={fifo_depth}, kick={kick_depth}, pending_uploads={pending_uploads}",
            flush=True,
        )
        if fifo_depth == 0 and kick_depth == 0 and pending_uploads == 0:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print("Yandex queues and upload staging are quiescent")
                return
        else:
            consecutive_empty = 0
        if fifo_depth > 0 and time.monotonic() - last_wake >= 20:
            sqs.send_message(
                QueueUrl=kick_url,
                MessageBody=json.dumps(
                    {"schema_version": 1, "event_id": "aws-cutover-drain"},
                    separators=(",", ":"),
                ),
            )
            last_wake = time.monotonic()
        time.sleep(5)

    raise SystemExit("Yandex did not become quiescent before the timeout")


if __name__ == "__main__":
    main()
