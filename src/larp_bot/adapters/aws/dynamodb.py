from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from larp_bot.adapters.memory import MemoryEventRepository, MemoryRegistrationRepository, MemoryUserRepository
from larp_bot.domain.models import Event, Platform, Registration, TelegramUser, User, VkUser

Key = tuple[str, ...]

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_MAX_TRANSACTION_ACTIONS = 100


def _serialize_map(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(item) for key, item in value.items()}


def _deserialize_map(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _DESERIALIZER.deserialize(item) for key, item in value.items()}


@dataclass(frozen=True)
class _StoredItem:
    table: str
    key: dict[str, str]
    revision: int
    document: dict[str, Any]


class _DynamoMemoryRepository[MemoryRepository]:
    """Run proven in-memory semantics, then commit its exact delta atomically.

    The AWS tables are intentionally small during migration. Strongly consistent
    snapshots keep the adapter simple and reversible, while per-item revisions,
    uniqueness sentinels, and DynamoDB transactions prevent lost updates.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _scan(self, table: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            request: dict[str, Any] = {"TableName": table, "ConsistentRead": True}
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = await asyncio.to_thread(self.client.scan, **request)
            items.extend(_deserialize_map(item) for item in response.get("Items", []))
            raw_start_key = response.get("LastEvaluatedKey")
            if not raw_start_key:
                return items
            start_key = raw_start_key

    async def _get(self, table: str, key: Mapping[str, str]) -> dict[str, Any] | None:
        response = await asyncio.to_thread(
            self.client.get_item,
            TableName=table,
            Key=_serialize_map(key),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return _deserialize_map(item) if item else None

    async def _query(
        self,
        table: str,
        *,
        key_condition: str,
        names: Mapping[str, str],
        values: Mapping[str, Any],
        index_name: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "TableName": table,
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeNames": dict(names),
                "ExpressionAttributeValues": _serialize_map(values),
            }
            if index_name is None:
                request["ConsistentRead"] = True
            else:
                request["IndexName"] = index_name
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = await asyncio.to_thread(self.client.query, **request)
            items.extend(_deserialize_map(item) for item in response.get("Items", []))
            raw_start_key = response.get("LastEvaluatedKey")
            if not raw_start_key:
                return items
            start_key = raw_start_key

    async def _load(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> tuple[MemoryRepository, dict[Key, _StoredItem]]:
        raise NotImplementedError

    def _desired(self, repository: MemoryRepository) -> dict[Key, _StoredItem]:
        raise NotImplementedError

    def _extra_actions(
        self,
        original: Mapping[Key, _StoredItem],
        desired: Mapping[Key, _StoredItem],
    ) -> list[dict[str, Any]]:
        del original, desired
        return []

    @staticmethod
    def _put_action(item: _StoredItem, original: _StoredItem | None) -> dict[str, Any]:
        document = item.document | {"_revision": 1 if original is None else original.revision + 1}
        put: dict[str, Any] = {
            "TableName": item.table,
            "Item": _serialize_map(document),
        }
        if original is None:
            partition_key = next(iter(item.key))
            put["ConditionExpression"] = "attribute_not_exists(#partition_key)"
            put["ExpressionAttributeNames"] = {"#partition_key": partition_key}
        else:
            put["ConditionExpression"] = "#revision = :revision"
            put["ExpressionAttributeNames"] = {"#revision": "_revision"}
            put["ExpressionAttributeValues"] = _serialize_map({":revision": original.revision})
        return {"Put": put}

    @staticmethod
    def _delete_action(item: _StoredItem) -> dict[str, Any]:
        return {
            "Delete": {
                "TableName": item.table,
                "Key": _serialize_map(item.key),
                "ConditionExpression": "#revision = :revision",
                "ExpressionAttributeNames": {"#revision": "_revision"},
                "ExpressionAttributeValues": _serialize_map({":revision": item.revision}),
            }
        }

    async def _commit(
        self,
        original: Mapping[Key, _StoredItem],
        desired: Mapping[Key, _StoredItem],
    ) -> None:
        actions: list[dict[str, Any]] = []
        for key, item in desired.items():
            previous = original.get(key)
            if previous is None or previous.document != item.document:
                actions.append(self._put_action(item, previous))
        for key, item in original.items():
            if key not in desired:
                actions.append(self._delete_action(item))
        actions.extend(self._extra_actions(original, desired))
        if not actions:
            return
        if len(actions) > _MAX_TRANSACTION_ACTIONS:
            raise RuntimeError(
                f"DynamoDB mutation requires {len(actions)} atomic actions; maximum is {_MAX_TRANSACTION_ACTIONS}"
            )
        await asyncio.to_thread(self.client.transact_write_items, TransactItems=actions)

    @staticmethod
    def _retryable(error: ClientError) -> bool:
        return error.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
            "TransactionConflictException",
        }

    async def _execute(self, method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        for attempt in range(4):
            repository, original = await self._load(method_name, args, kwargs)
            method: Callable[..., Awaitable[Any]] = getattr(repository, method_name)
            result = await method(*args, **kwargs)
            desired = self._desired(repository)
            try:
                await self._commit(original, desired)
            except ClientError as exc:
                if attempt == 3 or not self._retryable(exc):
                    raise
                await asyncio.sleep(0)
                continue
            return result
        raise AssertionError("unreachable DynamoDB retry loop")

    def __getattr__(self, method_name: str) -> Callable[..., Awaitable[Any]]:
        if method_name.startswith("_"):
            raise AttributeError(method_name)

        async def invoke(*args: Any, **kwargs: Any) -> Any:
            return await self._execute(method_name, args, kwargs)

        return invoke


class DynamoUserRepository(_DynamoMemoryRepository[MemoryUserRepository]):
    _BULK_SNAPSHOT_METHODS = frozenset({"find_linked", "list_all", "list_pass_preferences"})
    _MUTATING_METHODS = frozenset(
        {
            "claim_delivery",
            "clear_privileged_role",
            "grant_larp_experience",
            "save",
            "set_larp_experience",
            "set_privileged_role",
            "update_telegram_handle",
            "update_telegram_link",
        }
    )

    def __init__(self, client: Any, *, telegram_table: str, vk_table: str) -> None:
        super().__init__(client)
        self.telegram_table = telegram_table
        self.vk_table = vk_table
        self._bulk_snapshot_depth = 0
        self._bulk_snapshot_rows: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        self._bulk_snapshot_lock = asyncio.Lock()

    @asynccontextmanager
    async def bulk_read_snapshot(self) -> AsyncIterator[None]:
        """Reuse one complete user snapshot during a single queue batch.

        Game-table projections otherwise scan both user tables once per game.
        Deployment backfills can contain many games, so that pattern can exhaust
        the deliberately small free-tier provisioned capacity. Mutating methods
        invalidate the snapshot before they load state.
        """

        self._bulk_snapshot_depth += 1
        try:
            yield
        finally:
            self._bulk_snapshot_depth -= 1
            if self._bulk_snapshot_depth == 0:
                self._bulk_snapshot_rows = None

    async def _bulk_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with self._bulk_snapshot_lock:
            if self._bulk_snapshot_rows is None:
                self._bulk_snapshot_rows = await asyncio.gather(
                    self._scan(self.telegram_table),
                    self._scan(self.vk_table),
                )
            return self._bulk_snapshot_rows

    @staticmethod
    def _user_item(user: User, table: str, revision: int = 0) -> _StoredItem:
        if isinstance(user, TelegramUser):
            key = {"tg_id": str(user.tg_id)}
            storage_key = (Platform.TELEGRAM.value, str(user.tg_id))
            indexes = {
                **({"telegram_handle": user.telegram_handle} if user.telegram_handle is not None else {}),
                **({"linked_vk_id": str(user.linked_vk_id)} if user.linked_vk_id is not None else {}),
            }
        else:
            key = {"vk_id": str(user.vk_id)}
            storage_key = (Platform.VK.value, str(user.vk_id))
            indexes = {}
        document = key | indexes | {"record_type": "user", "payload": user.model_dump_json()}
        return _StoredItem(table=table, key=key, revision=revision, document=document | {"_storage_key": storage_key})

    @staticmethod
    def _without_internal(item: _StoredItem) -> _StoredItem:
        document = dict(item.document)
        document.pop("_storage_key", None)
        return _StoredItem(item.table, item.key, item.revision, document)

    async def _rows_for_call(
        self,
        method_name: str,
        args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._bulk_snapshot_depth and method_name in self._BULK_SNAPSHOT_METHODS:
            snapshot_telegram_rows, snapshot_vk_rows = await self._bulk_rows()
            return deepcopy(snapshot_telegram_rows), deepcopy(snapshot_vk_rows)

        telegram_rows: list[dict[str, Any]] = []
        vk_rows: list[dict[str, Any]] = []

        async def add_user(platform: Platform, user_id: int) -> None:
            table = self.telegram_table if platform is Platform.TELEGRAM else self.vk_table
            key_name = "tg_id" if platform is Platform.TELEGRAM else "vk_id"
            row = await self._get(table, {key_name: str(user_id)})
            if row is not None:
                (telegram_rows if platform is Platform.TELEGRAM else vk_rows).append(row)

        if method_name in {"list_all", "list_pass_preferences"}:
            return await asyncio.gather(self._scan(self.telegram_table), self._scan(self.vk_table))
        if method_name == "find_telegram_by_handle":
            telegram_rows = await self._query(
                self.telegram_table,
                key_condition="#handle = :handle",
                names={"#handle": "telegram_handle"},
                values={":handle": args[0]},
                index_name="telegram-handle",
            )
            return telegram_rows, vk_rows
        if method_name == "find_linked":
            user = args[0]
            if isinstance(user, TelegramUser):
                if user.telegram_handle is None or user.linked_vk_id is None:
                    return telegram_rows, vk_rows
                telegram_rows = await self._query(
                    self.telegram_table,
                    key_condition="#handle = :handle AND #linked = :linked",
                    names={"#handle": "telegram_handle", "#linked": "linked_vk_id"},
                    values={":handle": user.telegram_handle, ":linked": str(user.linked_vk_id)},
                    index_name="telegram-identity",
                )
                await add_user(Platform.VK, user.linked_vk_id)
            else:
                if user.telegram_handle is None:
                    return telegram_rows, vk_rows
                telegram_rows = await self._query(
                    self.telegram_table,
                    key_condition="#handle = :handle AND #linked = :linked",
                    names={"#handle": "telegram_handle", "#linked": "linked_vk_id"},
                    values={":handle": user.telegram_handle, ":linked": str(user.vk_id)},
                    index_name="telegram-identity",
                )
            return telegram_rows, vk_rows
        if method_name == "save":
            user = args[0]
            platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
            user_id = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
            await add_user(platform, user_id)
            return telegram_rows, vk_rows
        if method_name in {"update_telegram_handle", "update_telegram_link"}:
            await add_user(Platform.TELEGRAM, int(args[0]))
            return telegram_rows, vk_rows
        if args and isinstance(args[0], Platform):
            await add_user(args[0], int(args[1]))
            return telegram_rows, vk_rows
        return await asyncio.gather(self._scan(self.telegram_table), self._scan(self.vk_table))

    async def _execute(self, method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if self._bulk_snapshot_depth and method_name in self._MUTATING_METHODS:
            self._bulk_snapshot_rows = None
        return await super()._execute(method_name, args, kwargs)

    async def _load(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> tuple[MemoryUserRepository, dict[Key, _StoredItem]]:
        del kwargs
        telegram_rows, vk_rows = await self._rows_for_call(method_name, args)
        users: list[User] = []
        stored: dict[Key, _StoredItem] = {}
        for table, model, rows in (
            (self.telegram_table, TelegramUser, telegram_rows),
            (self.vk_table, VkUser, vk_rows),
        ):
            for row in rows:
                if row.get("record_type") != "user":
                    continue
                user = model.model_validate_json(row["payload"])
                item = self._user_item(user, table, int(row.get("_revision", 0)))
                storage_key = item.document["_storage_key"]
                assert isinstance(storage_key, tuple)
                stored[storage_key] = self._without_internal(item)
                users.append(user)
        repository = MemoryUserRepository()
        for user in users:
            platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
            user_id = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
            repository.rows[(platform, user_id)] = deepcopy(user)
        return repository, stored

    def _desired(self, repository: MemoryUserRepository) -> dict[Key, _StoredItem]:
        desired: dict[Key, _StoredItem] = {}
        for (platform, _), user in repository.rows.items():
            table = self.telegram_table if platform is Platform.TELEGRAM else self.vk_table
            item = self._user_item(user, table)
            storage_key = item.document["_storage_key"]
            assert isinstance(storage_key, tuple)
            desired[storage_key] = self._without_internal(item)
        return desired


class _DynamoMemoryEventRepository(MemoryEventRepository):
    def __init__(self, events: list[Event]) -> None:
        super().__init__(events)
        self.leader_created_at: dict[tuple[str, Platform, int], datetime] = {}


class DynamoEventRepository(_DynamoMemoryRepository[MemoryEventRepository]):
    def __init__(self, client: Any, *, events_table: str, leaders_table: str) -> None:
        super().__init__(client)
        self.events_table = events_table
        self.leaders_table = leaders_table

    def _event_item(self, event: Event, revision: int = 0) -> _StoredItem:
        key = {"event_id": event.event_id}
        indexes = {
            **({"signup_token": event.signup_token} if event.signup_token is not None else {}),
            **({"telegram_chat_id": str(event.telegram_chat_id)} if event.telegram_chat_id is not None else {}),
        }
        return _StoredItem(
            table=self.events_table,
            key=key,
            revision=revision,
            document=key | indexes | {"record_type": "event", "payload": event.model_dump_json()},
        )

    def _leader_item(
        self,
        event_id: str,
        platform: Platform,
        user_id: int,
        revision: int = 0,
        *,
        created_at: datetime | None = None,
    ) -> _StoredItem:
        leader_key = f"{platform.value}:{user_id}"
        key = {"event_id": event_id, "leader_key": leader_key}
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("event leader creation time must include a timezone")
        return _StoredItem(
            table=self.leaders_table,
            key=key,
            revision=revision,
            document=key
            | {
                "record_type": "event_leader",
                "platform": platform.value,
                "platform_user_id": str(user_id),
                "created_at": timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            },
        )

    @staticmethod
    def _lock_key(kind: str, value: str) -> str:
        return f"lock:{kind}:{value}"

    def _lock_action(self, kind: str, value: str, event_id: str) -> dict[str, Any]:
        key = self._lock_key(kind, value)
        return {
            "Put": {
                "TableName": self.events_table,
                "Item": _serialize_map(
                    {
                        "event_id": key,
                        "record_type": "unique_lock",
                        "owner_event_id": event_id,
                        "_revision": 1,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(event_id)",
            }
        }

    def _unlock_action(self, kind: str, value: str, event_id: str) -> dict[str, Any]:
        return {
            "Delete": {
                "TableName": self.events_table,
                "Key": _serialize_map({"event_id": self._lock_key(kind, value)}),
                "ConditionExpression": "owner_event_id = :owner",
                "ExpressionAttributeValues": _serialize_map({":owner": event_id}),
            }
        }

    @staticmethod
    def _unique_values(item: _StoredItem | None) -> set[tuple[str, str]]:
        if item is None or item.document.get("record_type") != "event":
            return set()
        values: set[tuple[str, str]] = set()
        signup_token = item.document.get("signup_token")
        telegram_chat_id = item.document.get("telegram_chat_id")
        if isinstance(signup_token, str):
            values.add(("signup", signup_token))
        if isinstance(telegram_chat_id, str):
            values.add(("telegram-chat", telegram_chat_id))
        return values

    def _extra_actions(
        self,
        original: Mapping[Key, _StoredItem],
        desired: Mapping[Key, _StoredItem],
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        event_keys = {key for key, item in original.items() if item.document.get("record_type") == "event"} | {
            key for key, item in desired.items() if item.document.get("record_type") == "event"
        }
        for key in event_keys:
            previous = original.get(key)
            current = desired.get(key)
            previous_values = self._unique_values(previous)
            current_values = self._unique_values(current)
            owner = current or previous
            assert owner is not None
            event_id = str(owner.document["event_id"])
            actions.extend(
                self._unlock_action(kind, value, event_id) for kind, value in previous_values - current_values
            )
            actions.extend(self._lock_action(kind, value, event_id) for kind, value in current_values - previous_values)
        return actions

    async def _event_rows_for_call(
        self,
        method_name: str,
        args: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        event_rows: list[dict[str, Any]] = []
        leader_rows: list[dict[str, Any]] = []

        async def add_event(event_id: str) -> None:
            row = await self._get(self.events_table, {"event_id": event_id})
            if row is not None and row.get("record_type") == "event":
                event_rows.append(row)

        async def add_leaders(event_id: str) -> None:
            leader_rows.extend(
                await self._query(
                    self.leaders_table,
                    key_condition="#event = :event",
                    names={"#event": "event_id"},
                    values={":event": event_id},
                )
            )

        if method_name in {
            "list_page",
            "list_due_lifecycle_events",
            "list_due_registration_notifications",
            "list_due_confirmation_openings",
            "list_due_scheduled_closings",
        }:
            return await self._scan(self.events_table), leader_rows
        if method_name == "get_by_signup_token":
            event_rows = await self._query(
                self.events_table,
                key_condition="#token = :token",
                names={"#token": "signup_token"},
                values={":token": args[0]},
                index_name="signup-token",
            )
            return event_rows, leader_rows
        if method_name == "get_by_telegram_chat_id":
            event_rows = await self._query(
                self.events_table,
                key_condition="#chat = :chat",
                names={"#chat": "telegram_chat_id"},
                values={":chat": str(args[0])},
                index_name="telegram-chat",
            )
            return event_rows, leader_rows
        if method_name == "create":
            event = args[0]
            await add_event(event.event_id)
            return event_rows, leader_rows
        event_id = str(args[0])
        await add_event(event_id)
        if method_name == "bind_telegram_chat":
            matches = await self._query(
                self.events_table,
                key_condition="#chat = :chat",
                names={"#chat": "telegram_chat_id"},
                values={":chat": str(args[1])},
                index_name="telegram-chat",
            )
            known_ids = {str(row["event_id"]) for row in event_rows}
            event_rows.extend(row for row in matches if str(row["event_id"]) not in known_ids)
        if method_name in {"add_leader", "remove_leader", "list_leaders", "delete"}:
            await add_leaders(event_id)
        return event_rows, leader_rows

    async def _load(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> tuple[MemoryEventRepository, dict[Key, _StoredItem]]:
        del kwargs
        event_rows, leader_rows = await self._event_rows_for_call(method_name, args)
        events: list[Event] = []
        stored: dict[Key, _StoredItem] = {}
        for row in event_rows:
            if row.get("record_type") != "event":
                continue
            event = Event.model_validate_json(row["payload"])
            item = self._event_item(event, int(row.get("_revision", 0)))
            stored[("event", event.event_id)] = item
            events.append(event)
        repository = _DynamoMemoryEventRepository(events)
        for row in leader_rows:
            if row.get("record_type") != "event_leader":
                continue
            event_id = str(row["event_id"])
            platform = Platform(str(row["platform"]))
            user_id = int(row["platform_user_id"])
            created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            repository.leaders.setdefault(event_id, set()).add((platform, user_id))
            repository.leader_created_at[(event_id, platform, user_id)] = created_at
            stored[("leader", event_id, platform.value, str(user_id))] = self._leader_item(
                event_id,
                platform,
                user_id,
                int(row.get("_revision", 0)),
                created_at=created_at,
            )
        return repository, stored

    def _desired(self, repository: MemoryEventRepository) -> dict[Key, _StoredItem]:
        desired: dict[Key, _StoredItem] = {
            ("event", event_id): self._event_item(event) for event_id, event in repository.rows.items()
        }
        for event_id, leaders in repository.leaders.items():
            if event_id not in repository.rows:
                continue
            for platform, user_id in leaders:
                leader_created_at = (
                    repository.leader_created_at if isinstance(repository, _DynamoMemoryEventRepository) else {}
                )
                desired[("leader", event_id, platform.value, str(user_id))] = self._leader_item(
                    event_id,
                    platform,
                    user_id,
                    created_at=leader_created_at.get((event_id, platform, user_id)),
                )
        return desired


class DynamoRegistrationRepository(_DynamoMemoryRepository[MemoryRegistrationRepository]):
    def __init__(self, client: Any, *, registrations_table: str) -> None:
        super().__init__(client)
        self.registrations_table = registrations_table

    def _registration_item(
        self,
        registration: Registration,
        deliveries: set[str],
        revision: int = 0,
    ) -> _StoredItem:
        key = {
            "event_id": registration.event_id,
            "participant_key": registration.participant_key,
        }
        indexes = (
            {"telegram_profile": registration.telegram_profile} if registration.telegram_profile is not None else {}
        )
        return _StoredItem(
            table=self.registrations_table,
            key=key,
            revision=revision,
            document=key
            | indexes
            | {
                "record_type": "registration",
                "payload": registration.model_dump_json(),
                "participation_deliveries": sorted(deliveries),
            },
        )

    async def _registration_rows_for_call(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        async def add(event_id: str, participant_key: str) -> None:
            row = await self._get(
                self.registrations_table,
                {"event_id": event_id, "participant_key": participant_key},
            )
            if row is not None:
                rows.append(row)

        async def add_many(keys: list[tuple[str, str]]) -> None:
            loaded = await asyncio.gather(*(add(event_id, participant_key) for event_id, participant_key in keys))
            del loaded

        async def add_event(event_id: str) -> None:
            rows.extend(
                await self._query(
                    self.registrations_table,
                    key_condition="#event = :event",
                    names={"#event": "event_id"},
                    values={":event": event_id},
                )
            )

        if method_name == "find_by_telegram_profile":
            return await self._query(
                self.registrations_table,
                key_condition="#profile = :profile",
                names={"#profile": "telegram_profile"},
                values={":profile": args[0]},
                index_name="telegram-profile",
            )
        if method_name == "import_missing":
            registrations = args[0]
            await add_many([(item.event_id, item.participant_key) for item in registrations])
            return rows
        event_id = str(args[0])
        if method_name in {
            "list_for_event",
            "list_page_for_event",
            "enlist",
            "sift_waiting_to_end",
            "delete_for_event",
        }:
            await add_event(event_id)
            return rows
        if method_name == "confirm":
            keys = [(event_id, str(kwargs["participant_key"]))]
            replaced_event_id = kwargs.get("replaced_event_id")
            replaced_participant_key = kwargs.get("replaced_participant_key")
            if isinstance(replaced_event_id, str) and isinstance(replaced_participant_key, str):
                keys.append((replaced_event_id, replaced_participant_key))
            await add_many(keys)
            return rows
        linked_methods = {
            "confirm_linked",
            "update_linked_character_wish",
            "update_linked_co_player_wish",
            "cancel_linked",
        }
        if method_name in linked_methods:
            keys = [(event_id, str(key)) for key in kwargs["participant_keys"]]
            replaced_event_id = kwargs.get("replaced_event_id")
            if isinstance(replaced_event_id, str):
                keys.extend((replaced_event_id, str(key)) for key in kwargs.get("replaced_participant_keys", ()))
            await add_many(keys)
            return rows
        if method_name == "swap_queue_positions":
            await add_many(
                [
                    (event_id, str(kwargs["promoted_participant_key"])),
                    (event_id, str(kwargs["demoted_participant_key"])),
                ]
            )
            return rows
        if method_name == "remove_many":
            await add_many([(event_id, str(key)) for key in kwargs["participant_keys"]])
            return rows
        participant_key = kwargs.get("participant_key")
        if participant_key is None and len(args) > 1:
            participant_key = args[1]
        if isinstance(participant_key, str):
            await add(event_id, participant_key)
            return rows
        await add_event(event_id)
        return rows

    async def _load(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> tuple[MemoryRegistrationRepository, dict[Key, _StoredItem]]:
        rows = await self._registration_rows_for_call(method_name, args, kwargs)
        registrations: list[Registration] = []
        deliveries_by_key: dict[tuple[str, str], set[str]] = {}
        stored: dict[Key, _StoredItem] = {}
        for row in rows:
            if row.get("record_type") != "registration":
                continue
            registration = Registration.model_validate_json(row["payload"])
            deliveries = {value for value in row.get("participation_deliveries", []) if isinstance(value, str)}
            item = self._registration_item(registration, deliveries, int(row.get("_revision", 0)))
            key = (registration.event_id, registration.participant_key)
            stored[("registration", *key)] = item
            deliveries_by_key[key] = deliveries
            registrations.append(registration)
        repository = MemoryRegistrationRepository(registrations)
        repository.participation_deliveries = {
            (event_id, participant_key, request_id)
            for (event_id, participant_key), request_ids in deliveries_by_key.items()
            for request_id in request_ids
        }
        return repository, stored

    def _desired(self, repository: MemoryRegistrationRepository) -> dict[Key, _StoredItem]:
        deliveries_by_key: dict[tuple[str, str], set[str]] = {}
        for event_id, participant_key, request_id in repository.participation_deliveries:
            deliveries_by_key.setdefault((event_id, participant_key), set()).add(request_id)
        return {
            ("registration", event_id, participant_key): self._registration_item(
                registration,
                deliveries_by_key.get((event_id, participant_key), set()),
            )
            for (event_id, participant_key), registration in repository.rows.items()
        }
