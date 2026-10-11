from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from pydantic import TypeAdapter

from larp_bot.adapters.ydb.repositories import YdbEventRepository, YdbExecutor
from larp_bot.domain.models import (
    AttendanceStatus,
    Button,
    Event,
    PassDetails,
    Platform,
    Registration,
    TelegramUser,
    VkUser,
)
from larp_bot.migration.canonical import (
    TABLE_NAMES,
    CanonicalDocument,
    CanonicalRow,
    CanonicalTables,
    CanonicalValidationError,
    build_document,
    canonical_event,
    canonical_leader,
    canonical_registration,
    canonical_user,
    compare_documents,
    event_from_row,
    leader_from_row,
    registration_from_row,
    row_key,
    user_from_row,
    validate_document,
)


class YdbMigrationExecutor(Protocol):
    async def snapshot(self, queries: Sequence[str]) -> list[list[dict[str, Any]]]: ...

    async def query(
        self,
        yql: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]: ...


_BUTTONS = TypeAdapter(list[Button])
_STRING_LIST = TypeAdapter(list[str])

_SNAPSHOT_QUERIES = (
    "SELECT * FROM `tg_users` ORDER BY tg_id;",
    "SELECT * FROM `vk_users` ORDER BY vk_id;",
    "SELECT * FROM `events` ORDER BY event_id;",
    "SELECT * FROM `event_leaders` ORDER BY event_id, platform, platform_user_id;",
    "SELECT * FROM `registrations` ORDER BY event_id, participant_key;",
)

_USER_UPSERT = """
    DECLARE $user_id AS Uint64;
    DECLARE $vk_url AS Optional<Utf8>; DECLARE $linked_vk_id AS Optional<Uint64>;
    DECLARE $telegram_handle AS Optional<Utf8>; DECLARE $full_name AS Optional<Utf8>;
    DECLARE $nickname AS Optional<Utf8>; DECLARE $crossplay AS Optional<Bool>;
    DECLARE $larp_experience AS Optional<Bool>; DECLARE $needs_pass AS Optional<Bool>;
    DECLARE $pass_details AS Optional<Utf8>; DECLARE $dialog_state AS Utf8;
    DECLARE $dialog_context AS Utf8; DECLARE $last_update_id AS Optional<Utf8>;
    DECLARE $last_update_at AS Optional<Timestamp>;
    DECLARE $last_delivery_operation_id AS Optional<Utf8>; DECLARE $last_bot_buttons AS Utf8;
    DECLARE $is_gamemaster AS Bool; DECLARE $privileged_role AS Optional<Utf8>;
    DECLARE $gamemaster_grant_operation_id AS Optional<Utf8>;
    DECLARE $created_at AS Timestamp; DECLARE $updated_at AS Timestamp;
    UPSERT INTO `{table}` (
        {id_column}, {platform_columns} full_name, nickname, crossplay, larp_experience,
        needs_pass, pass_details_json, dialog_state, dialog_context_json, last_update_id,
        last_update_at, last_delivery_operation_id, last_bot_buttons_json, is_gamemaster,
        privileged_role, gamemaster_grant_operation_id, created_at, updated_at
    ) VALUES (
        $user_id, {platform_values} $full_name, $nickname, $crossplay, $larp_experience,
        $needs_pass, $pass_details, $dialog_state, $dialog_context, $last_update_id,
        $last_update_at, $last_delivery_operation_id, $last_bot_buttons, $is_gamemaster,
        $privileged_role, $gamemaster_grant_operation_id, $created_at, $updated_at
    );
"""

