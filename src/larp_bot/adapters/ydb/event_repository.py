"""YDB persistence for event lifecycle state."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime
from typing import Any

from larp_bot.domain.models import (
    DEFAULT_PLAYER_AMOUNT,
    MAX_EVENT_PAGE_SIZE,
    MAX_PLAYER_AMOUNT,
    BotIdentity,
    Event,
    EventStatus,
    Platform,
    ScheduledLifecycleEvent,
)

from .common import (
    YdbExecutor,
    _confirmed_notifications,
    _date,
    _dt,
    _string_list,
)


class YdbEventRepository:
    COLUMNS = """
        event_id, name, game_date, player_amount, signup_token, disk_resource_path, public_registration_url,
        master_table_google_url,
        telegram_post_url, vk_post_url, telegram_chat_id, telegram_chat_invite_link,
        public_table_resource_path, public_table_public_url, public_table_google_url,
        status, registration_open_at, registration_open_notified_recipients_json,
        registration_open_notification_completed_at,
        confirmation_open_at,
        scheduled_close_at, scheduled_close_operation_id,
        scheduled_close_notified_recipients_json, scheduled_close_completed_at,
        confirmation_deadline, registrations_migrated_at,
        pass_table_resource_path, pass_table_public_url, pass_table_google_url,
        achievement_resource_path, achievement_public_url,
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
            telegram_chat_id=row.get("telegram_chat_id"),
            telegram_chat_invite_link=row.get("telegram_chat_invite_link"),
            disk_resource_path=row["disk_resource_path"],
            public_registration_url=row["public_registration_url"],
            master_table_google_url=row.get("master_table_google_url"),
            public_table_resource_path=row.get("public_table_resource_path"),
            public_table_public_url=row.get("public_table_public_url"),
            public_table_google_url=row.get("public_table_google_url"),
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
            confirmation_open_at=(
                _dt(row["confirmation_open_at"]) if row.get("confirmation_open_at") is not None else None
            ),
            scheduled_close_at=(_dt(row["scheduled_close_at"]) if row.get("scheduled_close_at") is not None else None),
            scheduled_close_operation_id=row.get("scheduled_close_operation_id"),
            scheduled_close_notified_recipients=_string_list(
                row.get("scheduled_close_notified_recipients_json"),
                "scheduled_close_notified_recipients_json",
            ),
            scheduled_close_completed_at=(
                _dt(row["scheduled_close_completed_at"])
                if row.get("scheduled_close_completed_at") is not None
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
            pass_table_google_url=row.get("pass_table_google_url"),
            achievement_resource_path=row.get("achievement_resource_path"),
            achievement_public_url=row.get("achievement_public_url"),
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

    async def get_by_telegram_chat_id(self, chat_id: int) -> Event | None:
        rows = await self.db.query(
            f"""
            DECLARE $chat_id AS Int64;
            SELECT {self.COLUMNS}
            FROM `events` WHERE telegram_chat_id = $chat_id
            LIMIT 2;
            """,
            {"$chat_id": chat_id},
            read_only=True,
        )
        if len(rows) > 1:
            raise RuntimeError("duplicate Telegram chat binding")
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
            DECLARE $telegram_chat_id AS Optional<Int64>;
            DECLARE $telegram_chat_invite_link AS Optional<Utf8>;
            DECLARE $disk_resource_path AS Utf8; DECLARE $public_url AS Utf8;
            DECLARE $master_table_google_url AS Optional<Utf8>;
            DECLARE $public_table_resource_path AS Optional<Utf8>;
            DECLARE $public_table_public_url AS Optional<Utf8>;
            DECLARE $public_table_google_url AS Optional<Utf8>;
            DECLARE $status AS Utf8; DECLARE $created_at AS Timestamp;
            DECLARE $registration_open_at AS Optional<Timestamp>;
            DECLARE $registration_open_notified_recipients_json AS Utf8;
            DECLARE $registration_open_notification_completed_at AS Optional<Timestamp>;
            DECLARE $confirmation_open_at AS Optional<Timestamp>;
            DECLARE $scheduled_close_at AS Optional<Timestamp>;
            DECLARE $scheduled_close_operation_id AS Optional<Utf8>;
            DECLARE $scheduled_close_notified_recipients_json AS Utf8;
            DECLARE $scheduled_close_completed_at AS Optional<Timestamp>;
            DECLARE $confirmation_deadline AS Optional<Timestamp>;
            DECLARE $registrations_migrated_at AS Timestamp;
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
            DECLARE $updated_at AS Timestamp;
            {" ".join(leader_declarations)}
            INSERT INTO `events` (
                event_id, name, game_date, player_amount, signup_token, disk_resource_path, public_registration_url,
                master_table_google_url,
                telegram_post_url, vk_post_url, telegram_chat_id, telegram_chat_invite_link,
                public_table_resource_path, public_table_public_url, public_table_google_url,
                status, registration_open_at, registration_open_notified_recipients_json,
                registration_open_notification_completed_at,
                confirmation_open_at,
                scheduled_close_at, scheduled_close_operation_id,
                scheduled_close_notified_recipients_json, scheduled_close_completed_at,
                confirmation_deadline, registrations_migrated_at,
                pass_table_resource_path, pass_table_public_url, pass_table_google_url,
                achievement_resource_path, achievement_public_url,
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
                $master_table_google_url,
                $telegram_post_url, $vk_post_url, $telegram_chat_id, $telegram_chat_invite_link,
                $public_table_resource_path, $public_table_public_url, $public_table_google_url,
                $status, $registration_open_at, $registration_open_notified_recipients_json,
                $registration_open_notification_completed_at,
                $confirmation_open_at,
                $scheduled_close_at, $scheduled_close_operation_id,
                $scheduled_close_notified_recipients_json, $scheduled_close_completed_at,
                $confirmation_deadline, $registrations_migrated_at,
                $pass_table_resource_path, $pass_table_public_url, $pass_table_google_url,
                $achievement_resource_path, $achievement_public_url,
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
                    event.registration_open_notified_recipients,
                    separators=(",", ":"),
                ),
                "$registration_open_notification_completed_at": (event.registration_open_notification_completed_at),
                "$confirmation_open_at": event.confirmation_open_at,
                "$scheduled_close_at": event.scheduled_close_at,
                "$scheduled_close_operation_id": event.scheduled_close_operation_id,
                "$scheduled_close_notified_recipients_json": json.dumps(
                    event.scheduled_close_notified_recipients,
                    separators=(",", ":"),
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
        params = {
            "$event_id": event_id,
            "$platform": leader.platform.value,
            "$platform_user_id": leader.platform_user_id,
        }
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $platform AS Utf8;
            DECLARE $platform_user_id AS Uint64;
            DELETE FROM `event_leaders`
            WHERE event_id = $event_id AND platform = $platform
              AND platform_user_id = $platform_user_id
            RETURNING event_id;
            """,
            params,
        )
        return bool(rows)

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
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events` SET status = $status, updated_at = $updated_at
            WHERE event_id = $event_id AND status != $status
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$status": status.value,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_registration_open_at(self, event_id: str, opens_at: datetime | None) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $opens_at AS Optional<Timestamp>;
            DECLARE $notified_recipients_json AS Utf8;
            DECLARE $completed_at AS Optional<Timestamp>; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET registration_open_at = $opens_at,
                registration_open_notified_recipients_json = $notified_recipients_json,
                registration_open_notification_completed_at = $completed_at,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND (
                (registration_open_at IS NULL AND $opens_at IS NOT NULL)
                OR (registration_open_at IS NOT NULL AND $opens_at IS NULL)
                OR registration_open_at != $opens_at
              )
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$opens_at": None if opens_at is None else opens_at.astimezone(UTC),
                "$notified_recipients_json": "[]",
                "$completed_at": None,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def list_due_lifecycle_events(
        self,
        due_by: datetime,
        *,
        limit: int = 300,
    ) -> Sequence[ScheduledLifecycleEvent]:
        rows = await self.db.query(
            """
            DECLARE $due_by AS Timestamp; DECLARE $created_status AS Utf8;
            DECLARE $closed_status AS Utf8; DECLARE $limit AS Uint64;
            SELECT event_id, name, game_date, status,
                   registration_open_at, registration_open_notified_recipients_json,
                   registration_open_notification_completed_at,
                   confirmation_open_at, confirmation_deadline,
                   scheduled_close_at, scheduled_close_operation_id,
                   scheduled_close_completed_at, archived_at
            FROM `events`
            WHERE (
                registration_open_at IS NOT NULL
                AND registration_open_at <= $due_by
                AND registration_open_notification_completed_at IS NULL
                AND archived_at IS NULL
                AND status != $closed_status
            ) OR (
                confirmation_open_at IS NOT NULL
                AND confirmation_open_at <= $due_by
                AND confirmation_deadline IS NOT NULL
                AND archived_at IS NULL
                AND status = $created_status
            ) OR (
                scheduled_close_at IS NOT NULL
                AND scheduled_close_at <= $due_by
                AND scheduled_close_completed_at IS NULL
                AND (archived_at IS NULL OR scheduled_close_operation_id IS NOT NULL)
            )
            ORDER BY event_id
            LIMIT $limit;
            """,
            {
                "$due_by": due_by.astimezone(UTC),
                "$created_status": EventStatus.CREATED.value,
                "$closed_status": EventStatus.CLOSED.value,
                "$limit": limit,
            },
            read_only=True,
        )
        return [
            ScheduledLifecycleEvent(
                event_id=row["event_id"],
                name=row["name"],
                game_date=_date(row["game_date"]) if row.get("game_date") is not None else None,
                status=(
                    EventStatus.CONFIRMATION_OPEN if str(row["status"]) == "OPEN" else EventStatus(str(row["status"]))
                ),
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
                confirmation_open_at=(
                    _dt(row["confirmation_open_at"]) if row.get("confirmation_open_at") is not None else None
                ),
                confirmation_deadline=(
                    _dt(row["confirmation_deadline"]) if row.get("confirmation_deadline") is not None else None
                ),
                scheduled_close_at=(
                    _dt(row["scheduled_close_at"]) if row.get("scheduled_close_at") is not None else None
                ),
                scheduled_close_operation_id=row.get("scheduled_close_operation_id"),
                scheduled_close_completed_at=(
                    _dt(row["scheduled_close_completed_at"])
                    if row.get("scheduled_close_completed_at") is not None
                    else None
                ),
                archived_at=_dt(row["archived_at"]) if row.get("archived_at") is not None else None,
            )
            for row in rows
        ]

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

    async def set_confirmation_schedule(
        self,
        event_id: str,
        opens_at: datetime | None,
        deadline: datetime | None,
    ) -> bool:
        if (opens_at is None) != (deadline is None):
            raise ValueError("confirmation opening and deadline must be set together")
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $opens_at AS Optional<Timestamp>;
            DECLARE $deadline AS Optional<Timestamp>; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET confirmation_open_at = $opens_at,
                confirmation_deadline = $deadline,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND (
                (confirmation_open_at IS NULL AND $opens_at IS NOT NULL)
                OR (confirmation_open_at IS NOT NULL AND $opens_at IS NULL)
                OR confirmation_open_at != $opens_at
                OR (confirmation_deadline IS NULL AND $deadline IS NOT NULL)
                OR (confirmation_deadline IS NOT NULL AND $deadline IS NULL)
                OR confirmation_deadline != $deadline
              )
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$opens_at": None if opens_at is None else opens_at.astimezone(UTC),
                "$deadline": None if deadline is None else deadline.astimezone(UTC),
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def list_due_confirmation_openings(
        self,
        opened_by: datetime,
        *,
        limit: int = 100,
    ) -> Sequence[Event]:
        rows = await self.db.query(
            f"""
            DECLARE $opened_by AS Timestamp; DECLARE $created_status AS Utf8;
            DECLARE $limit AS Uint64;
            SELECT {self.COLUMNS}
            FROM `events`
            WHERE confirmation_open_at IS NOT NULL
              AND confirmation_open_at <= $opened_by
              AND confirmation_deadline IS NOT NULL
              AND archived_at IS NULL
              AND status = $created_status
            ORDER BY confirmation_open_at, event_id
            LIMIT $limit;
            """,
            {
                "$opened_by": opened_by.astimezone(UTC),
                "$created_status": EventStatus.CREATED.value,
                "$limit": limit,
            },
            read_only=True,
        )
        return [self._from_row(row) for row in rows]

    async def open_scheduled_confirmation(
        self,
        event_id: str,
        opens_at: datetime,
        deadline: datetime,
    ) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $created_status AS Utf8;
            DECLARE $confirmation_status AS Utf8; DECLARE $opens_at AS Timestamp;
            DECLARE $deadline AS Timestamp; DECLARE $cleared_open_at AS Optional<Timestamp>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET status = $confirmation_status,
                confirmation_open_at = $cleared_open_at,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND archived_at IS NULL
              AND confirmation_deadline = $deadline
              AND (
                (status = $created_status AND confirmation_open_at = $opens_at)
                OR (status = $confirmation_status AND confirmation_open_at IS NULL)
              )
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$created_status": EventStatus.CREATED.value,
                "$confirmation_status": EventStatus.CONFIRMATION_OPEN.value,
                "$opens_at": opens_at.astimezone(UTC),
                "$deadline": deadline.astimezone(UTC),
                "$cleared_open_at": None,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_scheduled_close_at(self, event_id: str, closes_at: datetime | None) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $closes_at AS Optional<Timestamp>;
            DECLARE $operation_id AS Optional<Utf8>; DECLARE $recipients_json AS Utf8;
            DECLARE $completed_at AS Optional<Timestamp>; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET scheduled_close_at = $closes_at,
                scheduled_close_operation_id = $operation_id,
                scheduled_close_notified_recipients_json = $recipients_json,
                scheduled_close_completed_at = $completed_at,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND scheduled_close_operation_id IS NULL
              AND (
                (scheduled_close_at IS NULL AND $closes_at IS NOT NULL)
                OR (scheduled_close_at IS NOT NULL AND $closes_at IS NULL)
                OR scheduled_close_at != $closes_at
              )
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$closes_at": None if closes_at is None else closes_at.astimezone(UTC),
                "$operation_id": None,
                "$recipients_json": "[]",
                "$completed_at": None,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def list_due_scheduled_closings(
        self,
        closed_by: datetime,
        *,
        limit: int = 100,
    ) -> Sequence[Event]:
        rows = await self.db.query(
            f"""
            DECLARE $closed_by AS Timestamp; DECLARE $limit AS Uint64;
            SELECT {self.COLUMNS}
            FROM `events`
            WHERE scheduled_close_at IS NOT NULL
              AND scheduled_close_at <= $closed_by
              AND scheduled_close_completed_at IS NULL
              AND (archived_at IS NULL OR scheduled_close_operation_id IS NOT NULL)
            ORDER BY scheduled_close_at, event_id
            LIMIT $limit;
            """,
            {"$closed_by": closed_by.astimezone(UTC), "$limit": limit},
            read_only=True,
        )
        return [self._from_row(row) for row in rows]

    async def start_scheduled_close(
        self,
        event_id: str,
        operation_id: str,
        started_at: datetime,
    ) -> bool:
        event = await self.get(event_id)
        if event is None:
            return False
        if event.scheduled_close_operation_id == operation_id:
            return True
        if event.archived_at is not None or event.scheduled_close_operation_id is not None:
            return False
        started_at = started_at.astimezone(UTC)
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $started_at AS Timestamp;
            UPDATE `events`
            SET status = $status,
                scheduled_close_operation_id = $operation_id,
                updated_at = $started_at
            WHERE event_id = $event_id
              AND archived_at IS NULL
              AND scheduled_close_at IS NOT NULL
              AND scheduled_close_operation_id IS NULL
              AND scheduled_close_completed_at IS NULL;
            """,
            {
                "$event_id": event_id,
                "$status": EventStatus.CLOSED.value,
                "$operation_id": operation_id,
                "$started_at": started_at,
            },
        )
        return True

    async def complete_scheduled_close_actions(
        self,
        event_id: str,
        operation_id: str,
        completed_at: datetime,
    ) -> bool:
        event = await self.get(event_id)
        if event is None or event.scheduled_close_operation_id != operation_id:
            return False
        if event.archived_at is not None:
            return True
        completed_at = completed_at.astimezone(UTC)
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $operation_id AS Utf8; DECLARE $completed_at AS Timestamp;
            UPDATE `events`
            SET status = $status,
                archived_at = $completed_at,
                updated_at = $completed_at
            WHERE event_id = $event_id
              AND archived_at IS NULL
              AND scheduled_close_operation_id = $operation_id
              AND scheduled_close_completed_at IS NULL;
            """,
            {
                "$event_id": event_id,
                "$status": EventStatus.CLOSED.value,
                "$operation_id": operation_id,
                "$completed_at": completed_at,
            },
        )
        return True

    async def mark_scheduled_close_notification_delivered(
        self,
        event_id: str,
        operation_id: str,
        recipient: str,
    ) -> None:
        event = await self.get(event_id)
        if (
            event is None
            or event.scheduled_close_operation_id != operation_id
            or recipient in event.scheduled_close_notified_recipients
        ):
            return
        recipients = [*event.scheduled_close_notified_recipients, recipient]
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $recipients_json AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET scheduled_close_notified_recipients_json = $recipients_json,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND scheduled_close_operation_id = $operation_id
              AND scheduled_close_completed_at IS NULL;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$recipients_json": json.dumps(recipients, separators=(",", ":")),
                "$updated_at": datetime.now(UTC),
            },
        )

    async def complete_scheduled_close_notifications(
        self,
        event_id: str,
        operation_id: str,
        completed_at: datetime,
    ) -> None:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $completed_at AS Timestamp;
            UPDATE `events`
            SET scheduled_close_completed_at = $completed_at,
                updated_at = $completed_at
            WHERE event_id = $event_id
              AND scheduled_close_operation_id = $operation_id
              AND scheduled_close_completed_at IS NULL;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$completed_at": completed_at.astimezone(UTC),
            },
        )

    async def set_player_amount(self, event_id: str, player_amount: int) -> bool:
        if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
            raise ValueError(f"player_amount must be between 1 and {MAX_PLAYER_AMOUNT}")
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $player_amount AS Uint64;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events` SET player_amount = $player_amount, updated_at = $updated_at
            WHERE event_id = $event_id AND player_amount != $player_amount
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$player_amount": player_amount,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_details(self, event_id: str, name: str, game_date: date) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $name AS Utf8;
            DECLARE $game_date AS Date; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET name = $name, game_date = $game_date, updated_at = $updated_at
            WHERE event_id = $event_id
              AND (name != $name OR game_date IS NULL OR game_date != $game_date)
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$name": name,
                "$game_date": game_date,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_post_links(self, event_id: str, telegram_post_url: str, vk_post_url: str) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $telegram_post_url AS Utf8;
            DECLARE $vk_post_url AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET telegram_post_url = $telegram_post_url,
                vk_post_url = $vk_post_url,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND (
                telegram_post_url IS NULL OR telegram_post_url != $telegram_post_url
                OR vk_post_url IS NULL OR vk_post_url != $vk_post_url
              )
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$telegram_post_url": telegram_post_url,
                "$vk_post_url": vk_post_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def bind_telegram_chat(self, event_id: str, chat_id: int) -> bool:
        return await self.db.bind_event_chat(event_id, chat_id)

    async def set_telegram_chat_invite_link(self, event_id: str, chat_id: int, invite_link: str) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $chat_id AS Int64;
            DECLARE $invite_link AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET telegram_chat_invite_link = $invite_link, updated_at = $updated_at
            WHERE event_id = $event_id
              AND telegram_chat_id = $chat_id
              AND telegram_chat_invite_link IS NULL
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$chat_id": chat_id,
                "$invite_link": invite_link,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def archive(self, event_id: str, archived_at: datetime) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $archived_at AS Timestamp;
            UPDATE `events`
            SET status = $status, archived_at = $archived_at, updated_at = $archived_at
            WHERE event_id = $event_id AND archived_at IS NULL
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$status": EventStatus.CLOSED.value,
                "$archived_at": archived_at.astimezone(UTC),
            },
        )
        return bool(rows)

    async def unarchive(self, event_id: str, updated_at: datetime) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $archived_at AS Optional<Timestamp>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET archived_at = $archived_at, updated_at = $updated_at
            WHERE event_id = $event_id AND archived_at IS NOT NULL
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$archived_at": None,
                "$updated_at": updated_at.astimezone(UTC),
            },
        )
        return bool(rows)

    async def open_confirmation(self, event_id: str, deadline: datetime) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $status AS Utf8;
            DECLARE $confirmation_open_at AS Optional<Timestamp>;
            DECLARE $deadline AS Timestamp; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET status = $status,
                confirmation_open_at = $confirmation_open_at,
                confirmation_deadline = $deadline,
                updated_at = $updated_at
            WHERE event_id = $event_id
              AND (
                status != $status
                OR confirmation_deadline IS NULL
                OR confirmation_deadline != $deadline
              )
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$status": EventStatus.CONFIRMATION_OPEN.value,
                "$confirmation_open_at": None,
                "$deadline": deadline.astimezone(UTC),
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

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

    async def set_public_table(
        self,
        event_id: str,
        resource_path: str,
        public_url: str,
        google_url: str | None = None,
    ) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $resource_path AS Utf8;
            DECLARE $public_url AS Utf8; DECLARE $google_url AS Optional<Utf8>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET public_table_resource_path = $resource_path,
                public_table_public_url = $public_url,
                public_table_google_url = $google_url,
                updated_at = $updated_at
            WHERE event_id = $event_id AND public_table_public_url IS NULL
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$resource_path": resource_path,
                "$public_url": public_url,
                "$google_url": google_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_pass_table(
        self,
        event_id: str,
        resource_path: str,
        public_url: str,
        google_url: str | None = None,
    ) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $resource_path AS Utf8;
            DECLARE $public_url AS Utf8; DECLARE $google_url AS Optional<Utf8>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET pass_table_resource_path = $resource_path,
                pass_table_public_url = $public_url,
                pass_table_google_url = $google_url,
                updated_at = $updated_at
            WHERE event_id = $event_id AND pass_table_public_url IS NULL
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$resource_path": resource_path,
                "$public_url": public_url,
                "$google_url": google_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_google_table_links(
        self,
        event_id: str,
        *,
        master_url: str | None = None,
        public_url: str | None = None,
        pass_url: str | None = None,
    ) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8;
            DECLARE $master_url AS Optional<Utf8>;
            DECLARE $public_url AS Optional<Utf8>;
            DECLARE $pass_url AS Optional<Utf8>;
            DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET master_table_google_url = COALESCE($master_url, master_table_google_url),
                public_table_google_url = COALESCE($public_url, public_table_google_url),
                pass_table_google_url = COALESCE($pass_url, pass_table_google_url),
                updated_at = $updated_at
            WHERE event_id = $event_id
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$master_url": master_url,
                "$public_url": public_url,
                "$pass_url": pass_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

    async def set_achievement(self, event_id: str, resource_path: str, public_url: str) -> bool:
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $resource_path AS Utf8;
            DECLARE $public_url AS Utf8; DECLARE $updated_at AS Timestamp;
            UPDATE `events`
            SET achievement_resource_path = $resource_path,
                achievement_public_url = $public_url,
                updated_at = $updated_at
            WHERE event_id = $event_id AND achievement_public_url IS NULL
            RETURNING event_id;
            """,
            {
                "$event_id": event_id,
                "$resource_path": resource_path,
                "$public_url": public_url,
                "$updated_at": datetime.now(UTC),
            },
        )
        return bool(rows)

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
        rows = await self.db.query(
            """
            DECLARE $event_id AS Utf8;
            DELETE FROM `event_leaders` WHERE event_id = $event_id;
            DELETE FROM `events` WHERE event_id = $event_id
            RETURNING event_id;
            """,
            {"$event_id": event_id},
        )
        return bool(rows)

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
