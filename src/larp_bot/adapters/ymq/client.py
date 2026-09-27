from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from larp_bot.domain.models import OrderedRegistrationCommand


@dataclass(frozen=True)
class QueueEnvelope:
    command: OrderedRegistrationCommand
    receipt_handle: str


class YmqCommandPublisher:
    """Writes the authoritative FIFO command before emitting a standard wake-up."""

    def __init__(self, sqs_client: Any, fifo_url: str, kick_url: str) -> None:
        self.sqs = sqs_client
        self.fifo_url = fifo_url
        self.kick_url = kick_url

    async def publish(self, command: OrderedRegistrationCommand) -> None:
        body = command.model_dump_json()
        await asyncio.to_thread(
            self.sqs.send_message,
            QueueUrl=self.fifo_url,
            MessageBody=body,
            MessageGroupId=command.queue_group_id,
            MessageDeduplicationId=command.operation_id,
        )
        # A failed kick makes the HTTP attempt fail; the platform retry re-emits a kick while
        # FIFO deduplication prevents the already-durable command from being duplicated.
        await self.wake(command.event_id)

    async def wake(self, event_id: str = "continuation", *, delay_seconds: int = 0) -> None:
        await asyncio.to_thread(
            self.sqs.send_message,
            QueueUrl=self.kick_url,
            MessageBody=json.dumps({"schema_version": 1, "event_id": event_id}, separators=(",", ":")),
            DelaySeconds=delay_seconds,
        )

    async def continue_if_pending(self, *, processed: int) -> None:
        if processed:
            # Queue counters are approximate. After making progress always
            # arrange one more receive instead of trusting a stale zero.
            await self.wake(delay_seconds=10)
            return
        result = await asyncio.to_thread(
            self.sqs.get_queue_attributes,
            QueueUrl=self.fifo_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )
        attributes = result["Attributes"]
        if any(
            int(attributes[name]) > 0
            for name in ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible")
        ):
            # An empty receive can mean every FIFO group is leased by another
            # invocation (including one killed by the platform). Preserve a
            # wake-up until those leases expire; avoid a tight invocation loop.
            await self.wake(delay_seconds=10)


class YmqFifoConsumer:
    def __init__(self, sqs_client: Any, fifo_url: str, visibility_timeout: int = 180) -> None:
        self.sqs = sqs_client
        self.fifo_url = fifo_url
        self.visibility_timeout = visibility_timeout

    async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueEnvelope]:
        result = await asyncio.to_thread(
            self.sqs.receive_message,
            QueueUrl=self.fifo_url,
            MaxNumberOfMessages=min(max_messages, 10),
            WaitTimeSeconds=min(wait_seconds, 20),
            VisibilityTimeout=self.visibility_timeout,
            AttributeNames=["MessageGroupId", "MessageDeduplicationId"],
        )
        envelopes: list[QueueEnvelope] = []
        for raw in result.get("Messages", []):
            command = OrderedRegistrationCommand.model_validate_json(raw["Body"])
            envelopes.append(QueueEnvelope(command=command, receipt_handle=raw["ReceiptHandle"]))
        return envelopes

    async def delete(self, receipt_handle: str) -> None:
        await asyncio.to_thread(
            self.sqs.delete_message,
            QueueUrl=self.fifo_url,
            ReceiptHandle=receipt_handle,
        )

    async def release(self, receipt_handle: str) -> None:
        """Make a failed command immediately available to the kick retry."""

        await asyncio.to_thread(
            self.sqs.change_message_visibility,
            QueueUrl=self.fifo_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=0,
        )
