from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from larp_bot.adapters.memory import MemoryRegistrationRepository
from larp_bot.adapters.ydb.repositories import YdbRegistrationRepository
from larp_bot.domain.models import AttendanceStatus, Registration


@pytest.mark.asyncio
async def test_registration_lifecycle_is_stored_per_event_and_idempotent() -> None:
    repository = MemoryRegistrationRepository()
    key = "a" * 43

    assert await repository.enlist(
        "event-a1",
        operation_id="enlist",
        participant_key=key,
        display_name="Player",
        wish_play="A",
        larp_experience=True,
        crossplay=False,
    )
    assert not await repository.enlist(
        "event-a1",
        operation_id="enlist",
        participant_key=key,
        display_name="Player",
        wish_play="A",
        larp_experience=True,
        crossplay=False,
    )
    await repository.confirm(
        "event-a1",
        operation_id="confirm",
        participant_key=key,
        character_wish="Doctor",
    )
    await repository.cancel("event-a1", operation_id="cancel", participant_key=key)
    await repository.enlist(
        "event-a1",
        operation_id="reenlist",
        participant_key=key,
        display_name="Player",
        wish_play="B",
    )

    registration = await repository.get("event-a1", key)
    assert registration is not None
    assert registration.character_wish == "Doctor"
    assert registration.attendance_status is AttendanceStatus.WAITING
    assert registration.wish_play == "B"
    assert await repository.get("event-b1", key) is None


@pytest.mark.asyncio
async def test_legacy_import_never_overwrites_newer_ydb_state() -> None:
    current = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Current",
        wish_play="Current wish",
        last_operation_id="new-op",
    )
    stale = current.model_copy(update={"display_name": "Stale", "wish_play": "Old wish", "last_operation_id": "old-op"})
    repository = MemoryRegistrationRepository([current])

    await repository.import_missing([stale])

    assert await repository.get(current.event_id, current.participant_key) == current


@pytest.mark.asyncio
async def test_event_listing_is_stable_by_creation_time() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    later = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Later",
        wish_play="A",
        created_at=base + timedelta(seconds=1),
    )
    earlier = Registration(
        event_id="event-a1",
        participant_key="b" * 43,
        display_name="Earlier",
        wish_play="B",
        created_at=base,
    )
    repository = MemoryRegistrationRepository([later, earlier])

    assert [row.display_name for row in await repository.list_for_event("event-a1")] == ["Earlier", "Later"]


class RecordingExecutor:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def query(
        self,
        yql: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        del read_only
        self.queries.append((yql, params or {}))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_ydb_enlist_uses_composite_key_and_persists_showcase_fields() -> None:
    executor = RecordingExecutor([[], []])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.enlist(
        "event-a1",
        operation_id="operation",
        participant_key="a" * 43,
        display_name="Player",
        wish_play="Anyone",
        larp_experience=True,
        crossplay=False,
        vk_profile="https://vk.com/player",
        telegram_profile="https://t.me/player",
    )

    lookup, upsert = executor.queries
    assert "event_id = $event_id AND participant_key = $participant_key" in lookup[0]
    assert "UPSERT INTO `registrations`" in upsert[0]
    assert upsert[1]["$event_id"] == "event-a1"
    assert upsert[1]["$larp_experience"] is True
    assert upsert[1]["$crossplay"] is False
    assert upsert[1]["$vk_profile"] == "https://vk.com/player"
    assert upsert[1]["$telegram_profile"] == "https://t.me/player"


@pytest.mark.asyncio
async def test_ydb_confirmation_updates_wish_and_status_in_one_query() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.confirm(
        "event-a1",
        operation_id="confirm",
        participant_key="a" * 43,
        character_wish="Doctor",
    )

    query, params = executor.queries[0]
    assert "SET character_wish = $character_wish" in query
    assert "attendance_status = $attendance_status" in query
    assert "last_operation_id != $operation_id" in query
    assert params["$attendance_status"] == AttendanceStatus.CONFIRMED.value
