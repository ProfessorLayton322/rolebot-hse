from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from larp_bot.adapters.memory import MemoryDeferredTransport, MemoryEventRepository, StaticAdminProvider
from larp_bot.application.navigation import MAIN_MENU, MAIN_MENU_LABEL
from larp_bot.application.registration_openings import RegistrationOpeningNotificationService
from larp_bot.domain.models import Button, Event, EventStatus, Platform


def scheduled_event(event_id: str, opening: datetime, *, status: EventStatus = EventStatus.CREATED) -> Event:
    return Event(
        event_id=event_id,
        name="Лесной предел",
        game_date=datetime(2026, 11, 14, tzinfo=UTC).date(),
        disk_resource_path=f"disk:/larp-bot/events/{event_id}/master.xlsx",
        public_registration_url=f"https://disk.example/{event_id}",
        status=status,
        registration_open_at=opening,
    )


class FailVkOnceTransport(MemoryDeferredTransport):
    def __init__(self) -> None:
        super().__init__()
        self.fail_vk = True

    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        if platform is Platform.VK and self.fail_vk:
            raise RuntimeError("VK temporarily unavailable")
        await super().send(
            platform=platform,
            user_id=user_id,
            request_id=request_id,
            text=text,
            buttons=buttons,
        )


@pytest.mark.asyncio
async def test_due_registration_notifies_each_configured_admin_and_moderator_once() -> None:
    now = datetime(2026, 9, 18, 15, tzinfo=UTC)
    due = scheduled_event("event-due", now - timedelta(seconds=1))
    future = scheduled_event("event-future", now + timedelta(minutes=2))
    immediate = scheduled_event("event-immediate", now)
    immediate.registration_open_at = None
    closed = scheduled_event("event-closed", now - timedelta(minutes=1), status=EventStatus.CLOSED)
    events = MemoryEventRepository([due, future, immediate, closed])
    transport = MemoryDeferredTransport()
    service = RegistrationOpeningNotificationService(
        events,
        StaticAdminProvider(tg_ids={10, 11}, tg_gamemaster_ids={11}, vk_gamemaster_ids={20}),
        transport,
    )

    assert due.registration_is_open(now)
    result = await service.notify_due(now=now)

    assert result.events_completed == 1
    assert result.notifications_delivered == 3
    assert {(sent[0], sent[1]) for sent in transport.sent} == {
        (Platform.TELEGRAM, 10),
        (Platform.TELEGRAM, 11),
        (Platform.VK, 20),
    }
    assert {sent[3] for sent in transport.sent} == {"Регистрация на игру «Лесной предел» (14.11.26) открыта."}
    assert all(sent[4] == (Button(label=MAIN_MENU_LABEL, value=MAIN_MENU),) for sent in transport.sent)
    stored = await events.get(due.event_id)
    assert stored is not None
    assert set(stored.registration_open_notified_recipients) == {
        "telegram:10",
        "telegram:11",
        "vk:20",
    }
    assert stored.registration_open_notification_completed_at == now

    repeated = await service.notify_due(now=now + timedelta(minutes=1))
    assert repeated.events_completed == 0
    assert repeated.notifications_delivered == 0
    assert len(transport.sent) == 3


@pytest.mark.asyncio
async def test_failed_notification_retries_without_affecting_open_registration() -> None:
    now = datetime(2026, 9, 18, 15, tzinfo=UTC)
    event = scheduled_event("event-retry", now - timedelta(minutes=1))
    events = MemoryEventRepository([event])
    transport = FailVkOnceTransport()
    service = RegistrationOpeningNotificationService(
        events,
        StaticAdminProvider(tg_ids={10}, vk_gamemaster_ids={20}),
        transport,
    )

    first = await service.notify_due(now=now)

    stored = await events.get(event.event_id)
    assert stored is not None and stored.registration_is_open(now)
    assert stored.registration_open_notified_recipients == ["telegram:10"]
    assert stored.registration_open_notification_completed_at is None
    assert first.notifications_delivered == 1

    transport.fail_vk = False
    second = await service.notify_due(now=now + timedelta(minutes=1))

    assert [(sent[0], sent[1]) for sent in transport.sent] == [
        (Platform.TELEGRAM, 10),
        (Platform.VK, 20),
    ]
    assert second.notifications_delivered == 1
    assert second.events_completed == 1
    stored = await events.get(event.event_id)
    assert stored is not None
    assert stored.registration_open_notification_completed_at == now + timedelta(minutes=1)
