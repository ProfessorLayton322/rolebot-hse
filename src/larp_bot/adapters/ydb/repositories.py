from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

import ydb
from pydantic import ValidationError

from larp_bot.domain.models import (
    AttendanceStatus,
    Button,
    Event,
    EventStatus,
    PassDetails,
    Platform,
    Registration,
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
                "DECLARE $last_bot_buttons AS Utf8; "
                f"UPDATE `{table}` SET last_delivery_operation_id = $operation_id, "
                "last_bot_buttons_json = $last_bot_buttons "
                f"WHERE {id_column} = $user_id;"
            )
            transaction.execute(
                session.prepare(update),
                {"$user_id": user_id, "$operation_id": operation_id, "$last_bot_buttons": "[]"},
                commit_tx=True,
            )
            return True

        return cast(bool, await asyncio.to_thread(self.pool.retry_operation_sync, operation))

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
        last_delivery_operation_id, last_bot_buttons_json, is_gamemaster,
        gamemaster_grant_operation_id, created_at, updated_at
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
        platform_columns = f"{contact_column}, telegram_handle" if platform is Platform.TELEGRAM else contact_column
        query = f"""
            DECLARE $user_id AS Uint64;
            SELECT {id_column}, {platform_columns}, {self.COMMON_COLUMNS}
            FROM `{table}` WHERE {id_column} = $user_id;
        """
        rows = await self.db.query(query, {"$user_id": user_id}, read_only=True)
        if not rows:
            return None
        return self._from_row(platform, user_id, rows[0])

    @staticmethod
    def _from_row(platform: Platform, user_id: int, row: dict[str, Any]) -> User:
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
            "last_bot_buttons": _buttons(row.get("last_bot_buttons_json")),
            "is_gamemaster": row.get("is_gamemaster") or False,
            "gamemaster_grant_operation_id": row.get("gamemaster_grant_operation_id"),
            "created_at": _dt(row["created_at"]),
            "updated_at": _dt(row["updated_at"]),
        }
        if platform is Platform.TELEGRAM:
            return TelegramUser.model_validate(
                {
                    "tg_id": user_id,
                    "vk_url": row.get("vk_url"),
                    "telegram_handle": row.get("telegram_handle"),
                    **common,
                }
            )
        return VkUser.model_validate({"vk_id": user_id, "telegram_handle": row.get("telegram_handle"), **common})

    async def find_telegram_by_handle(self, handle: str) -> TelegramUser | None:
        rows = await self.db.query(
            f"""
            DECLARE $handle AS Utf8;
            SELECT tg_id, vk_url, telegram_handle, {self.COMMON_COLUMNS}
            FROM `tg_users` WHERE telegram_handle = $handle
            LIMIT 2;
            """,
            {"$handle": handle},
            read_only=True,
        )
        if len(rows) != 1:
            return None
        user = self._from_row(Platform.TELEGRAM, int(rows[0]["tg_id"]), rows[0])
        assert isinstance(user, TelegramUser)
        return user

    async def list_all(self) -> Sequence[User]:
        telegram_rows, vk_rows = await asyncio.gather(
            self.db.query(
                f"SELECT tg_id, vk_url, telegram_handle, {self.COMMON_COLUMNS} FROM `tg_users`;",
                read_only=True,
            ),
            self.db.query(
                f"SELECT vk_id, telegram_handle, {self.COMMON_COLUMNS} FROM `vk_users`;",
                read_only=True,
            ),
        )
        return [
            *(self._from_row(Platform.TELEGRAM, int(row["tg_id"]), row) for row in telegram_rows),
            *(self._from_row(Platform.VK, int(row["vk_id"]), row) for row in vk_rows),
        ]

    async def save(self, user: User) -> None:
        if isinstance(user, TelegramUser):
            table, id_column = "tg_users", "tg_id"
            user_id = user.tg_id
            contact_declarations = "DECLARE $vk_url AS Optional<Utf8>; DECLARE $telegram_handle AS Optional<Utf8>;"
            contact_columns = "vk_url, telegram_handle,"
            contact_values = "$vk_url, $telegram_handle,"
            contact_params = {"$vk_url": user.vk_url, "$telegram_handle": user.telegram_handle}
        else:
            table, id_column = "vk_users", "vk_id"
            user_id = user.vk_id
            contact_declarations = "DECLARE $telegram_handle AS Optional<Utf8>;"
            contact_columns = "telegram_handle,"
            contact_values = "$telegram_handle,"
            contact_params = {"$telegram_handle": user.telegram_handle}
        query = f"""
            DECLARE $user_id AS Uint64;
            {contact_declarations}
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
            DECLARE $last_bot_buttons AS Utf8;
            DECLARE $created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO `{table}` (
                {id_column}, {contact_columns} full_name, crossplay, larp_experience,
                needs_pass, pass_details_json, dialog_state, dialog_context_json,
                last_update_id, last_update_at, last_delivery_operation_id, last_bot_buttons_json,
                created_at, updated_at
            ) VALUES (
                $user_id, {contact_values} $full_name, $crossplay, $larp_experience,
                $needs_pass, $pass_details, $dialog_state, $dialog_context,
                $last_update_id, $last_update_at, $last_delivery_operation_id, $last_bot_buttons,
                $created_at, $updated_at
            );
        """
        await self.db.query(
            query,
            {
                "$user_id": user_id,
                **contact_params,
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
                "$last_bot_buttons": json.dumps(
                    [button.model_dump() for button in user.last_bot_buttons],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "$created_at": user.created_at,
                "$updated_at": user.updated_at,
            },
        )

    async def grant_gamemaster(self, platform: Platform, user_id: int, operation_id: str) -> None:
        table, id_column, _ = self._location(platform)
        await self.db.query(
            f"""
            DECLARE $user_id AS Uint64;
            DECLARE $operation_id AS Utf8;
            UPDATE `{table}`
            SET is_gamemaster = TRUE, gamemaster_grant_operation_id = $operation_id
            WHERE {id_column} = $user_id
              AND (is_gamemaster IS NULL OR is_gamemaster = FALSE);
            """,
            {"$user_id": user_id, "$operation_id": operation_id},
        )

    async def claim_delivery(self, platform: Platform, user_id: int, operation_id: str) -> bool:
        table, id_column, _ = self._location(platform)
        return await self.db.claim_delivery(
            table=table, id_column=id_column, user_id=user_id, operation_id=operation_id
        )


class YdbEventRepository:
    COLUMNS = """
        event_id, name, disk_resource_path, public_registration_url,
        public_table_resource_path, public_table_public_url,
        status, confirmation_deadline, registrations_migrated_at,
        pass_table_resource_path, pass_table_public_url, created_at, updated_at
    """

    def __init__(self, executor: YdbExecutor) -> None:
        self.db = executor

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Event:
        # OPEN was the only non-final status before the confirmation gate was
        # introduced. Preserve its former behavior during a rolling deploy.
        raw_status = str(row["status"])
        status = EventStatus.CONFIRMATION_OPEN if raw_status == "OPEN" else EventStatus(raw_status)
        return Event(
            event_id=row["event_id"],
            name=row["name"],
            disk_resource_path=row["disk_resource_path"],
            public_registration_url=row["public_registration_url"],
            public_table_resource_path=row.get("public_table_resource_path"),
            public_table_public_url=row.get("public_table_public_url"),
            status=status,
            confirmation_deadline=(
                _dt(row["confirmation_deadline"]) if row.get("confirmation_deadline") is not None else None
            ),
            registrations_migrated_at=(
                _dt(row["registrations_migrated_at"]) if row.get("registrations_migrated_at") is not None else None
            ),
            pass_table_resource_path=row.get("pass_table_resource_path"),
            pass_table_public_url=row.get("pass_table_public_url"),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    async def get(self, event_id: str) -> Event | None:
        rows = await self.db.query(
            f"""
            DECLARE $event_id AS Utf8;
            SELECT {self.COLUMNS}
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
            DECLARE $public_table_resource_path AS Optional<Utf8>;
            DECLARE $public_table_public_url AS Optional<Utf8>;
            DECLARE $status AS Utf8; DECLARE $created_at AS Timestamp;
            DECLARE $confirmation_deadline AS Optional<Timestamp>;
            DECLARE $registrations_migrated_at AS Timestamp;
            DECLARE $pass_table_resource_path AS Optional<Utf8>;
            DECLARE $pass_table_public_url AS Optional<Utf8>;
            DECLARE $updated_at AS Timestamp;
            INSERT INTO `events` (
                event_id, name, disk_resource_path, public_registration_url,
                public_table_resource_path, public_table_public_url,
                status, confirmation_deadline, registrations_migrated_at,
                pass_table_resource_path, pass_table_public_url, created_at, updated_at
            ) VALUES (
                $event_id, $name, $disk_resource_path, $public_url,
                $public_table_resource_path, $public_table_public_url,
                $status, $confirmation_deadline, $registrations_migrated_at,
                $pass_table_resource_path, $pass_table_public_url, $created_at, $updated_at
            );
            """,
            {
                "$event_id": event.event_id,
                "$name": event.name,
                "$disk_resource_path": event.disk_resource_path,
                "$public_url": event.public_registration_url,
                "$public_table_resource_path": event.public_table_resource_path,
                "$public_table_public_url": event.public_table_public_url,
                "$status": event.status.value,
                "$confirmation_deadline": event.confirmation_deadline,
                "$registrations_migrated_at": event.registrations_migrated_at,
                "$pass_table_resource_path": event.pass_table_resource_path,
                "$pass_table_public_url": event.pass_table_public_url,
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

    async def open_confirmation(self, event_id: str, deadline: datetime) -> bool:
        event = await self.get(event_id)
        if event is None:
            return False
        if event.status is EventStatus.CONFIRMATION_OPEN and event.confirmation_deadline == deadline:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $deadline AS Timestamp; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET status = $status, confirmation_deadline = $deadline, updated_at = $updated_at
            WHERE event_id = $event_id;
            """,
            {
                "$event_id": event_id,
                "$status": EventStatus.CONFIRMATION_OPEN.value,
                "$deadline": deadline.astimezone(UTC),
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def mark_registrations_migrated(self, event_id: str, migrated_at: datetime) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $migrated_at AS Timestamp;
            UPDATE `events`
            SET registrations_migrated_at = $migrated_at, updated_at = $migrated_at
            WHERE event_id = $event_id;
            """,
            {"$event_id": event_id, "$migrated_at": migrated_at},
        )

    async def set_public_table(self, event_id: str, resource_path: str, public_url: str) -> bool:
        event = await self.get(event_id)
        if event is None or event.public_table_public_url is not None:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $resource_path AS Utf8;
            DECLARE $public_url AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET public_table_resource_path = $resource_path,
                public_table_public_url = $public_url,
                updated_at = $updated_at
            WHERE event_id = $event_id AND public_table_public_url IS NULL;
            """,
            {
                "$event_id": event_id,
                "$resource_path": resource_path,
                "$public_url": public_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def set_pass_table(self, event_id: str, resource_path: str, public_url: str) -> bool:
        event = await self.get(event_id)
        if event is None or event.pass_table_public_url is not None:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $resource_path AS Utf8;
            DECLARE $public_url AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET pass_table_resource_path = $resource_path,
                pass_table_public_url = $public_url,
                updated_at = $updated_at
            WHERE event_id = $event_id AND pass_table_public_url IS NULL;
            """,
            {
                "$event_id": event_id,
                "$resource_path": resource_path,
                "$public_url": public_url,
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
        statuses: Collection[EventStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]:
        if limit < 1 or limit > 10:
            raise ValueError("event page size must be between 1 and 10")
        conditions: list[str] = []
        params: dict[str, Any] = {"$limit": limit}
        declarations = ["DECLARE $limit AS Uint64;"]
        if statuses is not None:
            persisted_statuses = {status.value for status in statuses}
            if EventStatus.CONFIRMATION_OPEN in statuses:
                persisted_statuses.add("OPEN")
            status_conditions = []
            for index, value in enumerate(sorted(persisted_statuses)):
                parameter = f"$status_{index}"
                status_conditions.append(f"status = {parameter}")
                declarations.append(f"DECLARE {parameter} AS Utf8;")
                params[parameter] = value
            conditions.append(f"({' OR '.join(status_conditions)})" if status_conditions else "FALSE")
        if after is not None:
            conditions.append("(created_at > $after_time OR (created_at = $after_time AND event_id > $after_id))")
            declarations.extend(["DECLARE $after_time AS Timestamp;", "DECLARE $after_id AS Utf8;"])
            params.update({"$after_time": after[0], "$after_id": after[1]})
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            {" ".join(declarations)}
            SELECT {self.COLUMNS}
            FROM `events` {where}
            ORDER BY created_at ASC, event_id ASC
            LIMIT $limit;
        """
        rows = await self.db.query(query, params, read_only=True)
        return [self._from_row(row) for row in rows]

    async def list_pass_tables_page(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]:
        if limit < 1 or limit > 10:
            raise ValueError("event page size must be between 1 and 10")
        params: dict[str, Any] = {"$limit": limit}
        declarations = ["DECLARE $limit AS Uint64;"]
        after_condition = ""
        if after is not None:
            declarations.extend(["DECLARE $after_time AS Timestamp;", "DECLARE $after_id AS Utf8;"])
            params.update({"$after_time": after[0], "$after_id": after[1]})
            after_condition = "AND (created_at > $after_time OR (created_at = $after_time AND event_id > $after_id))"
        rows = await self.db.query(
            f"""
            {" ".join(declarations)}
            SELECT {self.COLUMNS}
            FROM `events`
            WHERE pass_table_public_url IS NOT NULL {after_condition}
            ORDER BY created_at ASC, event_id ASC
            LIMIT $limit;
            """,
            params,
            read_only=True,
        )
        return [self._from_row(row) for row in rows]


class YdbRegistrationRepository:
    COLUMNS = """
        event_id, participant_key, display_name, vk_profile, telegram_profile,
        wish_play, larp_experience,
        crossplay, character_wish, attendance_status, last_operation_id,
        created_at, updated_at
    """
    INSERT = """
        DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
        DECLARE $display_name AS Utf8; DECLARE $vk_profile AS Utf8;
        DECLARE $telegram_profile AS Optional<Utf8>; DECLARE $wish_play AS Utf8;
        DECLARE $larp_experience AS Optional<Bool>; DECLARE $crossplay AS Optional<Bool>;
        DECLARE $character_wish AS Utf8; DECLARE $attendance_status AS Utf8;
        DECLARE $last_operation_id AS Utf8; DECLARE $created_at AS Timestamp;
        DECLARE $updated_at AS Timestamp;
        INSERT INTO `registrations` (
            event_id, participant_key, display_name, vk_profile, telegram_profile,
            wish_play, larp_experience,
            crossplay, character_wish, attendance_status, last_operation_id,
            created_at, updated_at
        ) VALUES (
            $event_id, $participant_key, $display_name, $vk_profile, $telegram_profile,
            $wish_play, $larp_experience,
            $crossplay, $character_wish, $attendance_status, $last_operation_id,
            $created_at, $updated_at
        );
    """

    def __init__(self, executor: YdbExecutor) -> None:
        self.db = executor

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Registration:
        return Registration(
            event_id=row["event_id"],
            participant_key=row["participant_key"],
            display_name=row["display_name"],
            vk_profile=row.get("vk_profile") or "",
            telegram_profile=row.get("telegram_profile"),
            wish_play=row["wish_play"],
            larp_experience=row.get("larp_experience"),
            crossplay=row.get("crossplay"),
            character_wish=row.get("character_wish") or "",
            attendance_status=AttendanceStatus(row["attendance_status"]),
            last_operation_id=row.get("last_operation_id") or "",
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    async def get(self, event_id: str, participant_key: str) -> Registration | None:
        rows = await self.db.query(
            f"""
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            SELECT {self.COLUMNS} FROM `registrations`
            WHERE event_id = $event_id AND participant_key = $participant_key;
            """,
            {"$event_id": event_id, "$participant_key": participant_key},
            read_only=True,
        )
        return None if not rows else self._from_row(rows[0])

    async def list_for_event(self, event_id: str) -> Sequence[Registration]:
        rows = await self.db.query(
            f"""
            DECLARE $event_id AS Utf8;
            SELECT {self.COLUMNS} FROM `registrations`
            WHERE event_id = $event_id
            ORDER BY event_id, participant_key;
            """,
            {"$event_id": event_id},
            read_only=True,
        )
        registrations = [self._from_row(row) for row in rows]
        return sorted(registrations, key=lambda item: (item.created_at, item.participant_key))

    async def _save(self, registration: Registration) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $display_name AS Utf8; DECLARE $vk_profile AS Utf8;
            DECLARE $telegram_profile AS Optional<Utf8>; DECLARE $wish_play AS Utf8;
            DECLARE $larp_experience AS Optional<Bool>; DECLARE $crossplay AS Optional<Bool>;
            DECLARE $character_wish AS Utf8; DECLARE $attendance_status AS Utf8;
            DECLARE $last_operation_id AS Utf8; DECLARE $created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO `registrations` (
                event_id, participant_key, display_name, vk_profile, telegram_profile,
                wish_play, larp_experience,
                crossplay, character_wish, attendance_status, last_operation_id,
                created_at, updated_at
            ) VALUES (
                $event_id, $participant_key, $display_name, $vk_profile, $telegram_profile,
                $wish_play, $larp_experience,
                $crossplay, $character_wish, $attendance_status, $last_operation_id,
                $created_at, $updated_at
            );
            """,
            {
                "$event_id": registration.event_id,
                "$participant_key": registration.participant_key,
                "$display_name": registration.display_name,
                "$vk_profile": registration.vk_profile,
                "$telegram_profile": registration.telegram_profile,
                "$wish_play": registration.wish_play,
                "$larp_experience": registration.larp_experience,
                "$crossplay": registration.crossplay,
                "$character_wish": registration.character_wish,
                "$attendance_status": registration.attendance_status.value,
                "$last_operation_id": registration.last_operation_id,
                "$created_at": registration.created_at,
                "$updated_at": registration.updated_at,
            },
        )

    @staticmethod
    def _params(registration: Registration) -> dict[str, Any]:
        return {
            "$event_id": registration.event_id,
            "$participant_key": registration.participant_key,
            "$display_name": registration.display_name,
            "$vk_profile": registration.vk_profile,
            "$telegram_profile": registration.telegram_profile,
            "$wish_play": registration.wish_play,
            "$larp_experience": registration.larp_experience,
            "$crossplay": registration.crossplay,
            "$character_wish": registration.character_wish,
            "$attendance_status": registration.attendance_status.value,
            "$last_operation_id": registration.last_operation_id,
            "$created_at": registration.created_at,
            "$updated_at": registration.updated_at,
        }

    async def import_missing(self, registrations: Sequence[Registration]) -> None:
        for registration in registrations:
            await self.db.insert_if_absent(
                select_yql="""
                    DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
                    SELECT participant_key FROM `registrations`
                    WHERE event_id = $event_id AND participant_key = $participant_key;
                """,
                select_params={
                    "$event_id": registration.event_id,
                    "$participant_key": registration.participant_key,
                },
                insert_yql=self.INSERT,
                insert_params=self._params(registration),
            )

    async def enlist(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        wish_play: str,
        larp_experience: bool | None = None,
        crossplay: bool | None = None,
        vk_profile: str = "",
        telegram_profile: str | None = None,
    ) -> bool:
        registration = await self.get(event_id, participant_key)
        if registration is not None and registration.last_operation_id == operation_id:
            return False
        if registration is None:
            registration = Registration(
                event_id=event_id,
                participant_key=participant_key,
                display_name=display_name,
                wish_play=wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            )
        else:
            registration.display_name = display_name
            registration.wish_play = wish_play
            registration.larp_experience = larp_experience
            registration.crossplay = crossplay
            registration.vk_profile = vk_profile
            registration.telegram_profile = telegram_profile
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                registration.attendance_status = AttendanceStatus.WAITING
                registration.created_at = datetime.now(UTC)
        registration.last_operation_id = operation_id
        registration.updated_at = datetime.now(UTC)
        await self._save(registration)
        return True

    async def confirm(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $character_wish AS Utf8;
            DECLARE $attendance_status AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET character_wish = $character_wish,
                attendance_status = $attendance_status,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $participant_key
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_key": participant_key,
                "$operation_id": operation_id,
                "$character_wish": character_wish,
                "$attendance_status": AttendanceStatus.CONFIRMED.value,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def update_character_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $character_wish AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET character_wish = $character_wish,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $participant_key
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_key": participant_key,
                "$operation_id": operation_id,
                "$character_wish": character_wish,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def cancel(self, event_id: str, *, operation_id: str, participant_key: str) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $attendance_status AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET attendance_status = $attendance_status,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $participant_key
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_key": participant_key,
                "$operation_id": operation_id,
                "$attendance_status": AttendanceStatus.CANCELLED.value,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def delete_for_event(self, event_id: str) -> None:
        await self.db.query(
            "DECLARE $event_id AS Utf8; DELETE FROM `registrations` WHERE event_id = $event_id;",
            {"$event_id": event_id},
        )
