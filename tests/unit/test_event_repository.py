from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from larp_bot.adapters.ydb.repositories import YdbEventRepository
from larp_bot.domain.models import DEFAULT_PLAYER_AMOUNT, BotIdentity, EventStatus, Platform


def event_row(status: str) -> dict[str, Any]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "event_id": "event-a1",
        "name": "Game",
        "player_amount": 12,
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
        self.insert_params: dict[str, Any] = {}

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

    async def insert_if_absent(
        self,
        *,
        select_yql: str,
        select_params: dict[str, Any],
        insert_yql: str,
        insert_params: dict[str, Any],
    ) -> bool:
        self.query_text = f"{select_yql}\n{insert_yql}"
        self.params = select_params
        self.insert_params = insert_params
        return True


def test_legacy_open_status_is_read_as_confirmation_open() -> None:
    event = YdbEventRepository._from_row(event_row("OPEN"))
    assert event.status is EventStatus.CONFIRMATION_OPEN
    assert event.player_amount == 12


def test_event_without_persisted_player_amount_uses_rollout_default() -> None:
    row = event_row("CREATED")
    del row["player_amount"]

    event = YdbEventRepository._from_row(row)

    assert event.player_amount == DEFAULT_PLAYER_AMOUNT


def test_confirmed_notification_history_is_loaded_from_event_json() -> None:
    row = event_row("CONFIRMATION_OPEN") | {
        "confirmed_notifications_json": json.dumps(
            ["Сбор в 18:30", "https://t.me/+GameChat_123"],
            ensure_ascii=False,
        ),
        "last_confirmed_notification_operation_id": "notification-operation",
    }

    event = YdbEventRepository._from_row(row)

    assert event.confirmed_notifications == ["Сбор в 18:30", "https://t.me/+GameChat_123"]
    assert event.last_confirmed_notification_operation_id == "notification-operation"


def test_participant_removal_delivery_state_is_loaded_from_event_json() -> None:
    row = event_row("CREATED") | {
        "last_participant_removal_operation_id": "removal-operation",
        "participant_removal_keys_json": json.dumps(["a" * 43, "b" * 43]),
        "participant_removal_notified_keys_json": json.dumps(["a" * 43]),
    }

    event = YdbEventRepository._from_row(row)

    assert event.last_participant_removal_operation_id == "removal-operation"
    assert event.participant_removal_keys == ["a" * 43, "b" * 43]
    assert event.participant_removal_notified_keys == ["a" * 43]


def test_confirmed_link_promotion_state_is_loaded() -> None:
    row = event_row("CONFIRMATION_OPEN") | {
        "last_confirmed_link_promotion_operation_id": "cancel-operation",
        "last_confirmed_link_promotion_participant_key": "p" * 43,
        "last_confirmed_link_promotion_delivered_operation_id": "cancel-operation",
    }

    event = YdbEventRepository._from_row(row)

    assert event.last_confirmed_link_promotion_operation_id == "cancel-operation"
    assert event.last_confirmed_link_promotion_participant_key == "p" * 43
    assert event.last_confirmed_link_promotion_delivered_operation_id == "cancel-operation"


def test_archival_timestamp_is_loaded() -> None:
    archived_at = datetime(2026, 2, 1, tzinfo=UTC)
    event = YdbEventRepository._from_row(event_row("CLOSED") | {"archived_at": archived_at})

    assert event.archived_at == archived_at


@pytest.mark.asyncio
async def test_event_creation_atomically_seeds_platform_specific_leaders() -> None:
    executor = RecordingExecutor([])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]
    event = YdbEventRepository._from_row(event_row("CREATED"))

    await repository.create(
        event,
        [
            BotIdentity(platform=Platform.TELEGRAM, platform_user_id=10),
            BotIdentity(platform=Platform.VK, platform_user_id=20),
        ],
    )

    assert executor.query_text.count("UPSERT INTO `event_leaders`") == 2
    assert executor.params["$leader_platform_0"] == Platform.TELEGRAM.value
    assert executor.params["$leader_user_id_0"] == 10
    assert executor.params["$leader_platform_1"] == Platform.VK.value
    assert executor.params["$leader_user_id_1"] == 20
    assert executor.params["$player_amount"] == 12


@pytest.mark.asyncio
async def test_player_amount_is_updated_on_existing_event() -> None:
    executor = RecordingExecutor([event_row("CREATED")])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    assert await repository.set_player_amount("event-a1", 24)

    assert "player_amount = $player_amount" in executor.query_text
    assert executor.params["$event_id"] == "event-a1"
    assert executor.params["$player_amount"] == 24


@pytest.mark.asyncio
async def test_reserve_promotion_operation_and_participant_are_persisted_together() -> None:
    executor = RecordingExecutor([])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    await repository.set_reserve_promotion("event-a1", "cancel-operation", "p" * 43)

    assert "last_reserve_promotion_operation_id = $operation_id" in executor.query_text
    assert "last_reserve_promotion_participant_key = $participant_key" in executor.query_text
    assert executor.params["$operation_id"] == "cancel-operation"
    assert executor.params["$participant_key"] == "p" * 43

    await repository.mark_reserve_promotion_delivered("event-a1", "cancel-operation")

    assert "last_reserve_promotion_delivered_operation_id = $operation_id" in executor.query_text
    assert executor.params["$operation_id"] == "cancel-operation"


