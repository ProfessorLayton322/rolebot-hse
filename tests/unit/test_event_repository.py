from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from larp_bot.adapters.ydb.repositories import YdbEventRepository
from larp_bot.domain.models import EventStatus


def event_row(status: str) -> dict[str, Any]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "event_id": "event-a1",
        "name": "Game",
        "disk_resource_path": "disk:/larp-bot/events/event-a1-game.xlsx",
        "public_registration_url": "https://disk.example/game",
        "status": status,
        "created_at": now,
        "updated_at": now,
    }


class RecordingExecutor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.query_text = ""
        self.params: dict[str, Any] = {}

    async def query(
        self,
        yql: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        del read_only
        self.query_text = yql
        self.params = params or {}
        return self.rows


def test_legacy_open_status_is_read_as_confirmation_open() -> None:
    event = YdbEventRepository._from_row(event_row("OPEN"))
    assert event.status is EventStatus.CONFIRMATION_OPEN


@pytest.mark.asyncio
async def test_confirmation_open_filter_includes_legacy_open_rows() -> None:
    executor = RecordingExecutor([event_row("OPEN")])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    events = await repository.list_page(
        statuses={EventStatus.CREATED, EventStatus.CONFIRMATION_OPEN},
        limit=10,
    )

    assert events[0].status is EventStatus.CONFIRMATION_OPEN
    status_values = {value for key, value in executor.params.items() if key.startswith("$status_")}
    assert status_values == {"CREATED", "CONFIRMATION_OPEN", "OPEN"}


@pytest.mark.asyncio
async def test_open_confirmation_persists_deadline_with_status() -> None:
    executor = RecordingExecutor([event_row("CREATED")])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]
    deadline = datetime(2026, 9, 10, 16, tzinfo=UTC)

    assert await repository.open_confirmation("event-a1", deadline)

    assert "confirmation_deadline = $deadline" in executor.query_text
    assert executor.params["$status"] == EventStatus.CONFIRMATION_OPEN.value
    assert executor.params["$deadline"] == deadline


@pytest.mark.asyncio
async def test_pass_table_link_is_persisted_once_on_the_event() -> None:
    executor = RecordingExecutor([event_row("CLOSED")])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    assert await repository.set_pass_table(
        "event-a1",
        "disk:/larp-bot/passes/event-a1.xlsx",
        "https://disk.example/pass",
    )

    assert "pass_table_public_url IS NULL" in executor.query_text
    assert executor.params["$resource_path"] == "disk:/larp-bot/passes/event-a1.xlsx"
    assert executor.params["$public_url"] == "https://disk.example/pass"


@pytest.mark.asyncio
async def test_public_game_table_link_is_persisted_once_on_the_event() -> None:
    executor = RecordingExecutor([event_row("CREATED")])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    assert await repository.set_public_table(
        "event-a1",
        "disk:/larp-bot/events/event-a1/public_table_Game.xlsx",
        "https://disk.example/public-game",
    )

    assert "public_table_public_url IS NULL" in executor.query_text
    assert executor.params["$resource_path"].endswith("/public_table_Game.xlsx")
    assert executor.params["$public_url"] == "https://disk.example/public-game"


@pytest.mark.asyncio
async def test_pass_table_listing_filters_events_with_stored_links() -> None:
    row = event_row("CLOSED") | {
        "pass_table_resource_path": "disk:/larp-bot/passes/event-a1.xlsx",
        "pass_table_public_url": "https://disk.example/pass",
    }
    executor = RecordingExecutor([row])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    events = await repository.list_pass_tables_page(limit=10)

    assert events[0].pass_table_public_url == "https://disk.example/pass"
    assert "WHERE pass_table_public_url IS NOT NULL" in executor.query_text
