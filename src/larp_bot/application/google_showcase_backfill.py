from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from larp_bot.domain.models import (
    MAX_EVENT_PAGE_SIZE,
    DailyPassRefreshPayload,
    EmptyPayload,
    Operation,
    OrderedRegistrationCommand,
    Platform,
)

from .ports import EventRepository, OrderedCommandPublisher
from .table_links import google_event_tables_ready


async def enqueue_google_showcase_backfill(
    events: EventRepository,
    publisher: OrderedCommandPublisher,
) -> int:
    """Queue one idempotent Google showcase refresh for every incomplete event."""

    queued = 0
    after = None
    active_game_dates = set()
    while True:
        page = list(await events.list_page(after=after, limit=MAX_EVENT_PAGE_SIZE))
        for event in page:
            if event.archived_at is None and event.game_date is not None:
                active_game_dates.add(event.game_date)
            if google_event_tables_ready(event):
                continue
            operation_id = str(uuid5(NAMESPACE_URL, f"larp-bot:google-showcase:{event.event_id}"))
            await publisher.publish(
                OrderedRegistrationCommand(
                    operation_id=operation_id,
                    event_id=event.event_id,
                    operation=Operation.REFRESH_EVENT_TABLES,
                    platform=Platform.SYSTEM,
                    platform_user_id=0,
                    payload=EmptyPayload(),
                )
            )
            queued += 1
        if len(page) < MAX_EVENT_PAGE_SIZE:
            break
        last = page[-1]
        after = (last.created_at, last.event_id)
    for game_date in sorted(active_game_dates):
        date_group = f"daily-passes:{game_date.isoformat()}"
        await publisher.publish(
            OrderedRegistrationCommand(
                operation_id=str(uuid5(NAMESPACE_URL, f"larp-bot:google-showcase:{date_group}")),
                event_id=date_group,
                operation=Operation.REFRESH_DAILY_PASS_TABLE,
                platform=Platform.SYSTEM,
                platform_user_id=0,
                payload=DailyPassRefreshPayload(game_date=game_date),
            )
        )
        queued += 1
    return queued
