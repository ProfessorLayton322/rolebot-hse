from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from larp_bot.domain.models import Event, Platform, Registration, TelegramUser, VkUser
from larp_bot.migration.canonical import (
    CanonicalValidationError,
    build_document,
    canonical_event,
    canonical_leader,
    canonical_registration,
    canonical_user,
)
from larp_bot.migration.ydb import YdbMigrationStore


class FakeYdbExecutor:
    """Logical YDB table double driven by the migration query markers."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "tg_users": [],
            "vk_users": [],
            "events": [],
            "event_leaders": [],
            "registrations": [],
        }
        self.mutations = 0

    async def snapshot(self, queries: Any) -> list[list[dict[str, Any]]]:
        assert len(queries) == 5
        return [deepcopy(self.tables[table]) for table in self.tables]

    @staticmethod
    def _key(table: str, row: dict[str, Any]) -> tuple[Any, ...]:
        if table == "tg_users":
            return (row["tg_id"],)
        if table == "vk_users":
            return (row["vk_id"],)
        if table == "events":
            return (row["event_id"],)
        if table == "event_leaders":
            return row["event_id"], row["platform"], row["platform_user_id"]
        return row["event_id"], row["participant_key"]

    async def query(
        self,
        yql: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        assert not read_only
        assert params is not None
        marker = yql.splitlines()[0].removeprefix("-- migration:")
        table, action = marker.split(":")
        if action == "upsert":
            row = self._row(table, params)
            key = self._key(table, row)
            rows = self.tables[table]
            index = next((position for position, item in enumerate(rows) if self._key(table, item) == key), None)
            if index is None:
                rows.append(row)
            else:
                rows[index] = row
        else:
            key_params = {name.removeprefix("$"): value for name, value in params.items()}
            key = self._key(table, key_params)
            self.tables[table] = [item for item in self.tables[table] if self._key(table, item) != key]
        self.mutations += 1
        return []

    @staticmethod
    def _row(table: str, params: dict[str, Any]) -> dict[str, Any]:
        values = {name.removeprefix("$"): value for name, value in params.items()}
        if table in {"tg_users", "vk_users"}:
            identifier = "tg_id" if table == "tg_users" else "vk_id"
            row = {
                identifier: values["user_id"],
                "telegram_handle": values["telegram_handle"],
                "full_name": values["full_name"],
                "nickname": values["nickname"],
                "crossplay": values["crossplay"],
                "larp_experience": values["larp_experience"],
                "needs_pass": values["needs_pass"],
                "pass_details_json": values["pass_details"],
                "dialog_state": values["dialog_state"],
                "dialog_context_json": values["dialog_context"],
                "last_update_id": values["last_update_id"],
                "last_update_at": values["last_update_at"],
                "last_delivery_operation_id": values["last_delivery_operation_id"],
                "last_bot_buttons_json": values["last_bot_buttons"],
                "is_gamemaster": values["is_gamemaster"],
                "privileged_role": values["privileged_role"],
                "gamemaster_grant_operation_id": values["gamemaster_grant_operation_id"],
                "created_at": values["created_at"],
                "updated_at": values["updated_at"],
            }
            if table == "tg_users":
                row.update(vk_url=values["vk_url"], linked_vk_id=values["linked_vk_id"])
            return row
        if table == "events":
            values["public_registration_url"] = values.pop("public_url")
            return values
        if table == "event_leaders":
            return values
        values["participation_notification_deliveries_json"] = values.pop("participation_deliveries")
        return values


def _document():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    event = Event(
        event_id="event-a1",
        name="Game",
        disk_resource_path="disk:/larp-bot/events/event-a1.xlsx",
        public_registration_url="https://example.test/event",
        master_table_google_url="https://drive.example/master",
        public_table_resource_path="disk:/larp-bot/events/event-a1-public.xlsx",
        public_table_public_url="https://disk.example/public",
        public_table_google_url="https://drive.example/public",
        pass_table_resource_path="disk:/larp-bot/passes/event-a1.xlsx",
        pass_table_public_url="https://disk.example/pass",
        pass_table_google_url="https://drive.example/pass",
        signup_token="game_1",
        created_at=now,
        updated_at=now,
    )
    registration = Registration(
        event_id=event.event_id,
        participant_key="p" * 43,
        display_name="Player",
        wish_play="Role",
        created_at=now,
        updated_at=now,
    )
    return build_document(
        {
            "tg_users": [canonical_user(TelegramUser(tg_id=1, created_at=now, updated_at=now))],
            "vk_users": [canonical_user(VkUser(vk_id=2, created_at=now, updated_at=now))],
            "events": [canonical_event(event)],
            "event_leaders": [canonical_leader(event.event_id, Platform.TELEGRAM, 1, now)],
            "registrations": [canonical_registration(registration, ["delivery-1"])],
        },
        source="dynamodb",
    )


@pytest.mark.asyncio
async def test_reverse_ydb_import_is_resumable_and_round_trip_verified() -> None:
    executor = FakeYdbExecutor()
    store = YdbMigrationStore(executor)
    document = _document()
    await store._upsert("tg_users", document["tables"]["tg_users"][0])

    first_export = await store.import_document(document)
    mutations = executor.mutations
    second_export = await store.import_document(document)

    assert first_export["hashes"] == document["hashes"]
    assert second_export["hashes"] == document["hashes"]
    assert executor.mutations == mutations


@pytest.mark.asyncio
async def test_reverse_ydb_import_requires_explicit_delete_for_extra_rows() -> None:
    executor = FakeYdbExecutor()
    store = YdbMigrationStore(executor)
    document = _document()
    await store.import_document(document)
    executor.tables["vk_users"].append(
        FakeYdbExecutor._row(
            "vk_users",
            {
                "$user_id": 999,
                "$vk_url": None,
                "$linked_vk_id": None,
                "$telegram_handle": None,
                "$full_name": None,
                "$nickname": None,
                "$crossplay": None,
                "$larp_experience": None,
                "$needs_pass": None,
                "$pass_details": None,
                "$dialog_state": "IDLE",
                "$dialog_context": "{}",
                "$last_update_id": None,
                "$last_update_at": None,
                "$last_delivery_operation_id": None,
                "$last_bot_buttons": "[]",
                "$is_gamemaster": False,
                "$privileged_role": None,
                "$gamemaster_grant_operation_id": None,
                "$created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "$updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
        )
    )
    mutations = executor.mutations

    with pytest.raises(CanonicalValidationError, match="absent from the dump"):
        await store.import_document(document)
    assert executor.mutations == mutations

    verified = await store.import_document(document, delete_extra=True)
    assert verified["hashes"] == document["hashes"]
