from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from larp_bot.domain.models import Event, Platform, Registration, TelegramUser, VkUser
from larp_bot.migration.canonical import (
    TABLE_NAMES,
    CanonicalDocument,
    CanonicalTables,
    CanonicalValidationError,
    build_document,
    canonical_event,
    canonical_leader,
    canonical_registration,
    canonical_user,
    compare_documents,
    dynamodb_physical_items,
    validate_document,
)

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


@dataclass(frozen=True)
class DynamoTableNames:
    tg_users: str
    vk_users: str
    events: str
    event_leaders: str
    registrations: str

    @classmethod
    def from_prefix(cls, prefix: str) -> DynamoTableNames:
        if not prefix or prefix.strip() != prefix:
            raise ValueError("DynamoDB table prefix must be non-empty and trimmed")
        return cls(
            tg_users=f"{prefix}-tg-users",
            vk_users=f"{prefix}-vk-users",
            events=f"{prefix}-events",
            event_leaders=f"{prefix}-event-leaders",
            registrations=f"{prefix}-registrations",
        )

    def as_dict(self) -> dict[str, str]:
        return {table: str(getattr(self, table)) for table in TABLE_NAMES}


def _serialize_map(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(item) for key, item in value.items()}


def _deserialize_map(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _DESERIALIZER.deserialize(item) for key, item in value.items()}


def _without_revision(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "_revision"}


def _physical_key(table: str, item: Mapping[str, Any]) -> tuple[str, ...]:
    if table == "tg_users":
        return (str(item["tg_id"]),)
    if table == "vk_users":
        return (str(item["vk_id"]),)
    if table == "events":
        return (str(item["event_id"]),)
    if table == "event_leaders":
        return str(item["event_id"]), str(item["leader_key"])
    if table == "registrations":
        return str(item["event_id"]), str(item["participant_key"])
    raise CanonicalValidationError(f"unknown DynamoDB table: {table}")


def _key_map(table: str, item: Mapping[str, Any]) -> dict[str, str]:
    if table == "tg_users":
        return {"tg_id": str(item["tg_id"])}
    if table == "vk_users":
        return {"vk_id": str(item["vk_id"])}
    if table == "events":
        return {"event_id": str(item["event_id"])}
    if table == "event_leaders":
        return {"event_id": str(item["event_id"]), "leader_key": str(item["leader_key"])}
    if table == "registrations":
        return {
            "event_id": str(item["event_id"]),
            "participant_key": str(item["participant_key"]),
        }
    raise CanonicalValidationError(f"unknown DynamoDB table: {table}")


