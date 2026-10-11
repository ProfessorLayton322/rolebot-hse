"""YDB persistence for event registrations."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from larp_bot.domain.models import AttendanceStatus, Registration

from .common import (
    YdbExecutor,
    _dt,
)


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

    async def participation_notification_delivered(self, event_id: str, participant_key: str, request_id: str) -> bool:
        rows = await self.db.query(
            "DECLARE $event_id AS Utf8; DECLARE $participant_key AS Utf8; "
            "SELECT participation_notification_deliveries_json FROM `registrations` "
            "WHERE event_id = $event_id AND participant_key = $participant_key;",
            {"$event_id": event_id, "$participant_key": participant_key},
            read_only=True,
        )
        return bool(
            rows and request_id in json.loads(rows[0].get("participation_notification_deliveries_json") or "[]")
        )

    async def mark_participation_notification_delivered(
        self, event_id: str, participant_key: str, request_id: str
    ) -> None:
        await self.db.mark_participation_notification_delivered(event_id, participant_key, request_id)

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
        queue_time = datetime.now(UTC)
        if registration is None or registration.attendance_status is AttendanceStatus.CANCELLED:
            # Sifting uses created_at as a queue rank and can move it into the future.
            # Event mutations run in FIFO order, so append strictly after its tail.
            rows = await self.db.query(
                """
                DECLARE $event_id AS Utf8; DECLARE $cancelled_status AS Utf8;
                SELECT MAX(created_at) AS latest FROM `registrations`
                WHERE event_id = $event_id AND attendance_status != $cancelled_status;
                """,
                {"$event_id": event_id, "$cancelled_status": AttendanceStatus.CANCELLED.value},
                read_only=True,
            )
            if rows and rows[0]["latest"] is not None:
                queue_time = max(queue_time, _dt(rows[0]["latest"]) + timedelta(microseconds=1))
        if registration is None:
            registration = Registration(
                created_at=queue_time,
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
                registration.created_at = queue_time
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

    async def swap_queue_positions(
        self,
        event_id: str,
        *,
        operation_id: str,
        promoted_participant_key: str,
        promoted_created_at: datetime,
        demoted_participant_key: str,
        demoted_created_at: datetime,
    ) -> bool:
        await self.db.query(
            """
            DECLARE $event_id AS Utf8; DECLARE $operation_id AS Utf8;
            DECLARE $promoted_participant_key AS Utf8; DECLARE $promoted_created_at AS Timestamp;
            DECLARE $demoted_participant_key AS Utf8; DECLARE $demoted_created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPDATE `registrations`
            SET created_at = $demoted_created_at,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $promoted_participant_key
              AND last_operation_id != $operation_id;
            UPDATE `registrations`
            SET created_at = $promoted_created_at,
                last_operation_id = $operation_id,
                updated_at = $updated_at
            WHERE event_id = $event_id AND participant_key = $demoted_participant_key
              AND last_operation_id != $operation_id;
            """,
            {
                "$event_id": event_id,
                "$operation_id": operation_id,
                "$promoted_participant_key": promoted_participant_key,
                "$promoted_created_at": promoted_created_at,
                "$demoted_participant_key": demoted_participant_key,
                "$demoted_created_at": demoted_created_at,
                "$updated_at": datetime.now(UTC),
            },
        )
        return True

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
