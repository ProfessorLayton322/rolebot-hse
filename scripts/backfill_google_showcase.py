from __future__ import annotations

import argparse
import asyncio

import boto3

from larp_bot.adapters.aws import DynamoEventRepository, SqsFifoCommandPublisher
from larp_bot.application.google_showcase_backfill import enqueue_google_showcase_backfill


async def _run(events_table: str, leaders_table: str, queue_url: str) -> int:
    events = DynamoEventRepository(
        boto3.client("dynamodb"),
        events_table=events_table,
        leaders_table=leaders_table,
    )
    publisher = SqsFifoCommandPublisher(boto3.client("sqs"), queue_url)
    return await enqueue_google_showcase_backfill(events, publisher)


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue Google Drive showcase backfill for incomplete DynamoDB events")
    parser.add_argument("--events-table", required=True)
    parser.add_argument("--leaders-table", required=True)
    parser.add_argument("--queue-url", required=True)
    args = parser.parse_args()
    queued = asyncio.run(_run(args.events_table, args.leaders_table, args.queue_url))
    print(f"Queued Google Drive showcase backfill for {queued} event(s)")


if __name__ == "__main__":
    main()
