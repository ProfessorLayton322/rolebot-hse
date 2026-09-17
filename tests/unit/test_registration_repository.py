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
async def test_reenlist_after_cancellation_moves_registration_to_end() -> None:
    base = datetime(2000, 1, 1, tzinfo=UTC)
    cancelled = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Returning player",
        wish_play="A",
        character_wish="Doctor",
        attendance_status=AttendanceStatus.CANCELLED,
        created_at=base,
        updated_at=base,
    )
    active = Registration(
        event_id="event-a1",
        participant_key="b" * 43,
        display_name="Active player",
        wish_play="B",
        created_at=base + timedelta(days=1),
        updated_at=base + timedelta(days=1),
    )
    repository = MemoryRegistrationRepository([cancelled, active])

    await repository.enlist(
        "event-a1",
        operation_id="reenlist",
        participant_key=cancelled.participant_key,
        display_name=cancelled.display_name,
        wish_play="New wish",
    )

    rows = await repository.list_for_event("event-a1")
    assert [row.display_name for row in rows] == ["Active player", "Returning player"]
    assert rows[-1].attendance_status is AttendanceStatus.WAITING
    assert rows[-1].character_wish == "Doctor"


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
async def test_registration_history_can_be_found_by_telegram_profile() -> None:
    matching = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Legacy player",
        telegram_profile="https://t.me/legacy_player",
        wish_play="Anyone",
    )
    other = matching.model_copy(update={"event_id": "event-b1", "participant_key": "b" * 43, "telegram_profile": None})
    repository = MemoryRegistrationRepository([matching, other])

    assert await repository.find_by_telegram_profile("https://t.me/legacy_player") == [matching]


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


@pytest.mark.asyncio
async def test_active_registration_pages_filter_status_and_support_both_directions() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    registrations = [
        Registration(
            event_id="event-a1",
            participant_key=f"{index:043d}",
            display_name=f"Player {index}",
            wish_play="Anyone",
            attendance_status=(
                AttendanceStatus.CANCELLED
                if index == 5
                else AttendanceStatus.CONFIRMED
                if index % 2
                else AttendanceStatus.WAITING
            ),
            created_at=base + timedelta(seconds=index),
        )
        for index in range(12)
    ]
    repository = MemoryRegistrationRepository(registrations)
    statuses = {AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED}

    first = await repository.list_page_for_event("event-a1", statuses=statuses, limit=10)
    assert [row.display_name for row in first] == [
        "Player 0",
        "Player 1",
        "Player 2",
        "Player 3",
        "Player 4",
        "Player 6",
        "Player 7",
        "Player 8",
        "Player 9",
        "Player 10",
    ]

    last_cursor = (first[-1].created_at, first[-1].participant_key)
    second = await repository.list_page_for_event("event-a1", statuses=statuses, after=last_cursor, limit=10)
    assert [row.display_name for row in second] == ["Player 11"]

    first_cursor = (second[0].created_at, second[0].participant_key)
    previous = await repository.list_page_for_event("event-a1", statuses=statuses, before=first_cursor, limit=10)
    assert previous == first


@pytest.mark.asyncio
async def test_registration_remove_is_scoped_to_one_event() -> None:
    registration = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Player",
        wish_play="Anyone",
    )
    other_event = registration.model_copy(update={"event_id": "event-b1"})
    repository = MemoryRegistrationRepository([registration, other_event])

    assert await repository.remove("event-a1", participant_key=registration.participant_key)
    assert not await repository.remove("event-a1", participant_key=registration.participant_key)
    assert await repository.get("event-a1", registration.participant_key) is None
    assert await repository.get("event-b1", registration.participant_key) == other_event


@pytest.mark.asyncio
async def test_registration_remove_many_is_scoped_to_selected_keys() -> None:
    registrations = [
        Registration(
            event_id="event-a1",
            participant_key=key * 43,
            display_name=key,
            wish_play="A",
        )
        for key in ("a", "b", "c")
    ]
    repository = MemoryRegistrationRepository(registrations)

    await repository.remove_many("event-a1", participant_keys=["a" * 43, "c" * 43])

    assert await repository.get("event-a1", "a" * 43) is None
    assert await repository.get("event-a1", "b" * 43) is not None
    assert await repository.get("event-a1", "c" * 43) is None


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
async def test_ydb_registration_history_lookup_is_parameterized() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    assert await repository.find_by_telegram_profile("https://t.me/legacy_player") == []

    query, params = executor.queries[0]
    assert "WHERE telegram_profile = $profile" in query
    assert params == {"$profile": "https://t.me/legacy_player"}


