from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar, cast

import ydb
from pydantic import ValidationError

from larp_bot.domain.models import (
    DEFAULT_PLAYER_AMOUNT,
    MAX_EVENT_PAGE_SIZE,
    MAX_PLAYER_AMOUNT,
    AttendanceStatus,
    BotIdentity,
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


class YdbUserRepository:
    COMMON_COLUMNS = """
        full_name, nickname, crossplay, larp_experience, needs_pass, pass_details_json,
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
        platform_columns = (
            f"{contact_column}, linked_vk_id, telegram_handle" if platform is Platform.TELEGRAM else contact_column
        )
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
            "nickname": row.get("nickname"),
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
                    "linked_vk_id": row.get("linked_vk_id"),
                    "telegram_handle": row.get("telegram_handle"),
                    **common,
                }
            )
        return VkUser.model_validate({"vk_id": user_id, "telegram_handle": row.get("telegram_handle"), **common})

    async def find_linked(self, user: User) -> User | None:
        if isinstance(user, TelegramUser):
            if user.linked_vk_id is None or user.telegram_handle is None:
                return None
            owners = await self.db.query(
                """
                DECLARE $vk_id AS Uint64;
                DECLARE $handle AS Utf8;
                SELECT tg_id FROM `tg_users`
                WHERE linked_vk_id = $vk_id AND telegram_handle = $handle
                LIMIT 2;
                """,
                {"$vk_id": user.linked_vk_id, "$handle": user.telegram_handle},
                read_only=True,
            )
            if len(owners) != 1 or int(owners[0]["tg_id"]) != user.tg_id:
                return None
            linked = await self.get(Platform.VK, user.linked_vk_id)
            return linked if linked is not None and linked.telegram_handle == user.telegram_handle else None
        if user.telegram_handle is None:
            return None
        rows = await self.db.query(
            f"""
            DECLARE $vk_id AS Uint64;
            DECLARE $handle AS Utf8;
            SELECT tg_id, vk_url, linked_vk_id, telegram_handle, {self.COMMON_COLUMNS}
            FROM `tg_users`
            WHERE linked_vk_id = $vk_id AND telegram_handle = $handle
            LIMIT 2;
            """,
            {"$vk_id": user.vk_id, "$handle": user.telegram_handle},
            read_only=True,
        )
        if len(rows) != 1:
            return None
        return self._from_row(Platform.TELEGRAM, int(rows[0]["tg_id"]), rows[0])

    async def find_telegram_by_handle(self, handle: str) -> TelegramUser | None:
        rows = await self.db.query(
            f"""
            DECLARE $handle AS Utf8;
            SELECT tg_id, vk_url, linked_vk_id, telegram_handle, {self.COMMON_COLUMNS}
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

    async def update_telegram_handle(self, user_id: int, handle: str) -> None:
        await self.db.query(
            """
            DECLARE $user_id AS Uint64;
            DECLARE $handle AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `tg_users`
            SET telegram_handle = $handle, updated_at = $updated_at
            WHERE tg_id = $user_id;
            """,
            {"$user_id": user_id, "$handle": handle, "$updated_at": datetime.now(UTC)},
        )

    async def update_telegram_link(
        self,
        user_id: int,
        handle: str | None,
        linked_vk_id: int | None,
    ) -> None:
        await self.db.query(
            """
            DECLARE $user_id AS Uint64;
            DECLARE $handle AS Optional<Utf8>;
            DECLARE $linked_vk_id AS Optional<Uint64>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `tg_users`
            SET telegram_handle = $handle,
                linked_vk_id = $linked_vk_id,
                updated_at = $updated_at
            WHERE tg_id = $user_id;
            """,
            {
                "$user_id": user_id,
                "$handle": handle,
                "$linked_vk_id": linked_vk_id,
                "$updated_at": datetime.now(UTC),
            },
        )

    async def list_all(self) -> Sequence[User]:
        telegram_rows, vk_rows = await asyncio.gather(
            self.db.query(
                f"SELECT tg_id, vk_url, linked_vk_id, telegram_handle, {self.COMMON_COLUMNS} FROM `tg_users`;",
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
            contact_declarations = (
                "DECLARE $vk_url AS Optional<Utf8>; DECLARE $linked_vk_id AS Optional<Uint64>; "
                "DECLARE $telegram_handle AS Optional<Utf8>;"
            )
            contact_columns = "vk_url, linked_vk_id, telegram_handle,"
            contact_values = "$vk_url, $linked_vk_id, $telegram_handle,"
            contact_params = {
                "$vk_url": user.vk_url,
                "$linked_vk_id": user.linked_vk_id,
                "$telegram_handle": user.telegram_handle,
            }
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
            DECLARE $nickname AS Optional<Utf8>;
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
                {id_column}, {contact_columns} full_name, nickname, crossplay, larp_experience,
                needs_pass, pass_details_json, dialog_state, dialog_context_json,
                last_update_id, last_update_at, last_delivery_operation_id, last_bot_buttons_json,
                created_at, updated_at
            ) VALUES (
                $user_id, {contact_values} $full_name, $nickname, $crossplay, $larp_experience,
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
                "$nickname": user.nickname,
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

    async def grant_larp_experience(self, platform: Platform, user_id: int) -> None:
        table, id_column, _ = self._location(platform)
        await self.db.query(
            f"""
            DECLARE $user_id AS Uint64;
            DECLARE $updated_at AS Timestamp;
            UPDATE `{table}`
            SET larp_experience = TRUE, updated_at = $updated_at
            WHERE {id_column} = $user_id;
            """,
            {"$user_id": user_id, "$updated_at": datetime.now(UTC)},
        )

    async def set_larp_experience(self, platform: Platform, user_id: int, experienced: bool) -> None:
        table, id_column, _ = self._location(platform)
        await self.db.query(
            f"""
            DECLARE $user_id AS Uint64;
            DECLARE $experienced AS Bool;
            DECLARE $updated_at AS Timestamp;
            UPDATE `{table}`
            SET larp_experience = $experienced, updated_at = $updated_at
            WHERE {id_column} = $user_id;
            """,
            {
                "$user_id": user_id,
                "$experienced": experienced,
                "$updated_at": datetime.now(UTC),
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

    async def revoke_gamemaster(self, platform: Platform, user_id: int, operation_id: str) -> None:
        table, id_column, _ = self._location(platform)
        await self.db.query(
            f"""
            DECLARE $user_id AS Uint64;
            DECLARE $operation_id AS Utf8;
            UPDATE `{table}`
            SET is_gamemaster = FALSE, gamemaster_grant_operation_id = $operation_id
            WHERE {id_column} = $user_id AND is_gamemaster = TRUE;
            """,
            {"$user_id": user_id, "$operation_id": operation_id},
        )

    async def claim_delivery(
        self,
        platform: Platform,
        user_id: int,
        operation_id: str,
        buttons: Sequence[Button] = (),
    ) -> bool:
        table, id_column, _ = self._location(platform)
        return await self.db.claim_delivery(
            table=table,
            id_column=id_column,
            user_id=user_id,
            operation_id=operation_id,
            buttons=buttons,
        )


class YdbEventRepository:
    COLUMNS = """
        event_id, name, game_date, player_amount, signup_token, disk_resource_path, public_registration_url,
        telegram_post_url, vk_post_url,
        public_table_resource_path, public_table_public_url,
        status, registration_open_at, registration_open_notified_recipients_json,
        registration_open_notification_completed_at,
        confirmation_deadline, registrations_migrated_at,
        pass_table_resource_path, pass_table_public_url,
        confirmed_notifications_json, last_confirmed_notification_operation_id,
        last_reserve_promotion_operation_id, last_reserve_promotion_participant_key,
        last_reserve_promotion_delivered_operation_id,
        last_confirmed_link_promotion_operation_id, last_confirmed_link_promotion_participant_key,
        last_confirmed_link_promotion_delivered_operation_id,
        last_participant_removal_operation_id, participant_removal_keys_json,
        participant_removal_notified_keys_json,
        archived_at,
        created_at, updated_at
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
            game_date=_date(row["game_date"]) if row.get("game_date") is not None else None,
            player_amount=row.get("player_amount") or DEFAULT_PLAYER_AMOUNT,
            signup_token=row.get("signup_token"),
            telegram_post_url=row.get("telegram_post_url"),
            vk_post_url=row.get("vk_post_url"),
            disk_resource_path=row["disk_resource_path"],
            public_registration_url=row["public_registration_url"],
            public_table_resource_path=row.get("public_table_resource_path"),
            public_table_public_url=row.get("public_table_public_url"),
            status=status,
            registration_open_at=(
                _dt(row["registration_open_at"]) if row.get("registration_open_at") is not None else None
            ),
            registration_open_notified_recipients=_string_list(
                row.get("registration_open_notified_recipients_json"),
                "registration_open_notified_recipients_json",
            ),
            registration_open_notification_completed_at=(
                _dt(row["registration_open_notification_completed_at"])
                if row.get("registration_open_notification_completed_at") is not None
                else None
            ),
            confirmation_deadline=(
                _dt(row["confirmation_deadline"]) if row.get("confirmation_deadline") is not None else None
            ),
            registrations_migrated_at=(
                _dt(row["registrations_migrated_at"]) if row.get("registrations_migrated_at") is not None else None
            ),
            pass_table_resource_path=row.get("pass_table_resource_path"),
            pass_table_public_url=row.get("pass_table_public_url"),
            confirmed_notifications=_confirmed_notifications(row.get("confirmed_notifications_json")),
            last_confirmed_notification_operation_id=row.get("last_confirmed_notification_operation_id"),
            last_reserve_promotion_operation_id=row.get("last_reserve_promotion_operation_id"),
            last_reserve_promotion_participant_key=row.get("last_reserve_promotion_participant_key"),
            last_reserve_promotion_delivered_operation_id=row.get("last_reserve_promotion_delivered_operation_id"),
            last_confirmed_link_promotion_operation_id=row.get("last_confirmed_link_promotion_operation_id"),
            last_confirmed_link_promotion_participant_key=row.get("last_confirmed_link_promotion_participant_key"),
            last_confirmed_link_promotion_delivered_operation_id=row.get(
                "last_confirmed_link_promotion_delivered_operation_id"
            ),
            last_participant_removal_operation_id=row.get("last_participant_removal_operation_id"),
            participant_removal_keys=_string_list(
                row.get("participant_removal_keys_json"),
                "participant_removal_keys_json",
            ),
            participant_removal_notified_keys=_string_list(
                row.get("participant_removal_notified_keys_json"),
                "participant_removal_notified_keys_json",
            ),
            archived_at=_dt(row["archived_at"]) if row.get("archived_at") is not None else None,
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

    async def get_by_signup_token(self, signup_token: str) -> Event | None:
        rows = await self.db.query(
            f"""
            DECLARE $signup_token AS Utf8;
            SELECT {self.COLUMNS}
            FROM `events` WHERE signup_token = $signup_token
            LIMIT 2;
            """,
            {"$signup_token": signup_token},
            read_only=True,
        )
        if len(rows) > 1:
            raise RuntimeError("duplicate game signup token")
        return None if not rows else self._from_row(rows[0])

    async def create(self, event: Event, leaders: Sequence[BotIdentity] = ()) -> None:
        unique_leaders = sorted(
            {(leader.platform, leader.platform_user_id) for leader in leaders},
            key=lambda item: (item[0].value, item[1]),
        )
        leader_declarations = []
        leader_upserts = []
        leader_params: dict[str, Any] = {}
        for index, (platform, platform_user_id) in enumerate(unique_leaders):
            leader_declarations.extend(
                [
                    f"DECLARE $leader_platform_{index} AS Utf8;",
                    f"DECLARE $leader_user_id_{index} AS Uint64;",
                ]
            )
            leader_upserts.append(
                f"""
                UPSERT INTO `event_leaders` (event_id, platform, platform_user_id, created_at)
                VALUES ($event_id, $leader_platform_{index}, $leader_user_id_{index}, $created_at);
                """
            )
            leader_params.update(
                {
                    f"$leader_platform_{index}": platform.value,
                    f"$leader_user_id_{index}": platform_user_id,
                }
            )
        await self.db.query(
            f"""
            DECLARE $event_id AS Utf8; DECLARE $name AS Utf8; DECLARE $game_date AS Optional<Date>;
            DECLARE $player_amount AS Uint64; DECLARE $signup_token AS Optional<Utf8>;
            DECLARE $telegram_post_url AS Optional<Utf8>; DECLARE $vk_post_url AS Optional<Utf8>;
            DECLARE $disk_resource_path AS Utf8; DECLARE $public_url AS Utf8;
            DECLARE $public_table_resource_path AS Optional<Utf8>;
            DECLARE $public_table_public_url AS Optional<Utf8>;
            DECLARE $status AS Utf8; DECLARE $created_at AS Timestamp;
            DECLARE $registration_open_at AS Optional<Timestamp>;
            DECLARE $registration_open_notified_recipients_json AS Utf8;
            DECLARE $registration_open_notification_completed_at AS Optional<Timestamp>;
            DECLARE $confirmation_deadline AS Optional<Timestamp>;
            DECLARE $registrations_migrated_at AS Timestamp;
            DECLARE $pass_table_resource_path AS Optional<Utf8>;
            DECLARE $pass_table_public_url AS Optional<Utf8>;
            DECLARE $confirmed_notifications_json AS Utf8;
            DECLARE $last_confirmed_notification_operation_id AS Optional<Utf8>;
            DECLARE $last_reserve_promotion_operation_id AS Optional<Utf8>;
            DECLARE $last_reserve_promotion_participant_key AS Optional<Utf8>;
            DECLARE $last_reserve_promotion_delivered_operation_id AS Optional<Utf8>;
            DECLARE $last_confirmed_link_promotion_operation_id AS Optional<Utf8>;
            DECLARE $last_confirmed_link_promotion_participant_key AS Optional<Utf8>;
            DECLARE $last_confirmed_link_promotion_delivered_operation_id AS Optional<Utf8>;
            DECLARE $last_participant_removal_operation_id AS Optional<Utf8>;
            DECLARE $participant_removal_keys_json AS Utf8;
            DECLARE $participant_removal_notified_keys_json AS Utf8;
            DECLARE $archived_at AS Optional<Timestamp>;
            DECLARE $updated_at AS Timestamp;
            {" ".join(leader_declarations)}
            INSERT INTO `events` (
                event_id, name, game_date, player_amount, signup_token, disk_resource_path, public_registration_url,
                telegram_post_url, vk_post_url,
                public_table_resource_path, public_table_public_url,
                status, registration_open_at, registration_open_notified_recipients_json,
                registration_open_notification_completed_at,
                confirmation_deadline, registrations_migrated_at,
                pass_table_resource_path, pass_table_public_url,
                confirmed_notifications_json, last_confirmed_notification_operation_id,
                last_reserve_promotion_operation_id, last_reserve_promotion_participant_key,
                last_reserve_promotion_delivered_operation_id,
                last_confirmed_link_promotion_operation_id, last_confirmed_link_promotion_participant_key,
                last_confirmed_link_promotion_delivered_operation_id,
                last_participant_removal_operation_id, participant_removal_keys_json,
                participant_removal_notified_keys_json,
                archived_at,
                created_at, updated_at
            ) VALUES (
                $event_id, $name, $game_date, $player_amount, $signup_token, $disk_resource_path, $public_url,
                $telegram_post_url, $vk_post_url,
                $public_table_resource_path, $public_table_public_url,
                $status, $registration_open_at, $registration_open_notified_recipients_json,
                $registration_open_notification_completed_at,
                $confirmation_deadline, $registrations_migrated_at,
                $pass_table_resource_path, $pass_table_public_url,
                $confirmed_notifications_json, $last_confirmed_notification_operation_id,
                $last_reserve_promotion_operation_id, $last_reserve_promotion_participant_key,
                $last_reserve_promotion_delivered_operation_id,
                $last_confirmed_link_promotion_operation_id, $last_confirmed_link_promotion_participant_key,
                $last_confirmed_link_promotion_delivered_operation_id,
                $last_participant_removal_operation_id, $participant_removal_keys_json,
                $participant_removal_notified_keys_json,
                $archived_at,
                $created_at, $updated_at
            );
            {" ".join(leader_upserts)}
            """,
            {
                "$event_id": event.event_id,
                "$name": event.name,
                "$game_date": event.game_date,
                "$player_amount": event.player_amount,
                "$signup_token": event.signup_token,
                "$telegram_post_url": event.telegram_post_url,
                "$vk_post_url": event.vk_post_url,
                "$disk_resource_path": event.disk_resource_path,
                "$public_url": event.public_registration_url,
                "$public_table_resource_path": event.public_table_resource_path,
                "$public_table_public_url": event.public_table_public_url,
                "$status": event.status.value,
                "$registration_open_at": event.registration_open_at,
                "$registration_open_notified_recipients_json": json.dumps(
                    event.registration_open_notified_recipients,
                    separators=(",", ":"),
                ),
                "$registration_open_notification_completed_at": (event.registration_open_notification_completed_at),
                "$confirmation_deadline": event.confirmation_deadline,
                "$registrations_migrated_at": event.registrations_migrated_at,
                "$pass_table_resource_path": event.pass_table_resource_path,
                "$pass_table_public_url": event.pass_table_public_url,
                "$confirmed_notifications_json": json.dumps(
                    event.confirmed_notifications,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "$last_confirmed_notification_operation_id": event.last_confirmed_notification_operation_id,
                "$last_reserve_promotion_operation_id": event.last_reserve_promotion_operation_id,
                "$last_reserve_promotion_participant_key": event.last_reserve_promotion_participant_key,
                "$last_reserve_promotion_delivered_operation_id": (event.last_reserve_promotion_delivered_operation_id),
                "$last_confirmed_link_promotion_operation_id": event.last_confirmed_link_promotion_operation_id,
                "$last_confirmed_link_promotion_participant_key": event.last_confirmed_link_promotion_participant_key,
                "$last_confirmed_link_promotion_delivered_operation_id": (
                    event.last_confirmed_link_promotion_delivered_operation_id
                ),
                "$last_participant_removal_operation_id": event.last_participant_removal_operation_id,
                "$participant_removal_keys_json": json.dumps(
                    event.participant_removal_keys,
                    separators=(",", ":"),
                ),
                "$participant_removal_notified_keys_json": json.dumps(
                    event.participant_removal_notified_keys,
                    separators=(",", ":"),
                ),
                "$archived_at": event.archived_at,
                "$created_at": event.created_at,
                "$updated_at": event.updated_at,
                **leader_params,
            },
        )

    async def add_leader(self, event_id: str, leader: BotIdentity) -> bool:
        if await self.get(event_id) is None:
            return False
        params = {
            "$event_id": event_id,
            "$platform": leader.platform.value,
            "$platform_user_id": leader.platform_user_id,
        }
        return await self.db.insert_if_absent(
            select_yql="""
                DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8;
                DECLARE $platform_user_id AS Uint64;
                SELECT event_id FROM `event_leaders`
                WHERE event_id = $event_id AND platform = $platform
                  AND platform_user_id = $platform_user_id;
            """,
            select_params=params,
            insert_yql="""
                DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8;
                DECLARE $platform_user_id AS Uint64; DECLARE $created_at AS Timestamp;
                INSERT INTO `event_leaders` (event_id, platform, platform_user_id, created_at)
                VALUES ($event_id, $platform, $platform_user_id, $created_at);
            """,
            insert_params=params | {"$created_at": datetime.now(UTC)},
        )

    async def remove_leader(self, event_id: str, leader: BotIdentity) -> bool:
        if await self.get(event_id) is None:
            return False
        params = {
            "$event_id": event_id,
            "$platform": leader.platform.value,
            "$platform_user_id": leader.platform_user_id,
        }
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8;
            DECLARE $platform_user_id AS Uint64;
            SELECT event_id FROM `event_leaders`
            WHERE event_id = $event_id AND platform = $platform
              AND platform_user_id = $platform_user_id;
            """,
            params,
            read_only=True,
        )
        if not rows:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8;
            DECLARE $platform_user_id AS Uint64;
            DELETE FROM `event_leaders`
            WHERE event_id = $event_id AND platform = $platform
              AND platform_user_id = $platform_user_id;
            """,
            params,
        )
        return True

    async def list_leaders(self, event_id: str) -> Sequence[BotIdentity]:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8;
            SELECT platform, platform_user_id FROM `event_leaders`
            WHERE event_id = $event_id
            ORDER BY platform, platform_user_id;
            """,
            {"$event_id": event_id},
            read_only=True,
        )
        return [
            BotIdentity(platform=Platform(str(row["platform"])), platform_user_id=int(row["platform_user_id"]))
            for row in rows
        ]

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

    async def list_due_registration_notifications(
        self,
        opened_by: datetime,
        *,
        limit: int = 100,
    ) -> Sequence[Event]:
        rows = await self.db.query(
            f"""
            DECLARE $opened_by AS Timestamp; DECLARE $closed_status AS Utf8;
            DECLARE $limit AS Uint64;
            SELECT {self.COLUMNS}
            FROM `events`
            WHERE registration_open_at IS NOT NULL
              AND registration_open_at <= $opened_by
              AND registration_open_notification_completed_at IS NULL
              AND archived_at IS NULL
              AND status != $closed_status
            ORDER BY registration_open_at, event_id
            LIMIT $limit;
            """,
            {
                "$opened_by": opened_by.astimezone(UTC),
                "$closed_status": EventStatus.CLOSED.value,
                "$limit": limit,
            },
            read_only=True,
        )
        return [self._from_row(row) for row in rows]

    async def mark_registration_open_notification_delivered(self, event_id: str, recipient: str) -> None:
        event = await self.get(event_id)
        if event is None or recipient in event.registration_open_notified_recipients:
            return
        notified_recipients = [*event.registration_open_notified_recipients, recipient]
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $notified_recipients_json AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET registration_open_notified_recipients_json = $notified_recipients_json,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND registration_open_notification_completed_at IS NULL;
            """,
            {
                "$event_id": event_id,
                "$notified_recipients_json": json.dumps(notified_recipients, separators=(",", ":")),
                "$updated_at": datetime.now(UTC),
            },
        )

    async def complete_registration_open_notification(self, event_id: str, completed_at: datetime) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $completed_at AS Timestamp;
            UPDATE `events`
            SET registration_open_notification_completed_at = $completed_at,
                updated_at = $completed_at
            WHERE event_id = $event_id
              AND registration_open_notification_completed_at IS NULL;
            """,
            {"$event_id": event_id, "$completed_at": completed_at.astimezone(UTC)},
        )

    async def set_player_amount(self, event_id: str, player_amount: int) -> bool:
        if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
            raise ValueError(f"player_amount must be between 1 and {MAX_PLAYER_AMOUNT}")
        event = await self.get(event_id)
        if event is None or event.player_amount == player_amount:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $player_amount AS Uint64;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events` SET player_amount = $player_amount, updated_at = $updated_at
            WHERE event_id = $event_id;
            """,
            {
                "$event_id": event_id,
                "$player_amount": player_amount,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def set_post_links(self, event_id: str, telegram_post_url: str, vk_post_url: str) -> bool:
        event = await self.get(event_id)
        if event is None:
            return False
        if event.telegram_post_url == telegram_post_url and event.vk_post_url == vk_post_url:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $telegram_post_url AS Utf8;
            DECLARE $vk_post_url AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET telegram_post_url = $telegram_post_url,
                vk_post_url = $vk_post_url,
                updated_at = $updated_at
            WHERE event_id = $event_id;
            """,
            {
                "$event_id": event_id,
                "$telegram_post_url": telegram_post_url,
                "$vk_post_url": vk_post_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def archive(self, event_id: str, archived_at: datetime) -> bool:
        event = await self.get(event_id)
        if event is None or event.archived_at is not None:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $archived_at AS Timestamp;
            UPDATE `events`
            SET status = $status, archived_at = $archived_at, updated_at = $archived_at
            WHERE event_id = $event_id AND archived_at IS NULL;
            """,
            {
                "$event_id": event_id,
                "$status": EventStatus.CLOSED.value,
                "$archived_at": archived_at.astimezone(UTC),
            },
        )
        return True

    async def unarchive(self, event_id: str, updated_at: datetime) -> bool:
        event = await self.get(event_id)
        if event is None or event.archived_at is None:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $archived_at AS Optional<Timestamp>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET archived_at = $archived_at, updated_at = $updated_at
            WHERE event_id = $event_id AND archived_at IS NOT NULL;
            """,
            {
                "$event_id": event_id,
                "$archived_at": None,
                "$updated_at": updated_at.astimezone(UTC),
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

    async def append_confirmed_notification(self, event_id: str, text: str, operation_id: str) -> bool:
        event = await self.get(event_id)
        if event is None or event.last_confirmed_notification_operation_id == operation_id:
            return False
        notifications = [*event.confirmed_notifications, text]
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $notifications_json AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET confirmed_notifications_json = $notifications_json,
                last_confirmed_notification_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND (
                last_confirmed_notification_operation_id IS NULL
                OR last_confirmed_notification_operation_id != $operation_id
              );
            """,
            {
                "$event_id": event_id,
                "$notifications_json": json.dumps(notifications, ensure_ascii=False, separators=(",", ":")),
                "$operation_id": operation_id,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def set_reserve_promotion(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str | None,
    ) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $participant_key AS Optional<Utf8>; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET last_reserve_promotion_operation_id = $operation_id,
                last_reserve_promotion_participant_key = $participant_key,
                updated_at = $updated_at
            WHERE event_id = $event_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$participant_key": participant_key,
                "$updated_at": datetime.now(UTC),
            },
        )

    async def mark_reserve_promotion_delivered(self, event_id: str, operation_id: str) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET last_reserve_promotion_delivered_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND last_reserve_promotion_operation_id = $operation_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$updated_at": datetime.now(UTC),
            },
        )

    async def set_confirmed_link_promotion(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str | None,
    ) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $participant_key AS Optional<Utf8>; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET last_confirmed_link_promotion_operation_id = $operation_id,
                last_confirmed_link_promotion_participant_key = $participant_key,
                updated_at = $updated_at
            WHERE event_id = $event_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$participant_key": participant_key,
                "$updated_at": datetime.now(UTC),
            },
        )

    async def mark_confirmed_link_promotion_delivered(self, event_id: str, operation_id: str) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET last_confirmed_link_promotion_delivered_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND last_confirmed_link_promotion_operation_id = $operation_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$updated_at": datetime.now(UTC),
            },
        )

    async def set_participant_removal(
        self,
        event_id: str,
        operation_id: str,
        participant_keys: Sequence[str],
    ) -> None:
        event = await self.get(event_id)
        if event is None or event.last_participant_removal_operation_id == operation_id:
            return
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $participant_keys_json AS Utf8; DECLARE $notified_keys_json AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET last_participant_removal_operation_id = $operation_id,
                participant_removal_keys_json = $participant_keys_json,
                participant_removal_notified_keys_json = $notified_keys_json,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND (
                last_participant_removal_operation_id IS NULL
                OR last_participant_removal_operation_id != $operation_id
              );
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$participant_keys_json": json.dumps(list(participant_keys), separators=(",", ":")),
                "$notified_keys_json": "[]",
                "$updated_at": datetime.now(UTC),
            },
        )

    async def add_participant_removal_keys(
        self,
        event_id: str,
        operation_id: str,
        participant_keys: Sequence[str],
    ) -> None:
        event = await self.get(event_id)
        if event is None or event.last_participant_removal_operation_id != operation_id:
            return
        added = [key for key in dict.fromkeys(participant_keys) if key not in event.participant_removal_keys]
        if not added:
            return
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $participant_keys_json AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET participant_removal_keys_json = $participant_keys_json,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND last_participant_removal_operation_id = $operation_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$participant_keys_json": json.dumps(
                    [*event.participant_removal_keys, *added],
                    separators=(",", ":"),
                ),
                "$updated_at": datetime.now(UTC),
            },
        )

    async def mark_participant_removal_delivered(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str,
    ) -> None:
        event = await self.get(event_id)
        if (
            event is None
            or event.last_participant_removal_operation_id != operation_id
            or participant_key in event.participant_removal_notified_keys
        ):
            return
        notified_keys = [*event.participant_removal_notified_keys, participant_key]
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $notified_keys_json AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET participant_removal_notified_keys_json = $notified_keys_json,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND last_participant_removal_operation_id = $operation_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$notified_keys_json": json.dumps(notified_keys, separators=(",", ":")),
                "$updated_at": datetime.now(UTC),
            },
        )

    async def delete(self, event_id: str) -> bool:
        exists = await self.get(event_id)
        if exists is None:
            return False
        await self.db.query(
            """
            DECLARE $event_id AS Utf8;
            DELETE FROM `event_leaders` WHERE event_id = $event_id;
            DELETE FROM `events` WHERE event_id = $event_id;
            """,
            {"$event_id": event_id},
        )
        return True

    async def list_page(
        self,
        *,
        statuses: Collection[EventStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        archived: bool | None = None,
        registration_opened: bool | None = None,
        newest_first: bool = False,
        limit: int = 10,
    ) -> Sequence[Event]:
        if limit < 1 or limit > MAX_EVENT_PAGE_SIZE:
            raise ValueError(f"event page size must be between 1 and {MAX_EVENT_PAGE_SIZE}")
        if after is not None and before is not None:
            raise ValueError("only one event-page cursor may be supplied")
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
            operator = "<" if newest_first else ">"
            conditions.append(
                f"(created_at {operator} $after_time OR (created_at = $after_time AND event_id {operator} $after_id))"
            )
            declarations.extend(["DECLARE $after_time AS Timestamp;", "DECLARE $after_id AS Utf8;"])
            params.update({"$after_time": after[0], "$after_id": after[1]})
        if before is not None:
            operator = ">" if newest_first else "<"
            conditions.append(
                f"(created_at {operator} $before_time OR "
                f"(created_at = $before_time AND event_id {operator} $before_id))"
            )
            declarations.extend(["DECLARE $before_time AS Timestamp;", "DECLARE $before_id AS Utf8;"])
            params.update({"$before_time": before[0], "$before_id": before[1]})
        if archived is not None:
            conditions.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
        if registration_opened is not None:
            declarations.append("DECLARE $registration_now AS Timestamp;")
            params["$registration_now"] = datetime.now(UTC)
            conditions.append(
                "(registration_open_at IS NULL OR registration_open_at <= $registration_now)"
                if registration_opened
                else "registration_open_at > $registration_now"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order = "DESC" if newest_first else "ASC"
        if before is not None:
            order = "ASC" if newest_first else "DESC"
        query = f"""
            {" ".join(declarations)}
            SELECT {self.COLUMNS}
            FROM `events` {where}
            ORDER BY created_at {order}, event_id {order}
            LIMIT $limit;
        """
        rows = await self.db.query(query, params, read_only=True)
        if before is not None:
            rows.reverse()
        return [self._from_row(row) for row in rows]


class YdbRegistrationRepository:
    COLUMNS = """
        event_id, participant_key, display_name, nickname, vk_profile, telegram_profile,
        wish_play, larp_experience,
        crossplay, character_wish, attendance_status, last_operation_id,
        created_at, updated_at
    """
    INSERT = """
        DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
        DECLARE $display_name AS Utf8; DECLARE $vk_profile AS Utf8;
        DECLARE $nickname AS Optional<Utf8>;
        DECLARE $telegram_profile AS Optional<Utf8>; DECLARE $wish_play AS Utf8;
        DECLARE $larp_experience AS Optional<Bool>; DECLARE $crossplay AS Optional<Bool>;
        DECLARE $character_wish AS Utf8; DECLARE $attendance_status AS Utf8;
        DECLARE $last_operation_id AS Utf8; DECLARE $created_at AS Timestamp;
        DECLARE $updated_at AS Timestamp;
        INSERT INTO `registrations` (
            event_id, participant_key, display_name, nickname, vk_profile, telegram_profile,
            wish_play, larp_experience,
            crossplay, character_wish, attendance_status, last_operation_id,
            created_at, updated_at
        ) VALUES (
            $event_id, $participant_key, $display_name, $nickname, $vk_profile, $telegram_profile,
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
            nickname=row.get("nickname"),
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

    async def find_by_telegram_profile(self, profile: str) -> Sequence[Registration]:
        rows = await self.db.query(
            f"""
            DECLARE $profile AS Utf8;
            SELECT {self.COLUMNS} FROM `registrations`
            WHERE telegram_profile = $profile
            ORDER BY event_id, participant_key;
            """,
            {"$profile": profile},
            read_only=True,
        )
        return [self._from_row(row) for row in rows]

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

    async def list_page_for_event(
        self,
        event_id: str,
        *,
        statuses: Collection[AttendanceStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Registration]:
        if after is not None and before is not None:
            raise ValueError("only one registration-page cursor may be supplied")
        declarations = ["DECLARE $event_id AS Utf8;", "DECLARE $limit AS Uint64;"]
        conditions = ["event_id = $event_id"]
        params: dict[str, Any] = {"$event_id": event_id, "$limit": limit}
        if statuses is not None:
            accepted = sorted(set(statuses), key=lambda status: status.value)
            if not accepted:
                return []
            status_parameters: list[str] = []
            for index, status in enumerate(accepted):
                name = f"$status_{index}"
                declarations.append(f"DECLARE {name} AS Utf8;")
                params[name] = status.value
                status_parameters.append(name)
            conditions.append(f"attendance_status IN ({', '.join(status_parameters)})")
        if after is not None:
            declarations.extend(["DECLARE $after_time AS Timestamp;", "DECLARE $after_key AS Utf8;"])
            conditions.append(
                "(created_at > $after_time OR (created_at = $after_time AND participant_key > $after_key))"
            )
            params.update({"$after_time": after[0], "$after_key": after[1]})
        if before is not None:
            declarations.extend(["DECLARE $before_time AS Timestamp;", "DECLARE $before_key AS Utf8;"])
            conditions.append(
                "(created_at < $before_time OR (created_at = $before_time AND participant_key < $before_key))"
            )
            params.update({"$before_time": before[0], "$before_key": before[1]})
        order = "DESC" if before is not None else "ASC"
        rows = await self.db.query(
            f"""
            {" ".join(declarations)}
            SELECT {self.COLUMNS} FROM `registrations`
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at {order}, participant_key {order}
            LIMIT $limit;
            """,
            params,
            read_only=True,
        )
        registrations = [self._from_row(row) for row in rows]
        if before is not None:
            registrations.reverse()
        return registrations

    async def _save(self, registration: Registration) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $display_name AS Utf8; DECLARE $vk_profile AS Utf8;
            DECLARE $nickname AS Optional<Utf8>;
            DECLARE $telegram_profile AS Optional<Utf8>; DECLARE $wish_play AS Utf8;
            DECLARE $larp_experience AS Optional<Bool>; DECLARE $crossplay AS Optional<Bool>;
            DECLARE $character_wish AS Utf8; DECLARE $attendance_status AS Utf8;
            DECLARE $last_operation_id AS Utf8; DECLARE $created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO `registrations` (
                event_id, participant_key, display_name, nickname, vk_profile, telegram_profile,
                wish_play, larp_experience,
                crossplay, character_wish, attendance_status, last_operation_id,
                created_at, updated_at
            ) VALUES (
                $event_id, $participant_key, $display_name, $nickname, $vk_profile, $telegram_profile,
                $wish_play, $larp_experience,
                $crossplay, $character_wish, $attendance_status, $last_operation_id,
                $created_at, $updated_at
            );
            """,
            {
                "$event_id": registration.event_id,
                "$participant_key": registration.participant_key,
                "$display_name": registration.display_name,
                "$nickname": registration.nickname,
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
            "$nickname": registration.nickname,
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
        nickname: str | None = None,
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
                nickname=nickname,
                wish_play=wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            )
        else:
            registration.display_name = display_name
            registration.nickname = nickname
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

    async def update_profile(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        nickname: str | None,
        larp_experience: bool,
        crossplay: bool,
        vk_profile: str,
        telegram_profile: str | None,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $display_name AS Utf8;
            DECLARE $nickname AS Optional<Utf8>;
            DECLARE $larp_experience AS Bool; DECLARE $crossplay AS Bool;
            DECLARE $vk_profile AS Utf8; DECLARE $telegram_profile AS Optional<Utf8>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET display_name = $display_name,
                nickname = $nickname,
                larp_experience = $larp_experience,
                crossplay = $crossplay,
                vk_profile = $vk_profile,
                telegram_profile = $telegram_profile,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $participant_key
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_key": participant_key,
                "$operation_id": operation_id,
                "$display_name": display_name,
                "$nickname": nickname,
                "$larp_experience": larp_experience,
                "$crossplay": crossplay,
                "$vk_profile": vk_profile,
                "$telegram_profile": telegram_profile,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def confirm(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
        replaced_event_id: str | None = None,
        replaced_participant_key: str | None = None,
    ) -> bool:
        if (replaced_event_id is None) != (replaced_participant_key is None):
            raise ValueError("replacement event and participant key must be supplied together")
        if replaced_event_id == event_id:
            raise ValueError("replacement event must differ from the confirmed event")
        replacement_declarations = ""
        replacement_delete = ""
        params: dict[str, Any] = {
            "$event_id": event_id,
            "$participant_key": participant_key,
            "$operation_id": operation_id,
            "$character_wish": character_wish,
            "$attendance_status": AttendanceStatus.CONFIRMED.value,
            "$updated_at": datetime.now(UTC),
        }
        if replaced_event_id is not None and replaced_participant_key is not None:
            replacement_declarations = "DECLARE $replaced_event_id AS Utf8; DECLARE $replaced_participant_key AS Utf8;"
            replacement_delete = """
                DELETE FROM `registrations`
                WHERE event_id = $replaced_event_id
                  AND participant_key = $replaced_participant_key;
            """
            params.update(
                {
                    "$replaced_event_id": replaced_event_id,
                    "$replaced_participant_key": replaced_participant_key,
                }
            )
        await self.db.query(
            f"""
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $character_wish AS Utf8;
            DECLARE $attendance_status AS Utf8; DECLARE $updated_at AS Timestamp;
            {replacement_declarations}
            {replacement_delete}
            UPDATE `registrations`
            SET character_wish = $character_wish,
                attendance_status = $attendance_status,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $participant_key
              AND last_operation_id != $operation_id;
            """,
            params,
        )
        return True

    async def confirm_linked(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
        character_wish: str,
        replaced_event_id: str | None = None,
        replaced_participant_keys: Sequence[str] = (),
    ) -> bool:
        if (replaced_event_id is None) != (not replaced_participant_keys):
            raise ValueError("replacement event and participant keys must be supplied together")
        if replaced_event_id == event_id:
            raise ValueError("replacement event must differ from the confirmed event")
        replacement_declarations = ""
        replacement_update = ""
        now = datetime.now(UTC)
        params: dict[str, Any] = {
            "$event_id": event_id,
            "$participant_keys": list(participant_keys),
            "$operation_id": operation_id,
            "$character_wish": character_wish,
            "$confirmed_status": AttendanceStatus.CONFIRMED.value,
            "$updated_at": now,
        }
        if replaced_event_id is not None:
            replacement_declarations = (
                "DECLARE $replaced_event_id AS Utf8; "
                "DECLARE $replaced_participant_keys AS List<Utf8>; "
                "DECLARE $cancelled_status AS Utf8;"
            )
            replacement_update = """
                UPDATE `registrations`
                SET attendance_status = $cancelled_status,
                    last_operation_id = $operation_id,
                    updated_at = $updated_at
                WHERE event_id = $replaced_event_id
                  AND participant_key IN $replaced_participant_keys
                  AND last_operation_id != $operation_id;
            """
            params.update(
                {
                    "$replaced_event_id": replaced_event_id,
                    "$replaced_participant_keys": list(replaced_participant_keys),
                    "$cancelled_status": AttendanceStatus.CANCELLED.value,
                }
            )
        await self.db.query(
            f"""
            DECLARE $event_id AS Utf8; DECLARE $participant_keys AS List<Utf8>;
            DECLARE $operation_id AS Utf8; DECLARE $character_wish AS Utf8;
            DECLARE $confirmed_status AS Utf8; DECLARE $updated_at AS Timestamp;
            {replacement_declarations}
            {replacement_update}
            UPDATE `registrations`
            SET character_wish = $character_wish,
                attendance_status = $confirmed_status,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key IN $participant_keys
              AND last_operation_id != $operation_id;
            """,
            params,
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

    async def update_linked_character_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
        character_wish: str,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_keys AS List<Utf8>;
            DECLARE $operation_id AS Utf8; DECLARE $character_wish AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET character_wish = $character_wish,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key IN $participant_keys
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_keys": list(participant_keys),
                "$operation_id": operation_id,
                "$character_wish": character_wish,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def update_co_player_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        wish_play: str,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $wish_play AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET wish_play = $wish_play,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $participant_key
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_key": participant_key,
                "$operation_id": operation_id,
                "$wish_play": wish_play,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def update_linked_co_player_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
        wish_play: str,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_keys AS List<Utf8>;
            DECLARE $operation_id AS Utf8; DECLARE $wish_play AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET wish_play = $wish_play,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key IN $participant_keys
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_keys": list(participant_keys),
                "$operation_id": operation_id,
                "$wish_play": wish_play,
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

    async def cancel_linked(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_keys AS List<Utf8>;
            DECLARE $operation_id AS Utf8; DECLARE $attendance_status AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET attendance_status = $attendance_status,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key IN $participant_keys
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$participant_keys": list(participant_keys),
                "$operation_id": operation_id,
                "$attendance_status": AttendanceStatus.CANCELLED.value,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

    async def sift_waiting_to_end(self, event_id: str) -> None:
        active = [
            registration
            for registration in await self.list_for_event(event_id)
            if registration.attendance_status is not AttendanceStatus.CANCELLED
        ]
        if not active:
            return
        created_times = [registration.created_at for registration in active]
        shift = max(created_times) - min(created_times) + timedelta(microseconds=1)
        shift_microseconds = shift // timedelta(microseconds=1)
        # Timestamp + Interval is Optional<Timestamp> in YQL because it may overflow.
        # YDB rejects the whole UPDATE when an optional value targets the NOT NULL
        # created_at column, so the result must be unwrapped explicitly.
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $waiting_status AS Utf8;
            DECLARE $shift AS Interval;
            UPDATE `registrations`
            SET created_at = Unwrap(created_at + $shift)
            WHERE event_id = $event_id AND attendance_status = $waiting_status;
            """,
            {
                "$event_id": event_id,
                "$waiting_status": AttendanceStatus.WAITING.value,
                "$shift": shift_microseconds,
            },
        )

    async def remove(self, event_id: str, *, participant_key: str) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
            DELETE FROM `registrations`
            WHERE event_id = $event_id AND participant_key = $participant_key;
            """,
            {"$event_id": event_id, "$participant_key": participant_key},
        )
        return True

    async def remove_many(self, event_id: str, *, participant_keys: Sequence[str]) -> None:
        if not participant_keys:
            return
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $participant_keys AS List<Utf8>;
            DELETE FROM `registrations`
            WHERE event_id = $event_id AND participant_key IN $participant_keys;
            """,
            {"$event_id": event_id, "$participant_keys": list(participant_keys)},
        )

    async def delete_for_event(self, event_id: str) -> None:
        await self.db.query(
            "DECLARE $event_id AS Utf8; DELETE FROM `registrations` WHERE event_id = $event_id;",
            {"$event_id": event_id},
        )
