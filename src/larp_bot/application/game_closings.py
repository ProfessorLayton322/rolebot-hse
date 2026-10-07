from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from larp_bot.domain.models import (
    Button,
    EmptyPayload,
    Event,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    ScheduledLifecycleEvent,
    UnreachableRecipient,
    event_reference,
)

from .navigation import main_menu_button
from .ports import DeferredTransport, EventRepository, OrderedCommandPublisher
from .services import DomainError, EventAdministrationService
from .statistics import StatisticsService

LOGGER = logging.getLogger("larp_bot.application.game_closings")


@dataclass(frozen=True)
class ScheduledClosingDispatchResult:
    commands_enqueued: int


class ScheduledGameClosingDispatcher:
    """Put due automatic closures into each game's authoritative FIFO group."""

    def __init__(self, events: EventRepository, publisher: OrderedCommandPublisher) -> None:
        self.events = events
        self.publisher = publisher

    @staticmethod
    def operation_id(event_id: str, scheduled_close_at: datetime) -> str:
        instant = scheduled_close_at.astimezone(UTC).isoformat()
        return str(uuid5(NAMESPACE_URL, f"larp-bot:scheduled-close:{event_id}:{instant}"))

    @staticmethod
    def stage_operation_id(operation_id: str, stage: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"larp-bot:{operation_id}:{stage}"))

    async def dispatch_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        due_events: Sequence[ScheduledLifecycleEvent] | None = None,
    ) -> ScheduledClosingDispatchResult:
        closed_by = datetime.now(UTC) if now is None else now.astimezone(UTC)
        if due_events is None:
            due: Sequence[Event | ScheduledLifecycleEvent] = await self.events.list_due_scheduled_closings(
                closed_by,
                limit=limit,
            )
        else:
            due = [
                event
                for event in due_events
                if event.scheduled_close_at is not None
                and event.scheduled_close_at <= closed_by
                and event.scheduled_close_completed_at is None
                and (event.archived_at is None or event.scheduled_close_operation_id is not None)
            ][:limit]
        for event in due:
            if event.scheduled_close_at is None:
                continue
            await self.publisher.publish(
                OrderedRegistrationCommand(
                    operation_id=self.operation_id(event.event_id, event.scheduled_close_at),
                    event_id=event.event_id,
                    operation=Operation.SCHEDULED_CLOSE_EVENT,
                    platform=Platform.SYSTEM,
                    platform_user_id=0,
                    payload=EmptyPayload(),
                )
            )
        return ScheduledClosingDispatchResult(commands_enqueued=len(due))


