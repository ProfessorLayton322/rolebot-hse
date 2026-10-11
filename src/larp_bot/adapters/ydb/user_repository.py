"""YDB persistence for Telegram and VK user profiles."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from larp_bot.domain.models import Button, Platform, PrivilegedRole, TelegramUser, User, UserPassPreference, VkUser

from .common import (
    YdbExecutor,
    _buttons,
    _context,
    _dt,
    _optional_pass,
)


class YdbUserRepository:
    COMMON_COLUMNS = """
        full_name, nickname, crossplay, larp_experience, needs_pass, pass_details_json,
        dialog_state, dialog_context_json, last_update_id, last_update_at,
        last_delivery_operation_id, last_bot_buttons_json, is_gamemaster, privileged_role,
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
            "privileged_role": row.get("privileged_role"),
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
                SELECT tg_id FROM `tg_users` VIEW telegram_identity
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
            FROM `tg_users` VIEW telegram_identity
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
            FROM `tg_users` VIEW telegram_identity WHERE telegram_handle = $handle
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

    async def list_pass_preferences(self) -> Sequence[UserPassPreference]:
        # Keep each result small and complete; a growing user table must not
        # silently lose participants to the table SDK's result-size limit.
        page_size = 500

        async def scan(platform: Platform) -> list[UserPassPreference]:
            table, id_column, _ = self._location(platform)
            link_column = ", linked_vk_id" if platform is Platform.TELEGRAM else ""
            after = 0
            profiles: list[UserPassPreference] = []
            while True:
                rows = await self.db.query(
                    f"DECLARE $after AS Uint64; "
                    f"SELECT {id_column}, needs_pass, telegram_handle{link_column}, updated_at "
                    f"FROM `{table}` WHERE {id_column} > $after ORDER BY {id_column} LIMIT {page_size};",
                    {"$after": after},
                    read_only=True,
                )
                profiles.extend(
                    UserPassPreference(
                        platform=platform,
                        user_id=int(row[id_column]),
                        needs_pass=row.get("needs_pass"),
                        telegram_handle=row.get("telegram_handle"),
                        linked_vk_id=row.get("linked_vk_id"),
                        updated_at=_dt(row["updated_at"]),
                    )
                    for row in rows
                )
                if len(rows) < page_size:
                    return profiles
                after = int(rows[-1][id_column])

        telegram, vk = await asyncio.gather(scan(Platform.TELEGRAM), scan(Platform.VK))
        return [*telegram, *vk]

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

    async def set_privileged_role(
        self,
        platform: Platform,
        user_id: int,
        role: PrivilegedRole,
        operation_id: str,
    ) -> None:
        table, id_column, _ = self._location(platform)
        await self.db.query(
            f"""
            DECLARE $user_id AS Uint64;
            DECLARE $role AS Utf8;
            DECLARE $operation_id AS Utf8;
            UPDATE `{table}`
            SET is_gamemaster = TRUE,
                privileged_role = $role,
                gamemaster_grant_operation_id = $operation_id
            WHERE {id_column} = $user_id
              AND (
                privileged_role IS NULL
                OR privileged_role != $role
                OR gamemaster_grant_operation_id IS NULL
                OR gamemaster_grant_operation_id != $operation_id
              );
            """,
            {"$user_id": user_id, "$role": role.value, "$operation_id": operation_id},
        )

    async def clear_privileged_role(self, platform: Platform, user_id: int, operation_id: str) -> None:
        table, id_column, _ = self._location(platform)
        await self.db.query(
            f"""
            DECLARE $user_id AS Uint64;
            DECLARE $role AS Optional<Utf8>;
            DECLARE $operation_id AS Utf8;
            UPDATE `{table}`
            SET is_gamemaster = FALSE,
                privileged_role = $role,
                gamemaster_grant_operation_id = $operation_id
            WHERE {id_column} = $user_id
              AND (is_gamemaster = TRUE OR privileged_role IS NOT NULL);
            """,
            {"$user_id": user_id, "$role": None, "$operation_id": operation_id},
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
