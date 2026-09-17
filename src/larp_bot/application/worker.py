from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.domain.models import Button, Operation, Platform

from .ports import DeferredTransport, OrderedCommandConsumer, UserRepository
from .services import ConfirmationNotificationService, DomainError, OrderedMutationService
from .statistics import StatisticsService

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
        statistics: StatisticsService | None = None,
    ) -> None:
        self.consumer = consumer
        self.mutations = mutations
        self.users = users
        self.transport = transport
        self.notifications = notifications
        self.max_seconds = max_seconds
        self.statistics = statistics

    async def _deliver_once(self, envelope: QueueEnvelope, text: str, buttons: Sequence[Button]) -> None:
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
            buttons=buttons,
        )
        # Mark only after the transport accepted the message. If delivery fails, the FIFO
        # message remains retryable and the already-authoritative YDB mutation is not undone.
        await self.users.claim_delivery(
            command.platform,
            command.platform_user_id,
            command.operation_id,
            buttons,
        )

    async def run(self) -> int:
        started = time.monotonic()
        processed = 0
        failed_groups: set[str] = set()
        retryable_error: Exception | None = None
        while time.monotonic() - started < self.max_seconds:
            received = await self.consumer.receive(max_messages=5, wait_seconds=1 if processed else 3)
            if not received:
                break
            envelopes: list[QueueEnvelope] = []
            for raw in received:
                if not isinstance(raw, QueueEnvelope):
                    raise TypeError("consumer returned an invalid queue envelope")
                envelopes.append(raw)

            # SYNC_PROFILE has its own per-user group, so moving it behind
            # foreground commands cannot violate FIFO ordering. Its external
            # statistics lookup must not delay signup persistence and delivery.
            envelopes.sort(key=lambda envelope: envelope.command.operation is Operation.SYNC_PROFILE)
            for raw in envelopes:
                command = raw.command
                group_id = command.queue_group_id
                if group_id in failed_groups:
                    continue
                command_response_delivered = False
                # Profile projections are background maintenance. Keep stale
                # commands (for example, when a game was archived meanwhile)
                # silent even when worker-time validation rejects them.
                notifications_enabled = command.operation not in {
                    Operation.SYNC_PROFILE,
                    Operation.UPDATE_PROFILE,
                }
                buttons: Sequence[Button] = command.reply_context.buttons
                try:
                    if command.operation is Operation.SYNC_PROFILE:
                        if self.statistics is None:
                            raise RuntimeError("statistics service is not configured")
                        default_text = await self.statistics.sync_profile(command)
                    elif command.operation is Operation.STATISTICS:
                        if self.statistics is None:
                            raise RuntimeError("statistics service is not configured")
                        default_text = await self.statistics.apply(command)
                    else:
                        result = await self.mutations.apply(command)
                        default_text = result.text
                        notifications_enabled = result.notifications_enabled
                        if result.buttons is not None:
                            buttons = result.buttons
                    # ENLIST position and CONFIRM roster membership are decided only
                    # after their ordered writes; ignore text embedded by gateways
                    # from an older rollout.
                    text = (
                        default_text
                        if command.operation in {Operation.ENLIST, Operation.CONFIRM}
                        else command.reply_context.text_success or default_text
                    )
                    if (
                        notifications_enabled
                        and command.operation in ConfirmationNotificationService.NOTIFICATION_OPERATIONS
                    ):
                        if self.notifications is None:
                            raise RuntimeError("confirmation notification service is not configured")
                        await self.notifications.notify(command)
                    elif (
                        notifications_enabled
                        and command.operation is Operation.CONFIRM
                        and self.notifications is not None
                    ):
                        await self.notifications.notify_confirming_participant(
                            command,
                            text,
                            command.reply_context.buttons,
                        )
                        if result.replaced_event_id is not None:
                            await self.notifications.notify_reserve_promotion(
                                command,
                                event_id=result.replaced_event_id,
                            )
                            await self.notifications.notify_confirmed_link_promotion(
                                command,
                                event_id=result.replaced_event_id,
                            )
                            await self.notifications.notify_masters_of_replaced_participation(
                                command,
                                event_id=result.replaced_event_id,
                            )
                        command_response_delivered = True
                    elif notifications_enabled and command.operation in {
                        Operation.CANCEL,
                        Operation.REMOVE_PARTICIPANT,
                        Operation.REMOVE_WAITING_PARTICIPANTS,
                    }:
                        if self.notifications is not None:
                            if command.operation is Operation.REMOVE_PARTICIPANT:
                                await self.notifications.notify_removed_participants(command)
                            if command.operation is Operation.REMOVE_WAITING_PARTICIPANTS:
                                await self.notifications.notify_sifted_participants(command)
                            if command.operation in {Operation.CANCEL, Operation.REMOVE_PARTICIPANT}:
                                await self.notifications.notify_reserve_promotion(command)
                                await self.notifications.notify_confirmed_link_promotion(command)
                            if command.operation is Operation.CANCEL:
                                await self.notifications.notify_masters_of_cancellation(command)
                    elif (
                        notifications_enabled
                        and command.operation is Operation.UPDATE_PLAYER_AMOUNT
                        and self.notifications is not None
                    ):
                        await self.notifications.notify_capacity_promotions(command)
                except DomainError as exc:
                    text = command.reply_context.text_failure or f"❌ {exc}"
                except Exception as exc:
                    # Keep this command and every later command in the same FIFO
                    # group retryable, but continue draining independent groups.
                    # Re-raise after the drain so the kick itself is retried too.
                    failed_groups.add(group_id)
                    if retryable_error is None:
                        retryable_error = exc
                    LOGGER.exception(
                        "ordered_command_failed",
                        extra={
                            "operation_id": command.operation_id,
                            "event_id": command.event_id,
                            "platform": command.platform.value,
                            "operation": command.operation.value,
                        },
                    )
                    continue
                if notifications_enabled and not command_response_delivered:
                    await self._deliver_once(raw, text, buttons)
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
        if retryable_error is not None:
            raise retryable_error
        return processed