class ScheduledGameClosingService:
    """Apply and report every part of one scheduled close with retry state."""

    def __init__(
        self,
        events: EventRepository,
        statistics: StatisticsService,
        transport: DeferredTransport,
        publisher: OrderedCommandPublisher,
        administration: EventAdministrationService | None = None,
    ) -> None:
        self.events = events
        self.statistics = statistics
        self.transport = transport
        self.publisher = publisher
        self.administration = administration

    @staticmethod
    def _recipient(identity_platform: Platform, identity_user_id: int) -> str:
        return f"{identity_platform.value}:{identity_user_id}"

    @staticmethod
    def _command(event_id: str, operation_id: str, operation: Operation) -> OrderedRegistrationCommand:
        return OrderedRegistrationCommand(
            operation_id=operation_id,
            event_id=event_id,
            operation=operation,
            platform=Platform.SYSTEM,
            platform_user_id=0,
            payload=EmptyPayload(),
        )

    async def _event(self, command: OrderedRegistrationCommand) -> tuple[Event, str]:
        event = await self.events.get(command.event_id)
        if event is None:
            raise DomainError("Игра уже удалена")
        if event.scheduled_close_at is None:
            raise DomainError("Для игры не запланировано закрытие")
        operation_id = ScheduledGameClosingDispatcher.operation_id(event.event_id, event.scheduled_close_at)
        expected_id = {
            Operation.SCHEDULED_CLOSE_EVENT: operation_id,
            Operation.SCHEDULED_CLOSE_STATISTICS: ScheduledGameClosingDispatcher.stage_operation_id(
                operation_id, "statistics"
            ),
            Operation.SCHEDULED_CLOSE_FINALIZE: ScheduledGameClosingDispatcher.stage_operation_id(
                operation_id, "finalize"
            ),
        }.get(command.operation)
        if expected_id is None or command.operation_id != expected_id:
            # A master may have changed or removed the schedule after this
            # command was queued. It is stale, not retryable.
            raise DomainError("Запланированное закрытие было изменено или отменено")
        return event, operation_id

    async def _schedule_next(self, event_id: str, operation_id: str, operation: Operation) -> None:
        stage = "statistics" if operation is Operation.SCHEDULED_CLOSE_STATISTICS else "finalize"
        await self.publisher.publish(
            self._command(
                event_id,
                ScheduledGameClosingDispatcher.stage_operation_id(operation_id, stage),
                operation,
            )
        )

    async def _start(self, command: OrderedRegistrationCommand) -> str:
        event, operation_id = await self._event(command)
        if event.scheduled_close_completed_at is not None:
            return "Запланированное закрытие уже выполнено"
        if event.scheduled_close_operation_id is None:
            if event.archived_at is not None:
                return "Игра уже архивирована вручную"
            if not await self.events.start_scheduled_close(
                event.event_id,
                operation_id,
                datetime.now(UTC),
            ):
                raise RuntimeError("scheduled close start was not persisted")
            refreshed = await self.events.get(event.event_id)
            if refreshed is None:
                return "Игра удалена после начала запланированного закрытия"
            event = refreshed
        elif event.scheduled_close_operation_id != operation_id:
            raise RuntimeError("scheduled close operation does not match persisted state")
        next_operation = (
            Operation.SCHEDULED_CLOSE_FINALIZE
            if event.archived_at is not None
            else Operation.SCHEDULED_CLOSE_STATISTICS
        )
        await self._schedule_next(event.event_id, operation_id, next_operation)
        return "Регистрация закрыта; добавление в статистику поставлено в очередь"

    async def _add_statistics(self, command: OrderedRegistrationCommand) -> str:
        event, operation_id = await self._event(command)
        if event.scheduled_close_completed_at is not None:
            return "Запланированное закрытие уже выполнено"
        if event.scheduled_close_operation_id != operation_id:
            raise RuntimeError("scheduled close has not started")
        if event.archived_at is None:
            await self.statistics.add_game(event.event_id, command.operation_id)
        await self._schedule_next(event.event_id, operation_id, Operation.SCHEDULED_CLOSE_FINALIZE)
        return "Игра добавлена в статистику; архивация поставлена в очередь"

    async def _finalize(self, command: OrderedRegistrationCommand) -> str:
        event, operation_id = await self._event(command)
        if event.scheduled_close_completed_at is not None:
            return "Запланированное закрытие уже выполнено"
        if event.scheduled_close_operation_id != operation_id:
            raise RuntimeError("scheduled close has not started")
        if not await self.events.complete_scheduled_close_actions(
            event.event_id,
            operation_id,
            datetime.now(UTC),
        ):
            raise RuntimeError("scheduled close actions were not persisted")
        refreshed = await self.events.get(event.event_id)
        if refreshed is None:
            return "Игра удалена после запланированного закрытия"
        event = refreshed
        if self.administration is not None:
            await self.administration.refresh_daily_pass_table(event.game_date)

        leaders = await self.events.list_leaders(event.event_id)
        notified = set(event.scheduled_close_notified_recipients)
        text = (
            f"Автоматическое закрытие игры {event_reference(event.name, event.game_date)} выполнено:\n\n"
            "— регистрация закрыта;\n"
            "— игра добавлена в статистику посещений;\n"
            "— игра архивирована."
        )
        buttons: list[Button] = [main_menu_button()]
        for leader in leaders:
            recipient = self._recipient(leader.platform, leader.platform_user_id)
            if recipient in notified or leader.platform is Platform.SYSTEM:
                continue
            request_id = f"{operation_id}:{recipient}"
            try:
                await self.transport.send(
                    platform=leader.platform,
                    user_id=leader.platform_user_id,
                    request_id=request_id,
                    text=text,
                    buttons=buttons,
                )
            except UnreachableRecipient:
                LOGGER.warning(
                    "scheduled_close_recipient_unreachable",
                    extra={
                        "event_id": event.event_id,
                        "platform": leader.platform.value,
                        "platform_user_id": leader.platform_user_id,
                    },
                )
            await self.events.mark_scheduled_close_notification_delivered(
                event.event_id,
                operation_id,
                recipient,
            )
            notified.add(recipient)

        await self.events.complete_scheduled_close_notifications(
            event.event_id,
            operation_id,
            datetime.now(UTC),
        )
        return "Игра закрыта, добавлена в статистику и архивирована; мастера уведомлены"

    async def apply(self, command: OrderedRegistrationCommand) -> str:
        if command.operation is Operation.SCHEDULED_CLOSE_EVENT:
            return await self._start(command)
        if command.operation is Operation.SCHEDULED_CLOSE_STATISTICS:
            return await self._add_statistics(command)
        if command.operation is Operation.SCHEDULED_CLOSE_FINALIZE:
            return await self._finalize(command)
        raise ValueError("scheduled close service received another operation")