_EVENT_UPSERT = """
    DECLARE $event_id AS Utf8; DECLARE $name AS Utf8; DECLARE $game_date AS Optional<Date>;
    DECLARE $player_amount AS Uint64; DECLARE $signup_token AS Optional<Utf8>;
    DECLARE $telegram_post_url AS Optional<Utf8>; DECLARE $vk_post_url AS Optional<Utf8>;
    DECLARE $telegram_chat_id AS Optional<Int64>;
    DECLARE $telegram_chat_invite_link AS Optional<Utf8>;
    DECLARE $disk_resource_path AS Utf8; DECLARE $public_url AS Utf8;
    DECLARE $master_table_google_url AS Optional<Utf8>;
    DECLARE $public_table_resource_path AS Optional<Utf8>;
    DECLARE $public_table_public_url AS Optional<Utf8>; DECLARE $status AS Utf8;
    DECLARE $public_table_google_url AS Optional<Utf8>;
    DECLARE $registration_open_at AS Optional<Timestamp>;
    DECLARE $registration_open_notified_recipients_json AS Utf8;
    DECLARE $registration_open_notification_completed_at AS Optional<Timestamp>;
    DECLARE $confirmation_open_at AS Optional<Timestamp>;
    DECLARE $scheduled_close_at AS Optional<Timestamp>;
    DECLARE $scheduled_close_operation_id AS Optional<Utf8>;
    DECLARE $scheduled_close_notified_recipients_json AS Utf8;
    DECLARE $scheduled_close_completed_at AS Optional<Timestamp>;
    DECLARE $confirmation_deadline AS Optional<Timestamp>;
    DECLARE $registrations_migrated_at AS Optional<Timestamp>;
    DECLARE $pass_table_resource_path AS Optional<Utf8>;
    DECLARE $pass_table_public_url AS Optional<Utf8>;
    DECLARE $pass_table_google_url AS Optional<Utf8>;
    DECLARE $achievement_resource_path AS Optional<Utf8>;
    DECLARE $achievement_public_url AS Optional<Utf8>;
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
    DECLARE $created_at AS Timestamp; DECLARE $updated_at AS Timestamp;
    UPSERT INTO `events` (
        event_id, name, game_date, player_amount, signup_token, telegram_post_url, vk_post_url,
        telegram_chat_id, telegram_chat_invite_link, disk_resource_path, public_registration_url,
        master_table_google_url, public_table_resource_path, public_table_public_url,
        public_table_google_url, status, registration_open_at,
        registration_open_notified_recipients_json, registration_open_notification_completed_at,
        confirmation_open_at, scheduled_close_at, scheduled_close_operation_id,
        scheduled_close_notified_recipients_json, scheduled_close_completed_at,
        confirmation_deadline, registrations_migrated_at, pass_table_resource_path,
        pass_table_public_url, pass_table_google_url, achievement_resource_path, achievement_public_url,
        confirmed_notifications_json, last_confirmed_notification_operation_id,
        last_reserve_promotion_operation_id, last_reserve_promotion_participant_key,
        last_reserve_promotion_delivered_operation_id, last_confirmed_link_promotion_operation_id,
        last_confirmed_link_promotion_participant_key,
        last_confirmed_link_promotion_delivered_operation_id,
        last_participant_removal_operation_id, participant_removal_keys_json,
        participant_removal_notified_keys_json, archived_at, created_at, updated_at
    ) VALUES (
        $event_id, $name, $game_date, $player_amount, $signup_token, $telegram_post_url, $vk_post_url,
        $telegram_chat_id, $telegram_chat_invite_link, $disk_resource_path, $public_url,
        $master_table_google_url, $public_table_resource_path, $public_table_public_url,
        $public_table_google_url, $status, $registration_open_at,
        $registration_open_notified_recipients_json, $registration_open_notification_completed_at,
        $confirmation_open_at, $scheduled_close_at, $scheduled_close_operation_id,
        $scheduled_close_notified_recipients_json, $scheduled_close_completed_at,
        $confirmation_deadline, $registrations_migrated_at, $pass_table_resource_path,
        $pass_table_public_url, $pass_table_google_url, $achievement_resource_path, $achievement_public_url,
        $confirmed_notifications_json, $last_confirmed_notification_operation_id,
        $last_reserve_promotion_operation_id, $last_reserve_promotion_participant_key,
        $last_reserve_promotion_delivered_operation_id, $last_confirmed_link_promotion_operation_id,
        $last_confirmed_link_promotion_participant_key,
        $last_confirmed_link_promotion_delivered_operation_id,
        $last_participant_removal_operation_id, $participant_removal_keys_json,
        $participant_removal_notified_keys_json, $archived_at, $created_at, $updated_at
    );
"""

