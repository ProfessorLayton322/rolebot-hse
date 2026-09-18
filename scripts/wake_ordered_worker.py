from __future__ import annotations

import json
import os

import boto3


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    sqs = boto3.client(
        "sqs",
        endpoint_url="https://message-queue.api.cloud.yandex.net",
        region_name="ru-central1",
        aws_access_key_id=required_environment("YMQ_ACCESS_KEY_ID"),
        aws_secret_access_key=required_environment("YMQ_SECRET_ACCESS_KEY"),
    )
    sqs.send_message(
        QueueUrl=required_environment("YMQ_KICK_URL"),
        MessageBody=json.dumps(
            {"schema_version": 1, "event_id": "deployment-recovery"},
            separators=(",", ":"),
        ),
    )


if __name__ == "__main__":
    main()
