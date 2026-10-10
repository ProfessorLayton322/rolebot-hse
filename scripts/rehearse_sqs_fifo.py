from __future__ import annotations

import argparse
import json
from typing import Any
from uuid import uuid4

import boto3

from larp_bot.migration.cli import AWS_ACCOUNT_ID, AWS_PROFILE, AWS_REGION, AWS_ROLE_NAME


def receive(client: Any, queue_url: str, *, maximum: int) -> list[dict[str, Any]]:
    response = client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=maximum,
        WaitTimeSeconds=10,
        VisibilityTimeout=10,
        AttributeNames=["ApproximateReceiveCount", "MessageGroupId", "SequenceNumber"],
    )
    return response.get("Messages", [])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-url", required=True)
    args = parser.parse_args()
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != AWS_ACCOUNT_ID or f":assumed-role/{AWS_ROLE_NAME}/" not in identity.get("Arn", ""):
        raise SystemExit("refusing rehearsal outside the fixed AWS account and bootstrap role")
    sqs = session.client("sqs")
    rehearsal = uuid4().hex
    retry_group = f"rehearsal-retry-{rehearsal}"
    retry_body = json.dumps({"rehearsal": rehearsal, "sequence": 0}, separators=(",", ":"))
    sqs.send_message(
        QueueUrl=args.queue_url,
        MessageBody=retry_body,
        MessageGroupId=retry_group,
        MessageDeduplicationId=f"{rehearsal}-retry",
    )
    first = receive(sqs, args.queue_url, maximum=1)
    if len(first) != 1 or first[0]["Body"] != retry_body:
        raise SystemExit("FIFO retry rehearsal did not receive its first message")
    sqs.change_message_visibility(QueueUrl=args.queue_url, ReceiptHandle=first[0]["ReceiptHandle"], VisibilityTimeout=0)
    retried = receive(sqs, args.queue_url, maximum=1)
    if len(retried) != 1 or retried[0]["Body"] != retry_body:
        raise SystemExit("FIFO retry rehearsal did not redeliver the failed message")
    if int(retried[0]["Attributes"]["ApproximateReceiveCount"]) < 2:
        raise SystemExit("FIFO retry rehearsal did not increment the receive count")
    sqs.delete_message(QueueUrl=args.queue_url, ReceiptHandle=retried[0]["ReceiptHandle"])

    order_group = f"rehearsal-order-{rehearsal}"
    expected = [json.dumps({"rehearsal": rehearsal, "sequence": value}, separators=(",", ":")) for value in range(1, 4)]
    for index, body in enumerate(expected, start=1):
        sqs.send_message(
            QueueUrl=args.queue_url,
            MessageBody=body,
            MessageGroupId=order_group,
            MessageDeduplicationId=f"{rehearsal}-order-{index}",
        )
    received: list[dict[str, Any]] = []
    while len(received) < len(expected):
        received.extend(receive(sqs, args.queue_url, maximum=10))
    if [message["Body"] for message in received] != expected:
        raise SystemExit("FIFO rehearsal messages were not received in send order")
    for message in received:
        sqs.delete_message(QueueUrl=args.queue_url, ReceiptHandle=message["ReceiptHandle"])
    print("SQS FIFO ordering and retry rehearsal passed; all synthetic messages were removed")


if __name__ == "__main__":
    main()