_LEADER_UPSERT = """
    DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8;
    DECLARE $platform_user_id AS Uint64; DECLARE $created_at AS Timestamp;
    UPSERT INTO `event_leaders` (event_id, platform, platform_user_id, created_at)
    VALUES ($event_id, $platform, $platform_user_id, $created_at);
"""

_REGISTRATION_UPSERT = """
    DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8;
    DECLARE $display_name AS Utf8; DECLARE $nickname AS Optional<Utf8>;
    DECLARE $vk_profile AS Utf8; DECLARE $telegram_profile AS Optional<Utf8>;
    DECLARE $wish_play AS Utf8; DECLARE $larp_experience AS Optional<Bool>;
    DECLARE $crossplay AS Optional<Bool>; DECLARE $character_wish AS Utf8;
    DECLARE $attendance_status AS Utf8; DECLARE $last_operation_id AS Utf8;
    DECLARE $created_at AS Timestamp; DECLARE $updated_at AS Timestamp;
    DECLARE $participation_deliveries AS Utf8;
    UPSERT INTO `registrations` (
        event_id, participant_key, display_name, nickname, vk_profile, telegram_profile,
        wish_play, larp_experience, crossplay, character_wish, attendance_status,
        last_operation_id, created_at, updated_at, participation_notification_deliveries_json
    ) VALUES (
        $event_id, $participant_key, $display_name, $nickname, $vk_profile, $telegram_profile,
        $wish_play, $larp_experience, $crossplay, $character_wish, $attendance_status,
        $last_operation_id, $created_at, $updated_at, $participation_deliveries
    );
"""


def _json_list(raw: object, field: str) -> list[str]:
    if raw is None or raw == "":
        return []
    try:
        return _STRING_LIST.validate_python(json.loads(str(raw)))
    except (ValueError, TypeError) as exc:
        raise CanonicalValidationError(f"{field} must contain a JSON string list") from exc


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if type(value) is int:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(value))
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if type(value) is int:
        return date(1970, 1, 1) + timedelta(days=int(value))
    return date.fromisoformat(str(value))


def _optional_json(raw: object, adapter: TypeAdapter[Any], field: str) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return adapter.validate_python(json.loads(str(raw)))
    except (ValueError, TypeError) as exc:
        raise CanonicalValidationError(f"{field} contains invalid JSON") from exc


def _user_from_ydb(table: str, row: Mapping[str, Any]) -> TelegramUser | VkUser:
    common: dict[str, Any] = {
        "full_name": row.get("full_name"),
        "nickname": row.get("nickname"),
        "crossplay": row.get("crossplay"),
        "larp_experience": row.get("larp_experience"),
        "needs_pass": row.get("needs_pass"),
        "pass_details": _optional_json(row.get("pass_details_json"), TypeAdapter(PassDetails), "pass_details_json"),
        "dialog_state": row.get("dialog_state") or "IDLE",
        "dialog_context": json.loads(str(row.get("dialog_context_json") or "{}")),
        "last_update_id": row.get("last_update_id"),
        "last_update_at": _datetime(row["last_update_at"]) if row.get("last_update_at") is not None else None,
        "last_delivery_operation_id": row.get("last_delivery_operation_id"),
        "last_bot_buttons": _optional_json(row.get("last_bot_buttons_json"), _BUTTONS, "last_bot_buttons_json") or [],
        "is_gamemaster": bool(row.get("is_gamemaster")),
        "privileged_role": row.get("privileged_role"),
        "gamemaster_grant_operation_id": row.get("gamemaster_grant_operation_id"),
        "created_at": _datetime(row["created_at"]),
        "updated_at": _datetime(row["updated_at"]),
    }
    if not isinstance(common["dialog_context"], dict):
        raise CanonicalValidationError("dialog_context_json must contain a JSON object")
    if table == "tg_users":
        return TelegramUser.model_validate(
            {
                "tg_id": int(row["tg_id"]),
                "vk_url": row.get("vk_url"),
                "linked_vk_id": row.get("linked_vk_id"),
                "telegram_handle": row.get("telegram_handle"),
                **common,
            }
        )
    return VkUser.model_validate({"vk_id": int(row["vk_id"]), "telegram_handle": row.get("telegram_handle"), **common})


