from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

import ydb

from larp_bot.domain.models import (
    Event,
    EventStatus,
    PassDetails,
    Platform,
    TelegramUser,
    User,
    VkUser,
)

T = TypeVar("T")


def _rows(result_sets: Sequence[Any]) -> list[dict[str, Any]]:
    if not result_sets:
        return []
    return [dict(row) for row in result_sets[0].rows]


class YdbExecutor:
    """Small synchronous-SDK bridge; Cloud Functions reuse the driver on warm starts."""

    def __init__(self, endpoint: str, database: str, *, iam_token: str | None = None) -> None:
        credentials = ydb.AccessTokenCredentials(iam_token) if iam_token else ydb.iam.MetadataUrlCredentials()
        self.driver = ydb.Driver(endpoint=endpoint, database=database, credentials=credentials)
        self.driver.wait(timeout=10, fail_fast=True)
        self.pool = ydb.SessionPool(self.driver, size=10)

    def close(self) -> None:
        self.pool.stop()
        self.driver.stop(timeout=5)

    async def query(
        self,
        yql: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        del read_only

        def operation(session: Any) -> list[dict[str, Any]]:
            # The dialog FSM, update deduplication and event transitions all need
            # strong reads. Serializable transactions deliberately trade a little
            # latency for deterministic behavior across warm Function instances.
            # The table SDK silently ignores parameters bound to raw YQL text;
            # binding is supported only for a prepared DataQuery.
            prepared = session.prepare(yql)
            result = session.transaction(ydb.SerializableReadWrite()).execute(
                prepared,
                params or {},
                commit_tx=True,
            )
            return _rows(result)

        return await asyncio.to_thread(self.pool.retry_operation_sync, operation)

    async def claim_delivery(self, *, table: str, id_column: str, user_id: int, operation_id: str) -> bool:
        if (table, id_column) not in {("tg_users", "tg_id"), ("vk_users", "vk_id")}:
            raise ValueError("invalid user table")

        def operation(session: Any) -> bool:
            transaction = session.transaction(ydb.SerializableReadWrite()).begin()
            select = (
                f"DECLARE $user_id AS Uint64; "
                f"SELECT last_delivery_operation_id FROM `{table}` "
                f"WHERE {id_column} = $user_id;"
            )
            rows = _rows(transaction.execute(session.prepare(select), {"$user_id": user_id}))
            previous = rows[0].get("last_delivery_operation_id") if rows else None
            if previous == operation_id:
                transaction.commit()
                return False
            update = (
                "DECLARE $user_id AS Uint64; DECLARE $operation_id AS Utf8; "
                f"UPDATE `{table}` SET last_delivery_operation_id = $operation_id "
                f"WHERE {id_column} = $user_id;"
            )
            transaction.execute(
                session.prepare(update),
                {"$user_id": user_id, "$operation_id": operation_id},
                commit_tx=True,
            )
            return True

        return cast(bool, await asyncio.to_thread(self.pool.retry_operation_sync, operation))


def _optional_pass(raw: object) -> PassDetails | None:
    if not raw:
        return None
    return PassDetails.model_validate_json(str(raw))


def _context(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict):
        raise ValueError("dialog_context must be an object")
    return parsed


def _dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC)
    # The YDB Table SDK represents Timestamp values as integer microseconds
    # since Unix epoch rather than datetime objects.
    if type(value) is int:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


