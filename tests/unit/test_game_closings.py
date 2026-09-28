from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from larp_bot.adapters.memory import MemoryCommandPublisher, MemoryDeferredTransport, MemoryEventRepository
from larp_bot.application.game_closings import ScheduledGameClosingDispatcher, ScheduledGameClosingService
from larp_bot.application.services import DomainError
from larp_bot.domain.models import (
    BotIdentity,
    Button,
    Event,
    EventStatus,
    Platform,
    UnreachableRecipient,
)


def scheduled_event(event_id: str, closing: datetime) -> Event:
    return Event(
        event_id=event_id,
        name="Лесной предел",
        game_date=date(2026, 11, 14),
        disk_resource_path=f"disk:/larp-bot/events/{event_id}/master.xlsx",
        public_registration_url=f"https://disk.example/{event_id}",
        status=EventStatus.CONFIRMATION_OPEN,
        scheduled_close_at=closing,
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


class BlockTelegramTransport(MemoryDeferredTransport):
    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        del platform, user_id, request_id, text, buttons
        raise UnreachableRecipient("bot was blocked")


@pytest.mark.asyncio
async def test_due_closing_dispatch_uses_one_deterministic_per_game_command() -> None:
    now = datetime(2026, 11, 15, tzinfo=UTC)
    due = scheduled_event("event-due", now - timedelta(seconds=1))
    future = scheduled_event("event-future", now + timedelta(minutes=1))
    events = MemoryEventRepository([due, future])
    publisher = MemoryCommandPublisher()
    dispatcher = ScheduledGameClosingDispatcher(events, publisher)

    first = await dispatcher.dispatch_due(now=now)
    second = await dispatcher.dispatch_due(now=now)

    assert first.commands_enqueued == 1
    assert second.commands_enqueued == 1
    assert len(publisher.commands) == 1
    command = publisher.commands[0]
    assert command.event_id == due.event_id
    assert command.operation.value == "SCHEDULED_CLOSE_EVENT"
    assert command.platform is Platform.SYSTEM
    assert command.operation_id == dispatcher.operation_id(due.event_id, due.scheduled_close_at)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_changed_closing_schedule_makes_an_earlier_command_non_retryable() -> None:
    closing = datetime(2026, 11, 15, tzinfo=UTC)
    event = scheduled_event("event-changed", closing)
    events = MemoryEventRepository([event])
    publisher = MemoryCommandPublisher()
    await ScheduledGameClosingDispatcher(events, publisher).dispatch_due(now=closing)
    await events.set_scheduled_close_at(event.event_id, closing + timedelta(hours=1))
    service = ScheduledGameClosingService(
        events,
        AsyncMock(),
        MemoryDeferredTransport(),
        publisher,
    )

    with pytest.raises(DomainError, match="изменено или отменено"):
        await service.apply(publisher.commands[0])


@pytest.mark.asyncio
async def test_scheduled_close_adds_statistics_archives_and_notifies_every_master() -> None:
    closing = datetime(2026, 11, 15, tzinfo=UTC)
    event = scheduled_event("event-due", closing)
    events = MemoryEventRepository([event])
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=10))
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.VK, platform_user_id=20))
    publisher = MemoryCommandPublisher()
    await ScheduledGameClosingDispatcher(events, publisher).dispatch_due(now=closing)
    command = publisher.commands[0]
    statistics = AsyncMock()
    statistics.add_game.return_value = "added"
    transport = MemoryDeferredTransport()
    service = ScheduledGameClosingService(events, statistics, transport, publisher)

    started = await service.apply(command)
    stored = await events.get(event.event_id)
    assert started == "Регистрация закрыта; добавление в статистику поставлено в очередь"
    assert stored is not None and stored.status is EventStatus.CLOSED
    assert stored.archived_at is None
    statistics_command = publisher.commands[-1]
    assert statistics_command.operation.value == "SCHEDULED_CLOSE_STATISTICS"
    assert statistics_command.queue_group_id == "statistics"

    await service.apply(statistics_command)
    finalize_command = publisher.commands[-1]
    assert finalize_command.operation.value == "SCHEDULED_CLOSE_FINALIZE"
    assert finalize_command.queue_group_id == event.event_id
    result = await service.apply(finalize_command)

    assert result == "Игра закрыта, добавлена в статистику и архивирована; мастера уведомлены"
    statistics.add_game.assert_awaited_once_with(event.event_id, statistics_command.operation_id)
    stored = await events.get(event.event_id)
    assert stored is not None
    assert stored.status is EventStatus.CLOSED
    assert stored.archived_at is not None
    assert stored.scheduled_close_operation_id == command.operation_id
    assert stored.scheduled_close_completed_at is not None
    assert set(stored.scheduled_close_notified_recipients) == {"telegram:10", "vk:20"}
    assert {(sent[0], sent[1]) for sent in transport.sent} == {
        (Platform.TELEGRAM, 10),
        (Platform.VK, 20),
    }
    assert all(
        sent[3]
        == (
            "Автоматическое закрытие игры «Лесной предел» (14.11.26) выполнено:\n\n"
            "— регистрация закрыта;\n"
            "— игра добавлена в статистику посещений;\n"
            "— игра архивирована."
        )
        for sent in transport.sent
    )


@pytest.mark.asyncio
async def test_scheduled_close_retries_only_missing_master_notifications() -> None:
    closing = datetime(2026, 11, 15, tzinfo=UTC)
    event = scheduled_event("event-retry", closing)
    events = MemoryEventRepository([event])
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=10))
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.VK, platform_user_id=20))
    publisher = MemoryCommandPublisher()
    await ScheduledGameClosingDispatcher(events, publisher).dispatch_due(now=closing)
    command = publisher.commands[0]
    statistics = AsyncMock()
    statistics.add_game.return_value = "added"
    transport = FailVkOnceTransport()
    service = ScheduledGameClosingService(events, statistics, transport, publisher)

    await service.apply(command)
    await service.apply(publisher.commands[-1])
    finalize_command = publisher.commands[-1]

    with pytest.raises(RuntimeError, match="VK temporarily unavailable"):
        await service.apply(finalize_command)
    partly_closed = await events.get(event.event_id)
    assert partly_closed is not None and partly_closed.archived_at is not None
    assert partly_closed.scheduled_close_notified_recipients == ["telegram:10"]
    assert partly_closed.scheduled_close_completed_at is None

    transport.fail_vk = False
    await service.apply(finalize_command)

    statistics.add_game.assert_awaited_once()
    assert [(sent[0], sent[1]) for sent in transport.sent] == [
        (Platform.TELEGRAM, 10),
        (Platform.VK, 20),
    ]


@pytest.mark.asyncio
async def test_unreachable_master_does_not_block_scheduled_close_completion() -> None:
    closing = datetime(2026, 11, 15, tzinfo=UTC)
    event = scheduled_event("event-blocked", closing)
    events = MemoryEventRepository([event])
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=10))
    publisher = MemoryCommandPublisher()
    await ScheduledGameClosingDispatcher(events, publisher).dispatch_due(now=closing)
    statistics = AsyncMock()
    statistics.add_game.return_value = "added"

    service = ScheduledGameClosingService(events, statistics, BlockTelegramTransport(), publisher)
    await service.apply(publisher.commands[0])
    await service.apply(publisher.commands[-1])
    await service.apply(publisher.commands[-1])

    stored = await events.get(event.event_id)
    assert stored is not None and stored.scheduled_close_completed_at is not None
    assert stored.scheduled_close_notified_recipients == ["telegram:10"]
