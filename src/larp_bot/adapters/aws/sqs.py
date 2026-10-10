from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.domain.models import OrderedRegistrationCommand


class SqsFifoCommandPublisher:
    """Publish directly to the AWS FIFO queue consumed by Lambda."""

    def __init__(self, client: Any, queue_url: str) -> None:
        self.client = client
        self.queue_url = queue_url

    async def publish(self, command: OrderedRegistrationCommand) -> None:
        await asyncio.to_thread(
            self.client.send_message,
            QueueUrl=self.queue_url,
            MessageBody=command.model_dump_json(),
            MessageGroupId=command.queue_group_id,
            MessageDeduplicationId=command.operation_id,
        )

    async def continue_if_pending(self, *, processed: int) -> None:
        del processed


class LambdaBatchConsumer:
    """Expose one Lambda SQS batch through the existing ordered-worker port."""

    def __init__(self) -> None:
        self._envelopes: list[QueueEnvelope] = []
        self._message_ids: list[str] = []
        self._delivered = False
        self._completed: set[str] = set()

    def reset(self, event: Mapping[str, Any]) -> None:
        records = event.get("Records")
        if not isinstance(records, list):
            raise ValueError("SQS event Records must be a list")
        envelopes: list[QueueEnvelope] = []
        message_ids: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("SQS record must be an object")
            message_id = record.get("messageId")
            body = record.get("body")
            attributes = record.get("attributes")
            if not isinstance(message_id, str) or not message_id or not isinstance(body, str):
                raise ValueError("SQS record is missing its message ID or body")
            if not isinstance(attributes, dict):
                raise ValueError("SQS record attributes must be an object")
            command = OrderedRegistrationCommand.model_validate_json(body)
            if attributes.get("MessageGroupId") != command.queue_group_id:
                raise ValueError("SQS message group does not match the ordered command")
            envelopes.append(QueueEnvelope(command=command, receipt_handle=message_id))
            message_ids.append(message_id)
        self._envelopes = envelopes
        self._message_ids = message_ids
        self._completed = set()
        self._delivered = False

    async def receive(self, *, max_messages: int, wait_seconds: int) -> Sequence[QueueEnvelope]:
        del max_messages, wait_seconds
        if self._delivered:
            return []
        self._delivered = True
        return list(self._envelopes)

    async def delete(self, receipt_handle: str) -> None:
        if receipt_handle not in self._message_ids:
            raise ValueError("worker completed an unknown SQS message")
        self._completed.add(receipt_handle)

    async def release(self, receipt_handle: str) -> None:
        if receipt_handle not in self._message_ids:
            raise ValueError("worker released an unknown SQS message")

    def batch_response(self) -> dict[str, list[dict[str, str]]]:
        return {
            "batchItemFailures": [
                {"itemIdentifier": message_id} for message_id in self._message_ids if message_id not in self._completed
            ]
        }
