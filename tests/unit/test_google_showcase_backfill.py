from __future__ import annotations

from datetime import date

from larp_bot.adapters.memory import MemoryCommandPublisher, MemoryEventRepository
from larp_bot.application.google_showcase_backfill import enqueue_google_showcase_backfill
from larp_bot.domain.models import DailyPassRefreshPayload, Event, Operation, Platform


async def test_backfill_queues_only_events_missing_a_google_showcase_copy() -> None:
    ready = Event(
        event_id="event-ready",
        name="Ready",
        disk_resource_path="disk:/larp-bot/events/ready-master.xlsx",
        public_registration_url="https://disk.example/ready-master",
        master_table_google_url="https://drive.example/ready-master",
        public_table_resource_path="disk:/larp-bot/events/ready-public.xlsx",
        public_table_public_url="https://disk.example/ready-public",
        public_table_google_url="https://drive.example/ready-public",
        pass_table_resource_path="disk:/larp-bot/passes/ready.xlsx",
        pass_table_public_url="https://disk.example/ready-pass",
        pass_table_google_url="https://drive.example/ready-pass",
        game_date=date(2026, 11, 14),
    )
    incomplete = Event(
        event_id="event-incomplete",
        name="Incomplete",
        disk_resource_path="disk:/larp-bot/events/incomplete-master.xlsx",
        public_registration_url="https://disk.example/incomplete-master",
    )
    events = MemoryEventRepository([ready, incomplete])
    publisher = MemoryCommandPublisher()

    first = await enqueue_google_showcase_backfill(events, publisher)
    second = await enqueue_google_showcase_backfill(events, publisher)

    assert first == second == 2
    assert len(publisher.commands) == 2
    event_command, daily_command = publisher.commands
    assert event_command.event_id == incomplete.event_id
    assert event_command.operation is Operation.REFRESH_EVENT_TABLES
    assert event_command.platform is Platform.SYSTEM
    assert event_command.platform_user_id == 0
    assert daily_command.operation is Operation.REFRESH_DAILY_PASS_TABLE
    assert daily_command.payload == DailyPassRefreshPayload(game_date=date(2026, 11, 14))
