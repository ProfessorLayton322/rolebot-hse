from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar, cast

import ydb
from pydantic import ValidationError

from larp_bot.domain.models import (
    Button,
    PassDetails,
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
        def operation(session: Any) -> list[dict[str, Any]]:
            # The table SDK silently ignores parameters bound to raw YQL text;
            # binding is supported only for a prepared DataQuery.
            prepared = session.prepare(yql)
            transaction_mode = ydb.SnapshotReadOnly() if read_only else ydb.SerializableReadWrite()
            result = session.transaction(transaction_mode).execute(
                prepared,
                params or {},
                commit_tx=True,
            )
            return _rows(result)

        return await asyncio.to_thread(self.pool.retry_operation_sync, operation)

    async def snapshot(self, queries: Sequence[str]) -> list[list[dict[str, Any]]]:
        """Read several tables from one consistent snapshot transaction."""

        if not queries:
            return []

        def operation(session: Any) -> list[list[dict[str, Any]]]:
            transaction = session.transaction(ydb.SnapshotReadOnly()).begin()
            snapshots: list[list[dict[str, Any]]] = []
            for index, query in enumerate(queries):
                result_sets = transaction.execute(
                    session.prepare(query),
                    {},
                    commit_tx=index == len(queries) - 1,
                )
                snapshots.append(_rows(result_sets))
            return snapshots

        return await asyncio.to_thread(self.pool.retry_operation_sync, operation)

    async def claim_delivery(
        self,
        *,
        table: str,
        id_column: str,
        user_id: int,
        operation_id: str,
        buttons: Sequence[Button] = (),
    ) -> bool:
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
                "DECLARE $last_bot_buttons AS Utf8; "
                f"UPDATE `{table}` SET last_delivery_operation_id = $operation_id, "
                "last_bot_buttons_json = $last_bot_buttons "
                f"WHERE {id_column} = $user_id;"
            )
            transaction.execute(
                session.prepare(update),
                {
                    "$user_id": user_id,
                    "$operation_id": operation_id,
                    "$last_bot_buttons": json.dumps(
                        [button.model_dump() for button in buttons],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
                commit_tx=True,
            )
            return True

        return cast(bool, await asyncio.to_thread(self.pool.retry_operation_sync, operation))

    async def mark_participation_notification_delivered(
        self, event_id: str, participant_key: str, request_id: str
    ) -> None:
        """Append a delivery marker without overwriting concurrent recipients."""

        def operation(session: Any) -> None:
            transaction = session.transaction(ydb.SerializableReadWrite()).begin()
            params = {"$event_id": event_id, "$participant_key": participant_key}
            rows = _rows(
                transaction.execute(
                    session.prepare(
                        "DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8; "
                        "SELECT participation_notification_deliveries_json FROM `registrations` "
                        "WHERE event_id = $event_id AND participant_key = $participant_key;"
                    ),
                    params,
                )
            )
            if not rows:
                raise RuntimeError("registration disappeared during notification delivery")
            delivered = json.loads(rows[0].get("participation_notification_deliveries_json") or "[]")
            if request_id in delivered:
                transaction.commit()
                return
            delivered.append(request_id)
            transaction.execute(
                session.prepare(
                    "DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8; DECLARE $delivered AS Utf8; "
                    "UPDATE `registrations` SET participation_notification_deliveries_json = $delivered "
                    "WHERE event_id = $event_id AND participant_key = $participant_key;"
                ),
                {**params, "$delivered": json.dumps(delivered, separators=(",", ":"))},
                commit_tx=True,
            )

        await asyncio.to_thread(self.pool.retry_operation_sync, operation)

    async def insert_if_absent(
        self,
        *,
        select_yql: str,
        select_params: dict[str, Any],
        insert_yql: str,
        insert_params: dict[str, Any],
    ) -> bool:
        """Serialize a migration insert against concurrent normal mutations."""

        def operation(session: Any) -> bool:
            transaction = session.transaction(ydb.SerializableReadWrite()).begin()
            rows = _rows(transaction.execute(session.prepare(select_yql), select_params))
            if rows:
                transaction.commit()
                return False
            transaction.execute(session.prepare(insert_yql), insert_params, commit_tx=True)
            return True

        return cast(bool, await asyncio.to_thread(self.pool.retry_operation_sync, operation))

    async def bind_event_chat(self, event_id: str, chat_id: int) -> bool:
        """Create a one-to-one event/chat binding in one serializable transaction."""

        def operation(session: Any) -> bool:
            transaction = session.transaction(ydb.SerializableReadWrite()).begin()
            target_query = """
                DECLARE $event_id AS Utf8;
                SELECT event_id, archived_at, telegram_chat_id FROM `events`
                WHERE event_id = $event_id;
            """
            target_rows = _rows(transaction.execute(session.prepare(target_query), {"$event_id": event_id}))
            if not target_rows or target_rows[0].get("archived_at") is not None:
                transaction.commit()
                return False
            existing_chat_id = target_rows[0].get("telegram_chat_id")
            if existing_chat_id is not None:
                transaction.commit()
                return int(existing_chat_id) == chat_id
            conflict_query = """
                DECLARE $chat_id AS Int64;
                SELECT event_id FROM `events` WHERE telegram_chat_id = $chat_id LIMIT 1;
            """
            conflicts = _rows(transaction.execute(session.prepare(conflict_query), {"$chat_id": chat_id}))
            if conflicts:
                transaction.commit()
                return False
            update_query = """
                DECLARE $event_id AS Utf8; DECLARE $chat_id AS Int64;
                DECLARE $updated_at AS Timestamp;
                UPDATE `events`
                SET telegram_chat_id = $chat_id, updated_at = $updated_at
                WHERE event_id = $event_id AND telegram_chat_id IS NULL;
            """
            transaction.execute(
                session.prepare(update_query),
                {"$event_id": event_id, "$chat_id": chat_id, "$updated_at": datetime.now(UTC)},
                commit_tx=True,
            )
            return True

        return cast(bool, await asyncio.to_thread(self.pool.retry_operation_sync, operation))


def _optional_pass(raw: object) -> PassDetails | None:
    if not raw:
        return None
    try:
        return PassDetails.model_validate_json(str(raw))
    except (ValidationError, ValueError):
        # Profiles saved before the structured pass schema do not contain all
        # mandatory fields. Keep the user row readable but make the profile
        # incomplete until the player fills it in again.
        return None


def _context(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict):
        raise ValueError("dialog_context must be an object")
    return parsed


def _buttons(raw: object) -> list[Button]:
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
        if not isinstance(parsed, list):
            return []
        return [Button.model_validate(item) for item in parsed]
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError):
        return []


def _string_list(raw: object, column: str) -> list[str]:
    if not raw:
        return []
    parsed = json.loads(str(raw))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{column} must be a list of strings")
    return [str(item) for item in parsed]


def _confirmed_notifications(raw: object) -> list[str]:
    return _string_list(raw, "confirmed_notifications_json")


def _dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC)
    # The YDB Table SDK represents Timestamp values as integer microseconds
    # since Unix epoch rather than datetime objects.
    if type(value) is int:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # The YDB Table SDK represents Date values as whole days since Unix epoch.
    if type(value) is int:
        return date(1970, 1, 1) + timedelta(days=value)
    return date.fromisoformat(str(value))
