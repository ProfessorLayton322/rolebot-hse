from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from larp_bot.domain.models import Event, Platform, Registration, TelegramUser, VkUser
from larp_bot.migration.canonical import (
    CanonicalValidationError,
    build_document,
    canonical_event,
    canonical_leader,
    canonical_registration,
    canonical_user,
    dynamodb_physical_items,
)
from larp_bot.migration.dynamodb import DynamoMigrationStore, DynamoTableNames

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}


def _serialized(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


class FakeDynamoClient:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.puts = 0
        self.deletes = 0

    @staticmethod
    def _key(table: str, item: dict[str, Any]) -> tuple[str, ...]:
        if table.endswith("-tg-users"):
            return (str(item["tg_id"]),)
        if table.endswith("-vk-users"):
            return (str(item["vk_id"]),)
        if table.endswith("-events"):
            return (str(item["event_id"]),)
        if table.endswith("-event-leaders"):
            return str(item["event_id"]), str(item["leader_key"])
        if table.endswith("-registrations"):
            return str(item["event_id"]), str(item["participant_key"])
        raise AssertionError(table)

    def scan(self, *, TableName: str, ConsistentRead: bool, **request: Any) -> dict[str, Any]:
        assert ConsistentRead
        assert "ExclusiveStartKey" not in request
        return {"Items": deepcopy(self.tables.get(TableName, []))}

    def put_item(self, *, TableName: str, Item: dict[str, Any], **request: Any) -> dict[str, Any]:
        item = _plain(Item)
        key = self._key(TableName, item)
        rows = self.tables.setdefault(TableName, [])
        index = next((position for position, row in enumerate(rows) if self._key(TableName, _plain(row)) == key), None)
        condition = request["ConditionExpression"]
        if condition == "attribute_not_exists(#partition_key)":
            assert index is None
        else:
            assert index is not None
            values = _plain(request["ExpressionAttributeValues"])
            assert _plain(rows[index])["_revision"] == values[":revision"]
        if index is None:
            rows.append(deepcopy(Item))
        else:
            rows[index] = deepcopy(Item)
        self.puts += 1
        return {}

    def delete_item(self, *, TableName: str, Key: dict[str, Any], **request: Any) -> dict[str, Any]:
        key = self._key(TableName, _plain(Key))
        rows = self.tables[TableName]
        index = next(position for position, row in enumerate(rows) if self._key(TableName, _plain(row)) == key)
        values = _plain(request["ExpressionAttributeValues"])
        assert _plain(rows[index])["_revision"] == values[":revision"]
        rows.pop(index)
        self.deletes += 1
        return {}


def _document():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    event = Event(
        event_id="event-a1",
        name="Game",
        disk_resource_path="disk:/larp-bot/events/event-a1.xlsx",
        public_registration_url="https://example.test/event",
        signup_token="game_1",
        telegram_chat_id=-100123,
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
        source="ydb",
    )


@pytest.mark.asyncio
async def test_dynamodb_import_is_resumable_and_reexports_identical_hashes() -> None:
    client = FakeDynamoClient()
    store = DynamoMigrationStore(client, DynamoTableNames.from_prefix("test"))
    document = _document()
    partial_user = dynamodb_physical_items(document)["tg_users"][0] | {"_revision": 1}
    client.tables["test-tg-users"] = [_serialized(partial_user)]

    first_export = await store.import_document(document)
    writes = client.puts
    second_export = await store.import_document(document)

    assert first_export["hashes"] == document["hashes"]
    assert second_export["hashes"] == document["hashes"]
    assert client.puts == writes
    assert _plain(client.tables["test-tg-users"][0]) == partial_user
    assert len(client.tables["test-events"]) == 3  # event plus signup/chat uniqueness locks


@pytest.mark.asyncio
async def test_dynamodb_import_refuses_extras_before_writing_and_can_delete_them_explicitly() -> None:
    client = FakeDynamoClient()
    store = DynamoMigrationStore(client, DynamoTableNames.from_prefix("test"))
    document = _document()
    client.tables["test-tg-users"] = [
        _serialized(
            {
                "tg_id": "999",
                "record_type": "user",
                "payload": TelegramUser(tg_id=999).model_dump_json(),
                "_revision": 1,
            }
        )
    ]

    with pytest.raises(CanonicalValidationError, match="absent from the dump"):
        await store.import_document(document)
    assert client.puts == 0

    verified = await store.import_document(document, delete_extra=True)
    assert verified["hashes"] == document["hashes"]
    assert client.deletes == 1


@pytest.mark.asyncio
async def test_dynamodb_export_rejects_missing_uniqueness_lock() -> None:
    client = FakeDynamoClient()
    store = DynamoMigrationStore(client, DynamoTableNames.from_prefix("test"))
    document = _document()
    await store.import_document(document)
    client.tables["test-events"].pop()

    with pytest.raises(CanonicalValidationError, match="uniqueness locks"):
        await store.export_document()


@pytest.mark.asyncio
async def test_dynamodb_export_canonicalizes_equivalent_timestamp_offsets_in_payload() -> None:
    client = FakeDynamoClient()
    store = DynamoMigrationStore(client, DynamoTableNames.from_prefix("test"))
    document = _document()
    await store.import_document(document)
    event_item = next(item for item in client.tables["test-events"] if _plain(item).get("record_type") == "event")
    plain = _plain(event_item)
    payload = json.loads(plain["payload"])
    payload["registration_open_at"] = "2026-01-01T03:00:00+03:00"
    plain["payload"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    event_item.clear()
    event_item.update(_serialized(plain))

    exported = await store.export_document()

    assert exported["tables"]["events"][0]["registration_open_at"] == "2026-01-01T00:00:00.000000Z"