def _registration_from_ydb(row: Mapping[str, Any]) -> tuple[Registration, list[str]]:
    registration = Registration(
        event_id=str(row["event_id"]),
        participant_key=str(row["participant_key"]),
        display_name=str(row["display_name"]),
        nickname=row.get("nickname"),
        vk_profile=str(row.get("vk_profile") or ""),
        telegram_profile=row.get("telegram_profile"),
        wish_play=str(row["wish_play"]),
        larp_experience=row.get("larp_experience"),
        crossplay=row.get("crossplay"),
        character_wish=str(row.get("character_wish") or ""),
        attendance_status=AttendanceStatus(str(row["attendance_status"])),
        last_operation_id=str(row.get("last_operation_id") or ""),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )
    deliveries = _json_list(
        row.get("participation_notification_deliveries_json"),
        "participation_notification_deliveries_json",
    )
    return registration, deliveries


def _user_params(user: TelegramUser | VkUser) -> dict[str, Any]:
    return {
        "$user_id": user.tg_id if isinstance(user, TelegramUser) else user.vk_id,
        "$vk_url": user.vk_url if isinstance(user, TelegramUser) else None,
        "$linked_vk_id": user.linked_vk_id if isinstance(user, TelegramUser) else None,
        "$telegram_handle": user.telegram_handle,
        "$full_name": user.full_name,
        "$nickname": user.nickname,
        "$crossplay": user.crossplay,
        "$larp_experience": user.larp_experience,
        "$needs_pass": user.needs_pass,
        "$pass_details": user.pass_details.model_dump_json() if user.pass_details is not None else None,
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
        "$is_gamemaster": user.is_gamemaster,
        "$privileged_role": user.privileged_role.value if user.privileged_role is not None else None,
        "$gamemaster_grant_operation_id": user.gamemaster_grant_operation_id,
        "$created_at": user.created_at,
        "$updated_at": user.updated_at,
    }