class YdbUserRepository:
    COMMON_COLUMNS = """
        full_name, crossplay, larp_experience, needs_pass, pass_details_json,
        dialog_state, dialog_context_json, last_update_id, last_update_at,
        last_delivery_operation_id, created_at, updated_at
    """

    def __init__(self, executor: YdbExecutor) -> None:
        self.db = executor

    @staticmethod
    def _location(platform: Platform) -> tuple[str, str, str]:
        if platform is Platform.TELEGRAM:
            return "tg_users", "tg_id", "vk_url"
        if platform is Platform.VK:
            return "vk_users", "vk_id", "telegram_handle"
        raise ValueError("system has no user table")

    async def get(self, platform: Platform, user_id: int) -> User | None:
        table, id_column, contact_column = self._location(platform)
        query = f"""
            DECLARE $user_id AS Uint64;
            SELECT {id_column}, {contact_column}, {self.COMMON_COLUMNS}
            FROM `{table}` WHERE {id_column} = $user_id;
        """
        rows = await self.db.query(query, {"$user_id": user_id}, read_only=True)
        if not rows:
            return None
        row = rows[0]
        common = {
            "full_name": row.get("full_name"),
            "crossplay": row.get("crossplay"),
            "larp_experience": row.get("larp_experience"),
            "needs_pass": row.get("needs_pass"),
            "pass_details": _optional_pass(row.get("pass_details_json")),
            "dialog_state": row.get("dialog_state") or "IDLE",
            "dialog_context": _context(row.get("dialog_context_json")),
            "last_update_id": row.get("last_update_id"),
            "last_update_at": _dt(row["last_update_at"]) if row.get("last_update_at") is not None else None,
            "last_delivery_operation_id": row.get("last_delivery_operation_id"),
            "created_at": _dt(row["created_at"]),
            "updated_at": _dt(row["updated_at"]),
        }
        if platform is Platform.TELEGRAM:
            return TelegramUser.model_validate({"tg_id": user_id, "vk_url": row.get("vk_url"), **common})
        return VkUser.model_validate({"vk_id": user_id, "telegram_handle": row.get("telegram_handle"), **common})

    async def save(self, user: User) -> None:
        if isinstance(user, TelegramUser):
            table, id_column, contact_column = "tg_users", "tg_id", "vk_url"
            user_id, contact = user.tg_id, user.vk_url
        else:
            table, id_column, contact_column = "vk_users", "vk_id", "telegram_handle"
            user_id, contact = user.vk_id, user.telegram_handle
        query = f"""
            DECLARE $user_id AS Uint64;
            DECLARE $contact AS Optional<Utf8>;
            DECLARE $full_name AS Optional<Utf8>;
            DECLARE $crossplay AS Optional<Bool>;
            DECLARE $larp_experience AS Optional<Bool>;
            DECLARE $needs_pass AS Optional<Bool>;
            DECLARE $pass_details AS Optional<Utf8>;
            DECLARE $dialog_state AS Utf8;
            DECLARE $dialog_context AS Utf8;
            DECLARE $last_update_id AS Optional<Utf8>;
            DECLARE $last_update_at AS Optional<Timestamp>;
            DECLARE $last_delivery_operation_id AS Optional<Utf8>;
            DECLARE $created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO `{table}` (
                {id_column}, {contact_column}, full_name, crossplay, larp_experience,
                needs_pass, pass_details_json, dialog_state, dialog_context_json,
                last_update_id, last_update_at, last_delivery_operation_id,
                created_at, updated_at
            ) VALUES (
                $user_id, $contact, $full_name, $crossplay, $larp_experience,
                $needs_pass, $pass_details, $dialog_state, $dialog_context,
                $last_update_id, $last_update_at, $last_delivery_operation_id,
                $created_at, $updated_at
            );
        """
        await self.db.query(
            query,
            {
                "$user_id": user_id,
                "$contact": contact,
                "$full_name": user.full_name,
                "$crossplay": user.crossplay,
                "$larp_experience": user.larp_experience,
                "$needs_pass": user.needs_pass,
                "$pass_details": (user.pass_details.model_dump_json() if user.pass_details else None),
                "$dialog_state": user.dialog_state,
                "$dialog_context": json.dumps(user.dialog_context, ensure_ascii=False, separators=(",", ":")),
                "$last_update_id": user.last_update_id,
                "$last_update_at": user.last_update_at,
                "$last_delivery_operation_id": user.last_delivery_operation_id,
                "$created_at": user.created_at,
                "$updated_at": user.updated_at,
            },
        )

    async def claim_delivery(self, platform: Platform, user_id: int, operation_id: str) -> bool:
        table, id_column, _ = self._location(platform)
        return await self.db.claim_delivery(
            table=table, id_column=id_column, user_id=user_id, operation_id=operation_id
        )


class YdbEventRepository:
    def __init__(self, executor: YdbExecutor) -> None:
        self.db = executor

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Event:
        return Event(
            event_id=row["event_id"],
            name=row["name"],
            disk_resource_path=row["disk_resource_path"],
            public_registration_url=row["public_registration_url"],
            status=EventStatus(row["status"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    async def get(self, event_id: str) -> Event | None:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8;
            SELECT event_id, name, disk_resource_path, public_registration_url,
                   status, created_at, updated_at
            FROM `events` WHERE event_id = $event_id;
            """,
            {"$event_id": event_id},
            read_only=True,
        )
        return None if not rows else self._from_row(rows[0])

    async def create(self, event: Event) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $name AS Utf8;
            DECLARE $disk_resource_path AS Utf8; DECLARE $public_url AS Utf8;
            DECLARE $status AS Utf8; DECLARE $created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            INSERT INTO `events` (
                event_id, name, disk_resource_path, public_registration_url,
                status, created_at, updated_at
            ) VALUES (
                $event_id, $name, $disk_resource_path, $public_url,
                $status, $created_at, $updated_at
            );
            """,
            {
                "$event_id": event.event_id,
                "$name": event.name,
                "$disk_resource_path": event.disk_resource_path,
                "$public_url": event.public_registration_url,
                "$status": event.status.value,
                "$created_at": event.created_at,
                "$updated_at": event.updated_at,
            },
        )

    async def set_status(self, event_id: str, status: EventStatus) -> bool:
        event = await self.get(event_id)
        if event is None:
            return False
        if event.status is status:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events` SET status = $status, updated_at = $updated_at
            WHERE event_id = $event_id;
            """,
            {
                "$event_id": event_id,
                "$status": status.value,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def delete(self, event_id: str) -> bool:
        exists = await self.get(event_id)
        if exists is None:
            return False
        await self.db.query(
            "DECLARE $event_id AS Utf8; DELETE FROM `events` WHERE event_id = $event_id;",
            {"$event_id": event_id},
        )
        return True

    async def list_page(
        self,
        *,
        status: EventStatus | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]:
        if limit < 1 or limit > 10:
            raise ValueError("event page size must be between 1 and 10")
        conditions: list[str] = []
        params: dict[str, Any] = {"$limit": limit}
        declarations = ["DECLARE $limit AS Uint64;"]
        if status is not None:
            conditions.append("status = $status")
            declarations.append("DECLARE $status AS Utf8;")
            params["$status"] = status.value
        if after is not None:
            conditions.append("(created_at > $after_time OR (created_at = $after_time AND event_id > $after_id))")
            declarations.extend(["DECLARE $after_time AS Timestamp;", "DECLARE $after_id AS Utf8;"])
            params.update({"$after_time": after[0], "$after_id": after[1]})
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            {" ".join(declarations)}
            SELECT event_id, name, disk_resource_path, public_registration_url,
                   status, created_at, updated_at
            FROM `events` {where}
            ORDER BY created_at ASC, event_id ASC
            LIMIT $limit;
        """
        rows = await self.db.query(query, params, read_only=True)
        return [self._from_row(row) for row in rows]