@pytest.mark.asyncio
async def test_ydb_enlist_uses_composite_key_and_persists_showcase_fields() -> None:
    executor = RecordingExecutor([[], []])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.enlist(
        "event-a1",
        operation_id="operation",
        participant_key="a" * 43,
        display_name="Player",
        nickname="Night Owl",
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
    assert upsert[1]["$nickname"] == "Night Owl"
    assert upsert[1]["$larp_experience"] is True
    assert upsert[1]["$crossplay"] is False
    assert upsert[1]["$vk_profile"] == "https://vk.com/player"
    assert upsert[1]["$telegram_profile"] == "https://t.me/player"


@pytest.mark.asyncio
async def test_ydb_profile_update_changes_only_registration_profile_projection() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.update_profile(
        "event-a1",
        operation_id="profile-update",
        participant_key="a" * 43,
        display_name="Новое Имя",
        nickname="Новый Ник",
        larp_experience=False,
        crossplay=True,
        vk_profile="https://vk.com/new-profile",
        telegram_profile="https://t.me/new_profile",
    )

    query, params = executor.queries[0]
    assert "SET display_name = $display_name" in query
    assert "nickname = $nickname" in query
    assert "wish_play" not in query
    assert "character_wish" not in query
    assert "attendance_status" not in query
    assert "created_at" not in query
    assert "last_operation_id != $operation_id" in query
    assert params["$display_name"] == "Новое Имя"
    assert params["$nickname"] == "Новый Ник"
    assert params["$telegram_profile"] == "https://t.me/new_profile"


@pytest.mark.asyncio
async def test_ydb_reenlist_after_cancellation_resets_queue_position() -> None:
    base = datetime(2000, 1, 1, tzinfo=UTC)
    cancelled = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Returning player",
        wish_play="A",
        character_wish="Doctor",
        attendance_status=AttendanceStatus.CANCELLED,
        created_at=base,
        updated_at=base,
    )
    stored_row = cancelled.model_dump()
    stored_row["attendance_status"] = AttendanceStatus.CANCELLED.value
    executor = RecordingExecutor([[stored_row], []])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.enlist(
        "event-a1",
        operation_id="reenlist",
        participant_key=cancelled.participant_key,
        display_name=cancelled.display_name,
        wish_play="New wish",
    )

    _, upsert = executor.queries
    assert upsert[1]["$attendance_status"] == AttendanceStatus.WAITING.value
    assert upsert[1]["$created_at"] > base
    assert upsert[1]["$character_wish"] == "Doctor"


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


@pytest.mark.asyncio
async def test_ydb_confirmation_deletes_replaced_game_row_in_the_same_transaction() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.confirm(
        "event-a1",
        operation_id="confirm",
        participant_key="a" * 43,
        character_wish="Doctor",
        replaced_event_id="event-b1",
        replaced_participant_key="b" * 43,
    )

    query, params = executor.queries[0]
    assert query.index("DELETE FROM `registrations`") < query.index("UPDATE `registrations`")
    assert "event_id = $replaced_event_id" in query
    assert "participant_key = $replaced_participant_key" in query
    assert params["$replaced_event_id"] == "event-b1"
    assert params["$replaced_participant_key"] == "b" * 43


@pytest.mark.asyncio
async def test_ydb_co_player_wish_update_changes_only_wish_projection() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.update_co_player_wish(
        "event-a1",
        operation_id="co-player-update",
        participant_key="a" * 43,
        wish_play="Петров Пётр",
    )

    query, params = executor.queries[0]
    assert "SET wish_play = $wish_play" in query
    assert "character_wish" not in query
    assert "attendance_status" not in query
    assert "created_at" not in query
    assert "last_operation_id != $operation_id" in query
    assert params["$wish_play"] == "Петров Пётр"


@pytest.mark.asyncio
async def test_ydb_registration_page_uses_status_and_keyset_filters() -> None:
    after = (datetime(2026, 1, 1, tzinfo=UTC), "a" * 43)
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    rows = await repository.list_page_for_event(
        "event-a1",
        statuses={AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED},
        after=after,
        limit=10,
    )

    query, params = executor.queries[0]
    assert rows == []
    assert "attendance_status IN ($status_0, $status_1)" in query
    assert "created_at > $after_time" in query
    assert "participant_key > $after_key" in query
    assert "ORDER BY created_at ASC, participant_key ASC" in query
    assert set(params.values()) >= {AttendanceStatus.WAITING.value, AttendanceStatus.CONFIRMED.value}
    assert params["$after_time"] == after[0]
    assert params["$after_key"] == after[1]


@pytest.mark.asyncio
async def test_ydb_registration_remove_uses_the_composite_key() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.remove("event-a1", participant_key="a" * 43)

    query, params = executor.queries[0]
    assert "DELETE FROM `registrations`" in query
    assert "event_id = $event_id AND participant_key = $participant_key" in query
    assert params == {"$event_id": "event-a1", "$participant_key": "a" * 43}


@pytest.mark.asyncio
async def test_ydb_registration_remove_many_uses_event_and_selected_keys() -> None:
    executor = RecordingExecutor([[]])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]
    keys = ["a" * 43, "b" * 43]

    await repository.remove_many("event-a1", participant_keys=keys)

    query, params = executor.queries[0]
    assert "participant_key IN $participant_keys" in query
    assert "DELETE FROM `registrations`" in query
    assert params == {"$event_id": "event-a1", "$participant_keys": keys}


@pytest.mark.asyncio
async def test_ydb_registration_sift_moves_all_waiting_rows_with_one_update() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    registrations = [
        Registration(
            event_id="event-a1",
            participant_key=key,
            display_name=name,
            wish_play="Anyone",
            created_at=base + timedelta(seconds=index),
        )
        for index, (key, name) in enumerate([("a" * 43, "First"), ("b" * 43, "Second")])
    ]
    stored_rows = []
    for registration in registrations:
        row = registration.model_dump()
        row["attendance_status"] = registration.attendance_status.value
        stored_rows.append(row)
    executor = RecordingExecutor([stored_rows, []])
    repository = YdbRegistrationRepository(executor)  # type: ignore[arg-type]

    await repository.sift_waiting_to_end("event-a1")

    query, params = executor.queries[1]
    assert query.count("UPDATE `registrations`") == 1
    assert "SET created_at = Unwrap(created_at + $shift)" in query
    assert "event_id = $event_id AND attendance_status = $waiting_status" in query
    assert params == {
        "$event_id": "event-a1",
        "$waiting_status": AttendanceStatus.WAITING.value,
        "$shift": 1_000_001,
    }