def _event_params(event: Event) -> dict[str, Any]:
    return {
        "$event_id": event.event_id,
        "$name": event.name,
        "$game_date": event.game_date,
        "$player_amount": event.player_amount,
        "$signup_token": event.signup_token,
        "$telegram_post_url": event.telegram_post_url,
        "$vk_post_url": event.vk_post_url,
        "$telegram_chat_id": event.telegram_chat_id,
        "$telegram_chat_invite_link": event.telegram_chat_invite_link,
        "$disk_resource_path": event.disk_resource_path,
        "$public_url": event.public_registration_url,
        "$master_table_google_url": event.master_table_google_url,
        "$public_table_resource_path": event.public_table_resource_path,
        "$public_table_public_url": event.public_table_public_url,
        "$public_table_google_url": event.public_table_google_url,
        "$status": event.status.value,
        "$registration_open_at": event.registration_open_at,
        "$registration_open_notified_recipients_json": json.dumps(
            event.registration_open_notified_recipients, separators=(",", ":")
        ),
        "$registration_open_notification_completed_at": event.registration_open_notification_completed_at,
        "$confirmation_open_at": event.confirmation_open_at,
        "$scheduled_close_at": event.scheduled_close_at,
        "$scheduled_close_operation_id": event.scheduled_close_operation_id,
        "$scheduled_close_notified_recipients_json": json.dumps(
            event.scheduled_close_notified_recipients, separators=(",", ":")
        ),
        "$scheduled_close_completed_at": event.scheduled_close_completed_at,
        "$confirmation_deadline": event.confirmation_deadline,
        "$registrations_migrated_at": event.registrations_migrated_at,
        "$pass_table_resource_path": event.pass_table_resource_path,
        "$pass_table_public_url": event.pass_table_public_url,
        "$pass_table_google_url": event.pass_table_google_url,
        "$achievement_resource_path": event.achievement_resource_path,
        "$achievement_public_url": event.achievement_public_url,
        "$confirmed_notifications_json": json.dumps(
            event.confirmed_notifications, ensure_ascii=False, separators=(",", ":")
        ),
        "$last_confirmed_notification_operation_id": event.last_confirmed_notification_operation_id,
        "$last_reserve_promotion_operation_id": event.last_reserve_promotion_operation_id,
        "$last_reserve_promotion_participant_key": event.last_reserve_promotion_participant_key,
        "$last_reserve_promotion_delivered_operation_id": event.last_reserve_promotion_delivered_operation_id,
        "$last_confirmed_link_promotion_operation_id": event.last_confirmed_link_promotion_operation_id,
        "$last_confirmed_link_promotion_participant_key": event.last_confirmed_link_promotion_participant_key,
        "$last_confirmed_link_promotion_delivered_operation_id": (
            event.last_confirmed_link_promotion_delivered_operation_id
        ),
        "$last_participant_removal_operation_id": event.last_participant_removal_operation_id,
        "$participant_removal_keys_json": json.dumps(event.participant_removal_keys, separators=(",", ":")),
        "$participant_removal_notified_keys_json": json.dumps(
            event.participant_removal_notified_keys, separators=(",", ":")
        ),
        "$archived_at": event.archived_at,
        "$created_at": event.created_at,
        "$updated_at": event.updated_at,
    }


def _registration_params(registration: Registration, deliveries: Sequence[str]) -> dict[str, Any]:
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
        "$participation_deliveries": json.dumps(deliveries, separators=(",", ":")),
    }


