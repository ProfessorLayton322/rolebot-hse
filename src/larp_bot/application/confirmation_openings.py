from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from larp_bot.domain.models import (
    ConfirmationDeadlinePayload,
    Event,
    EventStatus,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    ScheduledLifecycleEvent,
)

from .ports import EventRepository, OrderedCommandPublisher


@dataclass(frozen=True)
class ScheduledConfirmationDispatchResult:
    commands_enqueued: int


class ScheduledConfirmationOpeningDispatcher:
    """Put due confirmation openings into each game's authoritative FIFO group."""

    def __init__(self, events: EventRepository, publisher: OrderedCommandPublisher) -> None:
        self.events = events
        self.publisher = publisher

    @staticmethod
    def operation_id(event_id: str, opens_at: datetime) -> str:
        instant = opens_at.astimezone(UTC).isoformat()
        return str(uuid5(NAMESPACE_URL, f"larp-bot:scheduled-confirmation:{event_id}:{instant}"))

    async def dispatch_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        due_events: Sequence[ScheduledLifecycleEvent] | None = None,
    ) -> ScheduledConfirmationDispatchResult:
        opened_by = datetime.now(UTC) if now is None else now.astimezone(UTC)
        if due_events is None:
            due: Sequence[Event | ScheduledLifecycleEvent] = await self.events.list_due_confirmation_openings(
                opened_by,
                limit=limit,
            )
        else:
            due = [
                event
                for event in due_events
                if event.confirmation_open_at is not None
                and event.confirmation_open_at <= opened_by
                and event.confirmation_deadline is not None
                and event.archived_at is None
                and event.status is EventStatus.CREATED
            ][:limit]
        for event in due:
            if event.confirmation_open_at is None or event.confirmation_deadline is None:
                continue
            await self.publisher.publish(
                OrderedRegistrationCommand(
                    operation_id=self.operation_id(event.event_id, event.confirmation_open_at),
                    event_id=event.event_id,
                    operation=Operation.SCHEDULED_OPEN_CONFIRMATION,
                    platform=Platform.SYSTEM,
                    platform_user_id=0,
                    payload=ConfirmationDeadlinePayload(deadline=event.confirmation_deadline),
                )
            )
        return ScheduledConfirmationDispatchResult(commands_enqueued=len(due))
