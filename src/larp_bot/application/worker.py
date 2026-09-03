from __future__ import annotations

import logging
import time

from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.domain.models import Platform

from .ports import DeferredTransport, OrderedCommandConsumer, UserRepository
from .services import ConfirmationNotificationService, DomainError, OrderedMutationService

LOGGER = logging.getLogger("larp_bot.application.worker")


class OrderedWorker:
    def __init__(
        self,
        consumer: OrderedCommandConsumer,
        mutations: OrderedMutationService,
        users: UserRepository,
        transport: DeferredTransport,
        notifications: ConfirmationNotificationService | None = None,
        *,
        max_seconds: float = 40.0,
    ) -> None:
        self.consumer = consumer
        self.mutations = mutations
        self.users = users
        self.transport = transport
        self.notifications = notifications
        self.max_seconds = max_seconds

    async def _deliver_once(self, envelope: QueueEnvelope, text: str) -> None:
        command = envelope.command
        if command.platform is Platform.SYSTEM or command.platform_user_id <= 0:
            return
        user = await self.users.get(command.platform, command.platform_user_id)
        if user is not None and user.last_delivery_operation_id == command.operation_id:
            return
        await self.transport.send(
            platform=command.platform,
            user_id=command.platform_user_id,
            request_id=command.operation_id,
            text=text,
        )
        # Mark only after the transport accepted the message. If delivery fails, the FIFO
        # message remains retryable and the already-authoritative YDB mutation is not undone.
        await self.users.claim_delivery(command.platform, command.platform_user_id, command.operation_id)

    async def run(self) -> int:
        started = time.monotonic()
        processed = 0
        while time.monotonic() - started < self.max_seconds:
            received = await self.consumer.receive(max_messages=5, wait_seconds=1 if processed else 3)
            if not received:
                break
            for raw in received:
                if not isinstance(raw, QueueEnvelope):
                    raise TypeError("consumer returned an invalid queue envelope")
                command = raw.command
                notification_delivered = False
                try:
                    default_text = await self.mutations.apply(command)
                    text = command.reply_context.text_success or default_text
                    if command.operation in ConfirmationNotificationService.NOTIFICATION_OPERATIONS:
                        if self.notifications is None:
                            raise RuntimeError("confirmation notification service is not configured")
                        await self.notifications.notify(command)
                        notification_delivered = True
                except DomainError as exc:
                    text = command.reply_context.text_failure or f"❌ {exc}"
                if not notification_delivered:
                    await self._deliver_once(raw, text)
                await self.consumer.delete(raw.receipt_handle)
                LOGGER.info(
                    "ordered_command_completed",
                    extra={
                        "operation_id": command.operation_id,
                        "event_id": command.event_id,
                        "platform": command.platform.value,
                        "operation": command.operation.value,
                    },
                )
                processed += 1
        return processed
