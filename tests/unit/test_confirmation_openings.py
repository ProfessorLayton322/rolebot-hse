from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from larp_bot.adapters.memory import (
    MemoryCommandPublisher,
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
)
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.application.confirmation_openings import ScheduledConfirmationOpeningDispatcher
from larp_bot.application.services import (
    ConfirmationNotificationService,
    OrderedMutationService,
    RegistrationCatalog,
)
from larp_bot.domain.models import Event, EventStatus, Platform, TelegramUser
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore


def scheduled_event(event_id: str, opens_at: datetime, deadline: datetime) -> Event:
    return Event(
        event_id=event_id,
        name="Лесной предел",
        game_date=date(2026, 11, 14),
        disk_resource_path=f"disk:/larp-bot/events/{event_id}/master.xlsx",
        public_registration_url=f"https://disk.example/{event_id}",
        status=EventStatus.CREATED,
        confirmation_open_at=opens_at,
        confirmation_deadline=deadline,
    )


@pytest.mark.asyncio
async def test_due_confirmation_dispatch_is_deterministic_and_ignores_future_games() -> None:
    now = datetime(2026, 11, 10, 15, tzinfo=UTC)
    due = scheduled_event("event-due", now - timedelta(seconds=1), now + timedelta(days=1))
    future = scheduled_event("event-future", now + timedelta(minutes=1), now + timedelta(days=1))
    events = MemoryEventRepository([due, future])
    publisher = MemoryCommandPublisher()
    dispatcher = ScheduledConfirmationOpeningDispatcher(events, publisher)

    first = await dispatcher.dispatch_due(now=now)
    second = await dispatcher.dispatch_due(now=now)

    assert first.commands_enqueued == 1
    assert second.commands_enqueued == 1
    assert len(publisher.commands) == 1
    command = publisher.commands[0]
    assert command.operation.value == "SCHEDULED_OPEN_CONFIRMATION"
    assert command.platform is Platform.SYSTEM
    assert command.operation_id == dispatcher.operation_id(due.event_id, due.confirmation_open_at)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_scheduled_confirmation_opens_and_notifies_waiting_players(
    disk_store: MemoryDiskStore,
) -> None:
    secret = "participant-secret"
    opens_at = datetime(2026, 11, 10, 15, tzinfo=UTC)
    deadline = datetime(2026, 11, 12, 16, tzinfo=UTC)
    event = scheduled_event("event-due", opens_at, deadline)
    events = MemoryEventRepository([event])
    registrations = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=2))
    await registrations.enlist(
        event.event_id,
        operation_id="enlist-player",
        participant_key=participant_key(secret, Platform.TELEGRAM, 2, event.event_id),
        display_name="Player",
        wish_play="Без пожеланий",
    )
    catalog = RegistrationCatalog(events, registrations, YandexDiskShowcaseRepository(disk_store))
    publisher = MemoryCommandPublisher()
    await ScheduledConfirmationOpeningDispatcher(events, publisher).dispatch_due(now=opens_at)
    command = publisher.commands[0]

    result = await OrderedMutationService(events, catalog, users, secret).apply(command)
    transport = MemoryDeferredTransport()
    delivered = await ConfirmationNotificationService(events, catalog, users, transport, secret).notify(command)

    stored = await events.get(event.event_id)
    assert result.notifications_enabled
    assert stored is not None and stored.status is EventStatus.CONFIRMATION_OPEN
    assert stored.confirmation_open_at is None
    assert stored.confirmation_deadline == deadline
    assert delivered == 1
    assert transport.sent[0][0:2] == (Platform.TELEGRAM, 2)
    assert "Подтверждение участия" in transport.sent[0][3]
    assert "12.11.26 19:00 (мск)" in transport.sent[0][3]


@pytest.mark.asyncio
async def test_changed_confirmation_schedule_makes_an_earlier_command_stale(
    disk_store: MemoryDiskStore,
) -> None:
    opens_at = datetime(2026, 11, 10, 15, tzinfo=UTC)
    deadline = datetime(2026, 11, 12, 16, tzinfo=UTC)
    event = scheduled_event("event-due", opens_at, deadline)
    events = MemoryEventRepository([event])
    publisher = MemoryCommandPublisher()
    await ScheduledConfirmationOpeningDispatcher(events, publisher).dispatch_due(now=opens_at)
    await events.set_confirmation_schedule(
        event.event_id,
        opens_at + timedelta(hours=1),
        deadline + timedelta(hours=1),
    )
    catalog = RegistrationCatalog(
        events,
        MemoryRegistrationRepository(),
        YandexDiskShowcaseRepository(disk_store),
    )

    result = await OrderedMutationService(events, catalog).apply(publisher.commands[0])

    stored = await events.get(event.event_id)
    assert result.text == "Расписание открытия подтверждения было изменено"
    assert not result.notifications_enabled
    assert stored is not None and stored.status is EventStatus.CREATED