@pytest.mark.asyncio
async def test_confirmed_link_promotion_operation_and_participant_are_persisted_together() -> None:
    executor = RecordingExecutor([])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    await repository.set_confirmed_link_promotion("event-a1", "cancel-operation", "p" * 43)

    assert "last_confirmed_link_promotion_operation_id = $operation_id" in executor.query_text
    assert "last_confirmed_link_promotion_participant_key = $participant_key" in executor.query_text
    assert executor.params["$operation_id"] == "cancel-operation"
    assert executor.params["$participant_key"] == "p" * 43

    await repository.mark_confirmed_link_promotion_delivered("event-a1", "cancel-operation")

    assert "last_confirmed_link_promotion_delivered_operation_id = $operation_id" in executor.query_text
    assert executor.params["$operation_id"] == "cancel-operation"


@pytest.mark.asyncio
async def test_participant_removal_snapshot_and_delivery_are_persisted() -> None:
    snapshot_executor = RecordingExecutor([event_row("CREATED")])
    repository = YdbEventRepository(snapshot_executor)  # type: ignore[arg-type]
    keys = ["a" * 43, "b" * 43]

    await repository.set_participant_removal("event-a1", "removal-operation", keys)

    assert "participant_removal_keys_json = $participant_keys_json" in snapshot_executor.query_text
    assert json.loads(snapshot_executor.params["$participant_keys_json"]) == keys

    delivery_executor = RecordingExecutor(
        [
            event_row("CREATED")
            | {
                "last_participant_removal_operation_id": "removal-operation",
                "participant_removal_keys_json": json.dumps(keys),
                "participant_removal_notified_keys_json": json.dumps([keys[0]]),
            }
        ]
    )
    repository = YdbEventRepository(delivery_executor)  # type: ignore[arg-type]
    await repository.mark_participant_removal_delivered("event-a1", "removal-operation", keys[1])

    assert "participant_removal_notified_keys_json = $notified_keys_json" in delivery_executor.query_text
    assert json.loads(delivery_executor.params["$notified_keys_json"]) == keys


@pytest.mark.asyncio
async def test_event_leaders_are_added_idempotently_and_listed_by_platform_identity() -> None:
    identity = BotIdentity(platform=Platform.VK, platform_user_id=22)
    add_executor = RecordingExecutor([event_row("CREATED")])
    repository = YdbEventRepository(add_executor)  # type: ignore[arg-type]

    assert await repository.add_leader("event-a1", identity)
    assert "INSERT INTO `event_leaders`" in add_executor.query_text
    assert add_executor.insert_params["$platform"] == Platform.VK.value
    assert add_executor.insert_params["$platform_user_id"] == 22

    list_executor = RecordingExecutor([{"platform": "vk", "platform_user_id": 22}])
    listed = await YdbEventRepository(list_executor).list_leaders("event-a1")  # type: ignore[arg-type]
    assert listed == [identity]


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
async def test_active_game_page_filters_archived_rows_and_supports_previous_cursor() -> None:
    older = event_row("CREATED") | {"event_id": "event-old", "created_at": datetime(2025, 1, 1, tzinfo=UTC)}
    executor = RecordingExecutor([older])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]
    cursor = (datetime(2026, 1, 1, tzinfo=UTC), "event-a1")

    events = await repository.list_page(before=cursor, archived=False, limit=10)

    assert [event.event_id for event in events] == ["event-old"]
    assert "archived_at IS NULL" in executor.query_text
    assert "created_at DESC, event_id DESC" in executor.query_text
    assert executor.params["$before_time"] == cursor[0]
    assert executor.params["$before_id"] == cursor[1]


@pytest.mark.asyncio
async def test_newest_first_page_uses_reverse_keyset_order() -> None:
    older = event_row("CLOSED") | {"event_id": "event-old", "created_at": datetime(2025, 1, 1, tzinfo=UTC)}
    executor = RecordingExecutor([older])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]
    cursor = (datetime(2026, 1, 1, tzinfo=UTC), "event-a1")

    events = await repository.list_page(after=cursor, newest_first=True, limit=10)

    assert [event.event_id for event in events] == ["event-old"]
    assert "created_at < $after_time" in executor.query_text
    assert "event_id < $after_id" in executor.query_text
    assert "created_at DESC, event_id DESC" in executor.query_text


@pytest.mark.asyncio
async def test_archive_sets_timestamp_and_closed_status() -> None:
    executor = RecordingExecutor([event_row("CONFIRMATION_OPEN")])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]
    archived_at = datetime(2026, 9, 5, 15, tzinfo=UTC)

    assert await repository.archive("event-a1", archived_at)

    assert "archived_at = $archived_at" in executor.query_text
    assert executor.params["$status"] == EventStatus.CLOSED.value
    assert executor.params["$archived_at"] == archived_at


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
async def test_confirmed_notification_is_appended_as_plain_text_once() -> None:
    row = event_row("CONFIRMATION_OPEN") | {
        "confirmed_notifications_json": json.dumps(["Сбор в 18:30"], ensure_ascii=False),
    }
    executor = RecordingExecutor([row])
    repository = YdbEventRepository(executor)  # type: ignore[arg-type]

    assert await repository.append_confirmed_notification(
        "event-a1",
        "https://t.me/+GameChat_123",
        "notification-operation",
    )

    assert json.loads(executor.params["$notifications_json"]) == [
        "Сбор в 18:30",
        "https://t.me/+GameChat_123",
    ]
    assert executor.params["$operation_id"] == "notification-operation"
    assert "last_confirmed_notification_operation_id != $operation_id" in executor.query_text


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