class YdbMigrationStore:
    def __init__(self, executor: YdbMigrationExecutor) -> None:
        self.executor = executor

    async def export_document(self) -> CanonicalDocument:
        snapshots = await self.executor.snapshot(_SNAPSHOT_QUERIES)
        if len(snapshots) != len(TABLE_NAMES):
            raise CanonicalValidationError("YDB snapshot returned an unexpected number of result sets")
        tg_rows, vk_rows, event_rows, leader_rows, registration_rows = snapshots
        tables: CanonicalTables = {
            "tg_users": [canonical_user(_user_from_ydb("tg_users", row)) for row in tg_rows],
            "vk_users": [canonical_user(_user_from_ydb("vk_users", row)) for row in vk_rows],
            "events": [canonical_event(YdbEventRepository._from_row(row)) for row in event_rows],
            "event_leaders": [
                canonical_leader(
                    str(row["event_id"]),
                    Platform(str(row["platform"])),
                    int(row["platform_user_id"]),
                    _datetime(row["created_at"]),
                )
                for row in leader_rows
            ],
            "registrations": [canonical_registration(*_registration_from_ydb(row)) for row in registration_rows],
        }
        return build_document(tables, source="ydb")

    async def import_document(
        self,
        document: CanonicalDocument,
        *,
        delete_extra: bool = False,
    ) -> CanonicalDocument:
        document = validate_document(document)
        current = await self.export_document()
        current_by_key = {
            table: {row_key(table, row): row for row in current["tables"][table]} for table in TABLE_NAMES
        }
        desired_by_key = {
            table: {row_key(table, row): row for row in document["tables"][table]} for table in TABLE_NAMES
        }
        extras = {table: sorted(set(current_by_key[table]) - set(desired_by_key[table])) for table in TABLE_NAMES}
        extras = {table: keys for table, keys in extras.items() if keys}
        if extras and not delete_extra:
            summary = ", ".join(f"{table}={len(keys)}" for table, keys in extras.items())
            raise CanonicalValidationError(
                f"target YDB has records absent from the dump ({summary}); "
                "rerun with --delete-extra only in maintenance"
            )

        for table in TABLE_NAMES:
            for key, row in desired_by_key[table].items():
                if current_by_key[table].get(key) == row:
                    continue
                await self._upsert(table, row)

        if delete_extra:
            for table in reversed(TABLE_NAMES):
                for key in extras.get(table, []):
                    await self._delete(table, key)

        exported = await self.export_document()
        compare_documents(document, exported)
        return exported

    async def _upsert(self, table: str, row: CanonicalRow) -> None:
        if table in {"tg_users", "vk_users"}:
            user = user_from_row(table, row)
            if table == "tg_users":
                query = _USER_UPSERT.format(
                    table="tg_users",
                    id_column="tg_id",
                    platform_columns="vk_url, linked_vk_id, telegram_handle,",
                    platform_values="$vk_url, $linked_vk_id, $telegram_handle,",
                )
            else:
                query = _USER_UPSERT.format(
                    table="vk_users",
                    id_column="vk_id",
                    platform_columns="telegram_handle,",
                    platform_values="$telegram_handle,",
                )
            await self.executor.query(f"-- migration:{table}:upsert\n{query}", _user_params(user))
            return
        if table == "events":
            await self.executor.query(
                f"-- migration:{table}:upsert\n{_EVENT_UPSERT}",
                _event_params(event_from_row(row)),
            )
            return
        if table == "event_leaders":
            event_id, platform, user_id, created_at = leader_from_row(row)
            await self.executor.query(
                f"-- migration:{table}:upsert\n{_LEADER_UPSERT}",
                {
                    "$event_id": event_id,
                    "$platform": platform.value,
                    "$platform_user_id": user_id,
                    "$created_at": created_at,
                },
            )
            return
        if table == "registrations":
            registration, deliveries = registration_from_row(row)
            await self.executor.query(
                f"-- migration:{table}:upsert\n{_REGISTRATION_UPSERT}",
                _registration_params(registration, deliveries),
            )
            return
        raise CanonicalValidationError(f"unknown YDB table: {table}")

    async def _delete(self, table: str, key: tuple[str, ...]) -> None:
        if table == "tg_users":
            query = "DECLARE $tg_id AS Uint64; DELETE FROM `tg_users` WHERE tg_id = $tg_id;"
            params: dict[str, Any] = {"$tg_id": int(key[0])}
        elif table == "vk_users":
            query = "DECLARE $vk_id AS Uint64; DELETE FROM `vk_users` WHERE vk_id = $vk_id;"
            params = {"$vk_id": int(key[0])}
        elif table == "events":
            query = "DECLARE $event_id AS Utf8; DELETE FROM `events` WHERE event_id = $event_id;"
            params = {"$event_id": key[0]}
        elif table == "event_leaders":
            query = (
                "DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8; "
                "DECLARE $platform_user_id AS Uint64; DELETE FROM `event_leaders` "
                "WHERE event_id = $event_id AND platform = $platform "
                "AND platform_user_id = $platform_user_id;"
            )
            params = {"$event_id": key[0], "$platform": key[1], "$platform_user_id": int(key[2])}
        elif table == "registrations":
            query = (
                "DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8; "
                "DELETE FROM `registrations` WHERE event_id = $event_id "
                "AND participant_key = $participant_key;"
            )
            params = {"$event_id": key[0], "$participant_key": key[1]}
        else:
            raise CanonicalValidationError(f"unknown YDB table: {table}")
        await self.executor.query(f"-- migration:{table}:delete\n{query}", params)


def open_ydb_store(endpoint: str, database: str, iam_token: str | None) -> tuple[YdbMigrationStore, YdbExecutor]:
    executor = YdbExecutor(endpoint, database, iam_token=iam_token)
    return YdbMigrationStore(executor), executor
