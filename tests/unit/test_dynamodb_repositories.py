from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from larp_bot.adapters.aws.dynamodb import (
    DynamoEventRepository,
    DynamoRegistrationRepository,
    DynamoUserRepository,
)
from larp_bot.domain.models import AttendanceStatus, BotIdentity, Event, Platform, Registration, TelegramUser

_DESERIALIZER = TypeDeserializer()


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}


class MemoryDynamoClient:
    """Faithful enough transaction double to exercise revision and lock conditions."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.scans: list[str] = []
        self.transaction_attempts = 0
        self.fail_next_transaction = False

    @staticmethod
    def _key(table: str, item: dict[str, Any]) -> tuple[str, ...]:
        if table == "tg-users":
            return (str(item["tg_id"]),)
        if table == "vk-users":
            return (str(item["vk_id"]),)
        if table == "events":
            return (str(item["event_id"]),)
        if table == "leaders":
            return str(item["event_id"]), str(item["leader_key"])
        if table == "registrations":
            return str(item["event_id"]), str(item["participant_key"])
        raise AssertionError(table)

    def scan(self, *, TableName: str, ConsistentRead: bool, **request: Any) -> dict[str, Any]:
        assert ConsistentRead is True
        assert "ExclusiveStartKey" not in request
        self.scans.append(TableName)
        return {"Items": deepcopy(self.tables.get(TableName, []))}

    def get_item(
        self,
        *,
        TableName: str,
        Key: dict[str, Any],
        ConsistentRead: bool,
    ) -> dict[str, Any]:
        assert ConsistentRead is True
        index = self._find(self.tables, TableName, self._key(TableName, _plain(Key)))
        return {} if index is None else {"Item": deepcopy(self.tables[TableName][index])}

    def query(self, **request: Any) -> dict[str, Any]:
        table = request["TableName"]
        names = request["ExpressionAttributeNames"]
        values = _plain(request["ExpressionAttributeValues"])
        condition = request["KeyConditionExpression"]
        if "IndexName" not in request:
            assert request["ConsistentRead"] is True
        matches: list[dict[str, Any]] = []
        for raw in self.tables.get(table, []):
            row = _plain(raw)
            accepted = True
            for comparison in condition.split(" AND "):
                name_token, value_token = comparison.split(" = ")
                accepted = accepted and row.get(names[name_token]) == values[value_token]
            if accepted:
                matches.append(deepcopy(raw))
        return {"Items": matches}

    def _find(self, tables: dict[str, list[dict[str, Any]]], table: str, key: tuple[str, ...]) -> int | None:
        for index, raw in enumerate(tables.get(table, [])):
            if self._key(table, _plain(raw)) == key:
                return index
        return None

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transaction_attempts += 1
        if self.fail_next_transaction:
            self.fail_next_transaction = False
            raise ClientError(
                {"Error": {"Code": "TransactionConflictException", "Message": "retry"}},
                "TransactWriteItems",
            )
        candidate = deepcopy(self.tables)
        for action in TransactItems:
            operation = action.get("Put") or action.get("Delete")
            assert isinstance(operation, dict)
            table = operation["TableName"]
            raw_item = operation.get("Item")
            raw_key = operation.get("Key")
            item = _plain(raw_item or raw_key)
            key = self._key(table, item)
            existing_index = self._find(candidate, table, key)
            existing = None if existing_index is None else _plain(candidate[table][existing_index])
            condition = operation.get("ConditionExpression")
            values = _plain(operation.get("ExpressionAttributeValues", {}))
            valid = True
            if condition == "attribute_not_exists(#partition_key)" or condition == "attribute_not_exists(event_id)":
                valid = existing is None
            elif condition == "#revision = :revision":
                valid = existing is not None and existing.get("_revision") == values[":revision"]
            elif condition == "owner_event_id = :owner":
                valid = existing is not None and existing.get("owner_event_id") == values[":owner"]
            if not valid:
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException", "Message": "condition failed"}},
                    "TransactWriteItems",
                )
            rows = candidate.setdefault(table, [])
            if "Put" in action:
                if existing_index is None:
                    rows.append(deepcopy(raw_item))
                else:
                    rows[existing_index] = deepcopy(raw_item)
            elif existing_index is not None:
                rows.pop(existing_index)
        self.tables = candidate
        self.transactions.append(deepcopy(TransactItems))
        return {}


@pytest.mark.asyncio
async def test_dynamodb_user_ids_are_strings_and_delivery_claim_is_conditional() -> None:
    client = MemoryDynamoClient()
    repository = DynamoUserRepository(client, telegram_table="tg-users", vk_table="vk-users")
    user = TelegramUser(tg_id=123, telegram_handle="@player", linked_vk_id=456)

    await repository.save(user)
    first = await repository.claim_delivery(Platform.TELEGRAM, 123, "operation-1")
    repeated = await repository.claim_delivery(Platform.TELEGRAM, 123, "operation-1")

    stored = _plain(client.tables["tg-users"][0])
    assert stored["tg_id"] == "123"
    assert stored["linked_vk_id"] == "456"
    assert stored["_revision"] == 2
    assert first is True
    assert repeated is False
    assert client.scans == []


@pytest.mark.asyncio
async def test_transaction_conflict_reloads_and_retries_the_delivery_claim() -> None:
    client = MemoryDynamoClient()
    repository = DynamoUserRepository(client, telegram_table="tg-users", vk_table="vk-users")
    await repository.save(TelegramUser(tg_id=123))
    attempts_before = client.transaction_attempts
    client.fail_next_transaction = True

    assert await repository.claim_delivery(Platform.TELEGRAM, 123, "operation-1")

    assert client.transaction_attempts == attempts_before + 2
    stored = await repository.get(Platform.TELEGRAM, 123)
    assert stored is not None and stored.last_delivery_operation_id == "operation-1"


@pytest.mark.asyncio
async def test_telegram_handle_lookup_indexes_users_without_a_vk_link() -> None:
    client = MemoryDynamoClient()
    repository = DynamoUserRepository(client, telegram_table="tg-users", vk_table="vk-users")
    user = TelegramUser(tg_id=123, telegram_handle="@player")
    await repository.save(user)

    assert await repository.find_telegram_by_handle("@player") == user
    assert client.scans == []


@pytest.mark.asyncio
async def test_event_and_leaders_are_created_with_unique_signup_and_chat_locks(event: Event) -> None:
    client = MemoryDynamoClient()
    repository = DynamoEventRepository(client, events_table="events", leaders_table="leaders")
    event.signup_token = "Event_A1_signup"
    event.telegram_chat_id = -100123

    await repository.create(
        event,
        leaders=[BotIdentity(platform=Platform.TELEGRAM, platform_user_id=42)],
    )

    rows = [_plain(item) for item in client.tables["events"]]
    assert {row["record_type"] for row in rows} == {"event", "unique_lock"}
    assert sum(row["record_type"] == "unique_lock" for row in rows) == 2
    assert await repository.get_by_signup_token("Event_A1_signup") == event
    assert await repository.list_leaders(event.event_id) == [
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=42)
    ]
    assert client.scans == []


@pytest.mark.asyncio
async def test_telegram_chat_binding_is_one_to_one(event: Event) -> None:
    client = MemoryDynamoClient()
    repository = DynamoEventRepository(client, events_table="events", leaders_table="leaders")
    other = event.model_copy(update={"event_id": "event-b1", "name": "Other"})
    await repository.create(event)
    await repository.create(other)

    assert await repository.bind_telegram_chat(event.event_id, -100123)
    assert not await repository.bind_telegram_chat(other.event_id, -100123)
    assert (await repository.get(other.event_id)).telegram_chat_id is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_linked_confirmation_is_one_dynamodb_transaction() -> None:
    client = MemoryDynamoClient()
    repository = DynamoRegistrationRepository(client, registrations_table="registrations")
    current = [
        Registration(event_id="event-a1", participant_key=key * 43, display_name=key, wish_play="A")
        for key in ("a", "b")
    ]
    replaced = [
        Registration(event_id="event-b1", participant_key=key * 43, display_name=key, wish_play="B")
        for key in ("a", "b")
    ]
    await repository.import_missing([*current, *replaced])
    before = len(client.transactions)

    assert await repository.confirm_linked(
        "event-a1",
        operation_id="confirm-linked",
        participant_keys=["a" * 43, "b" * 43],
        character_wish="Wizard",
        replaced_event_id="event-b1",
        replaced_participant_keys=["a" * 43, "b" * 43],
    )

    assert len(client.transactions) == before + 1
    assert len(client.transactions[-1]) == 4
    assert {row.attendance_status for row in await repository.list_for_event("event-a1")} == {
        AttendanceStatus.CONFIRMED
    }
    assert {row.attendance_status for row in await repository.list_for_event("event-b1")} == {
        AttendanceStatus.CANCELLED
    }
    assert client.scans == []


@pytest.mark.asyncio
async def test_queue_position_swap_updates_both_rows_atomically() -> None:
    client = MemoryDynamoClient()
    repository = DynamoRegistrationRepository(client, registrations_table="registrations")
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    second_time = datetime(2026, 1, 2, tzinfo=UTC)
    await repository.import_missing(
        [
            Registration(
                event_id="event-a1",
                participant_key="a" * 43,
                display_name="A",
                wish_play="A",
                created_at=first_time,
            ),
            Registration(
                event_id="event-a1",
                participant_key="b" * 43,
                display_name="B",
                wish_play="B",
                created_at=second_time,
            ),
        ]
    )

    assert await repository.swap_queue_positions(
        "event-a1",
        operation_id="swap-1",
        promoted_participant_key="b" * 43,
        promoted_created_at=second_time,
        demoted_participant_key="a" * 43,
        demoted_created_at=first_time,
    )

    assert len(client.transactions[-1]) == 2
    rows = await repository.list_for_event("event-a1")
    assert [row.display_name for row in rows] == ["B", "A"]