class DynamoMigrationStore:
    def __init__(self, client: Any, table_names: DynamoTableNames) -> None:
        self.client = client
        self.table_names = table_names

    async def _scan(self, logical_table: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "TableName": self.table_names.as_dict()[logical_table],
                "ConsistentRead": True,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = await asyncio.to_thread(self.client.scan, **request)
            items.extend(_deserialize_map(item) for item in response.get("Items", []))
            raw_start_key = response.get("LastEvaluatedKey")
            if not raw_start_key:
                return items
            start_key = raw_start_key

    async def _scan_all(self) -> CanonicalTables:
        scans = await asyncio.gather(*(self._scan(table) for table in TABLE_NAMES))
        return {table: rows for table, rows in zip(TABLE_NAMES, scans, strict=True)}

    @staticmethod
    def _revision(table: str, item: Mapping[str, Any]) -> int:
        revision = item.get("_revision")
        if isinstance(revision, bool) or not isinstance(revision, (int, Decimal)):
            raise CanonicalValidationError(f"{table} DynamoDB item has an invalid _revision")
        parsed = int(revision)
        if Decimal(parsed) != revision or parsed < 1:
            raise CanonicalValidationError(f"{table} DynamoDB item has an invalid _revision")
        return parsed

    @staticmethod
    def _expect_item(
        table: str,
        actual: Mapping[str, Any],
        expected: Mapping[str, Any],
    ) -> None:
        # The validated model payload is authoritative. Canonical export
        # normalizes equivalent timestamp offsets (for example +03:00 to Z),
        # so its serialized JSON can legitimately differ byte-for-byte from
        # the runtime payload while describing the same model. Compare every
        # physical key and denormalized index, but do not require the original
        # JSON spelling to match the canonical re-serialization.
        actual_fields = _without_revision(actual)
        if "payload" in expected:
            actual_fields.pop("payload", None)
            expected = {key: value for key, value in expected.items() if key != "payload"}
        if actual_fields != expected:
            key = _physical_key(table, actual)
            raise CanonicalValidationError(f"DynamoDB {table} item {key!r} does not match its payload")

    async def export_document(self) -> CanonicalDocument:
        raw_tables = await self._scan_all()
        canonical: CanonicalTables = {table: [] for table in TABLE_NAMES}
        actual_locks: dict[str, dict[str, Any]] = {}

        for item in raw_tables["tg_users"]:
            self._revision("tg_users", item)
            if item.get("record_type") != "user" or not isinstance(item.get("payload"), str):
                raise CanonicalValidationError("unexpected item in DynamoDB tg_users")
            user = TelegramUser.model_validate_json(item["payload"])
            row = canonical_user(user)
            expected_document = build_document(
                {
                    "tg_users": [row],
                    "vk_users": [],
                    "events": [],
                    "event_leaders": [],
                    "registrations": [],
                },
                source="dynamodb",
            )
            expected = dynamodb_physical_items(expected_document)["tg_users"][0]
            self._expect_item("tg_users", item, expected)
            canonical["tg_users"].append(row)

        for item in raw_tables["vk_users"]:
            self._revision("vk_users", item)
            if item.get("record_type") != "user" or not isinstance(item.get("payload"), str):
                raise CanonicalValidationError("unexpected item in DynamoDB vk_users")
            vk_user = VkUser.model_validate_json(item["payload"])
            row = canonical_user(vk_user)
            expected_document = build_document(
                {
                    "tg_users": [],
                    "vk_users": [row],
                    "events": [],
                    "event_leaders": [],
                    "registrations": [],
                },
                source="dynamodb",
            )
            expected = dynamodb_physical_items(expected_document)["vk_users"][0]
            self._expect_item("vk_users", item, expected)
            canonical["vk_users"].append(row)

        for item in raw_tables["events"]:
            self._revision("events", item)
            record_type = item.get("record_type")
            if record_type == "unique_lock":
                key = str(item.get("event_id"))
                if key in actual_locks:
                    raise CanonicalValidationError(f"duplicate DynamoDB event lock {key!r}")
                actual_locks[key] = _without_revision(item)
                continue
            if record_type != "event" or not isinstance(item.get("payload"), str):
                raise CanonicalValidationError("unexpected item in DynamoDB events")
            event = Event.model_validate_json(item["payload"])
            row = canonical_event(event)
            expected_document = build_document(
                {
                    "tg_users": [],
                    "vk_users": [],
                    "events": [row],
                    "event_leaders": [],
                    "registrations": [],
                },
                source="dynamodb",
            )
            expected_items = dynamodb_physical_items(expected_document)["events"]
            expected = next(candidate for candidate in expected_items if candidate["record_type"] == "event")
            self._expect_item("events", item, expected)
            canonical["events"].append(row)

        for item in raw_tables["event_leaders"]:
            self._revision("event_leaders", item)
            if item.get("record_type") != "event_leader":
                raise CanonicalValidationError("unexpected item in DynamoDB event_leaders")
            try:
                platform = Platform(str(item["platform"]))
                user_id = int(str(item["platform_user_id"]))
                event_id = str(item["event_id"])
                created_at = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
            except (KeyError, ValueError) as exc:
                raise CanonicalValidationError("invalid DynamoDB event leader") from exc
            row = canonical_leader(event_id, platform, user_id, created_at)
            expected = {
                "event_id": event_id,
                "leader_key": f"{platform.value}:{user_id}",
                "record_type": "event_leader",
                "platform": platform.value,
                "platform_user_id": str(user_id),
                "created_at": row["created_at"],
            }
            self._expect_item("event_leaders", item, expected)
            canonical["event_leaders"].append(row)

        for item in raw_tables["registrations"]:
            self._revision("registrations", item)
            deliveries = item.get("participation_deliveries", [])
            if (
                item.get("record_type") != "registration"
                or not isinstance(item.get("payload"), str)
                or not isinstance(deliveries, list)
                or not all(isinstance(value, str) for value in deliveries)
            ):
                raise CanonicalValidationError("unexpected item in DynamoDB registrations")
            registration = Registration.model_validate_json(item["payload"])
            row = canonical_registration(registration, deliveries)
            expected = {
                "event_id": registration.event_id,
                "participant_key": registration.participant_key,
                **(
                    {"telegram_profile": registration.telegram_profile}
                    if registration.telegram_profile is not None
                    else {}
                ),
                "record_type": "registration",
                "payload": registration.model_dump_json(),
                "participation_deliveries": sorted(deliveries),
            }
            self._expect_item("registrations", item, expected)
            canonical["registrations"].append(row)

        document = build_document(canonical, source="dynamodb")
        expected_locks = {
            str(item["event_id"]): item
            for item in dynamodb_physical_items(document)["events"]
            if item["record_type"] == "unique_lock"
        }
        if actual_locks != expected_locks:
            missing = sorted(set(expected_locks) - set(actual_locks))
            extra = sorted(set(actual_locks) - set(expected_locks))
            raise CanonicalValidationError(
                f"DynamoDB event uniqueness locks do not match events; missing={missing}, extra={extra}"
            )
        return document

    async def import_document(
        self,
        document: CanonicalDocument,
        *,
        delete_extra: bool = False,
    ) -> CanonicalDocument:
        document = validate_document(document)
        desired_without_revision = dynamodb_physical_items(document)
        current = await self._scan_all()
        desired_by_key = {
            table: {_physical_key(table, item): item for item in desired_without_revision[table]}
            for table in TABLE_NAMES
        }
        current_by_key: dict[str, dict[tuple[str, ...], dict[str, Any]]] = {}
        for table in TABLE_NAMES:
            for item in current[table]:
                self._revision(table, item)
            rows_by_key = {_physical_key(table, item): item for item in current[table]}
            if len(rows_by_key) != len(current[table]):
                raise CanonicalValidationError(f"target DynamoDB {table} contains duplicate primary keys")
            current_by_key[table] = rows_by_key
        extras = {table: sorted(set(current_by_key[table]) - set(desired_by_key[table])) for table in TABLE_NAMES}
        extras = {table: keys for table, keys in extras.items() if keys}
        if extras and not delete_extra:
            summary = ", ".join(f"{table}={len(keys)}" for table, keys in extras.items())
            raise CanonicalValidationError(
                f"target DynamoDB has records absent from the dump ({summary}); "
                "rerun with --delete-extra only in maintenance"
            )

        for table in TABLE_NAMES:
            physical_table = self.table_names.as_dict()[table]
            for key, desired in desired_by_key[table].items():
                existing = current_by_key[table].get(key)
                if existing is not None and _without_revision(existing) == desired:
                    self._revision(table, existing)
                    continue
                revision = 1 if existing is None else self._revision(table, existing) + 1
                request: dict[str, Any] = {
                    "TableName": physical_table,
                    "Item": _serialize_map(desired | {"_revision": revision}),
                }
                if existing is None:
                    partition_key = next(iter(_key_map(table, desired)))
                    request["ConditionExpression"] = "attribute_not_exists(#partition_key)"
                    request["ExpressionAttributeNames"] = {"#partition_key": partition_key}
                else:
                    request.update(
                        {
                            "ConditionExpression": "#revision = :revision",
                            "ExpressionAttributeNames": {"#revision": "_revision"},
                            "ExpressionAttributeValues": _serialize_map({":revision": revision - 1}),
                        }
                    )
                await asyncio.to_thread(self.client.put_item, **request)

        if delete_extra:
            for table in reversed(TABLE_NAMES):
                physical_table = self.table_names.as_dict()[table]
                for key in extras.get(table, []):
                    existing = current_by_key[table][key]
                    revision = self._revision(table, existing)
                    await asyncio.to_thread(
                        self.client.delete_item,
                        TableName=physical_table,
                        Key=_serialize_map(_key_map(table, existing)),
                        ConditionExpression="#revision = :revision",
                        ExpressionAttributeNames={"#revision": "_revision"},
                        ExpressionAttributeValues=_serialize_map({":revision": revision}),
                    )

        exported = await self.export_document()
        compare_documents(document, exported)
        return exported
