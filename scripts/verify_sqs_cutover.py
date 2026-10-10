from __future__ import annotations

import argparse
import time
from typing import Any, cast
from uuid import UUID

import boto3

from larp_bot.domain.models import EmptyPayload, Operation, OrderedRegistrationCommand, Platform
from larp_bot.migration.cli import AWS_ACCOUNT_ID, AWS_PROFILE, AWS_REGION, AWS_ROLE_NAME

QUEUE_ATTRIBUTES = ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible")
WORKER_FUNCTION = "larp-bot-prod-ordered-worker"


def session() -> Any:
    value = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    identity = value.client("sts").get_caller_identity()
    if identity.get("Account") != AWS_ACCOUNT_ID or f":assumed-role/{AWS_ROLE_NAME}/" not in identity.get("Arn", ""):
        raise RuntimeError("refusing cutover probe outside the fixed AWS account and bootstrap role")
    return value


def queue_depth(client: Any, queue_url: str) -> int:
    response = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=list(QUEUE_ATTRIBUTES))
    return sum(int(response.get("Attributes", {}).get(name, "0")) for name in QUEUE_ATTRIBUTES)


def event_mapping(lambda_client: Any) -> dict[str, Any]:
    mappings = lambda_client.list_event_source_mappings(FunctionName=WORKER_FUNCTION).get("EventSourceMappings", [])
    if len(mappings) != 1:
        raise RuntimeError(f"expected one ordered-worker event source mapping, found {len(mappings)}")
    return cast(dict[str, Any], mappings[0])


def prepare(sqs: Any, lambda_client: Any, queue_url: str, dlq_url: str, probe_id: str) -> None:
    mapping = event_mapping(lambda_client)
    if mapping.get("State") != "Disabled":
        raise RuntimeError("cutover probe must be prepared while the SQS event source mapping is disabled")
    if queue_depth(sqs, queue_url) or queue_depth(sqs, dlq_url):
        raise RuntimeError("cutover probe requires empty primary and dead-letter queues")
    event_id = f"phase6-probe-{probe_id}"
    command = OrderedRegistrationCommand(
        operation_id=probe_id,
        event_id=event_id,
        operation=Operation.OPEN_REGISTRATION,
        platform=Platform.SYSTEM,
        platform_user_id=0,
        payload=EmptyPayload(),
    )
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=command.model_dump_json(),
        MessageGroupId=event_id,
        MessageDeduplicationId=probe_id,
    )
    for _ in range(15):
        if queue_depth(sqs, queue_url) == 1:
            print(f"Prepared one non-mutating cutover FIFO probe: {probe_id}")
            return
        time.sleep(1)
    raise RuntimeError("the prepared cutover probe did not become visible in SQS")


def verify(sqs: Any, lambda_client: Any, queue_url: str, dlq_url: str, probe_id: str) -> None:
    del probe_id  # The queue is isolated and was required to be empty before this one probe was added.
    consecutive_empty = 0
    for _ in range(60):
        mapping = event_mapping(lambda_client)
        primary_depth = queue_depth(sqs, queue_url)
        dlq_depth = queue_depth(sqs, dlq_url)
        if mapping.get("State") == "Enabled" and mapping.get("LastProcessingResult") == "OK" and primary_depth == 0:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                if dlq_depth:
                    raise RuntimeError("the cutover FIFO probe reached the dead-letter queue")
                print("Enabled Lambda event source consumed the non-mutating FIFO cutover probe")
                return
        else:
            consecutive_empty = 0
        time.sleep(2)
    raise RuntimeError("the enabled Lambda event source did not consume the cutover FIFO probe")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "verify"))
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--dlq-url", required=True)
    parser.add_argument("--probe-id", required=True)
    args = parser.parse_args()
    try:
        probe_id = str(UUID(args.probe_id))
    except ValueError as exc:
        raise SystemExit("--probe-id must be a UUID") from exc
    aws = session()
    sqs = aws.client("sqs")
    lambda_client = aws.client("lambda")
    if args.action == "prepare":
        prepare(sqs, lambda_client, args.queue_url, args.dlq_url, probe_id)
    else:
        verify(sqs, lambda_client, args.queue_url, args.dlq_url, probe_id)


if __name__ == "__main__":
    main()
