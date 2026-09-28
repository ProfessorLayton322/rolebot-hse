from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from larp_bot.domain.models import BotIdentity, Button, Event, Platform, event_reference, format_game_date

from .conversation import (
    STATUS_REGISTRATION,
    event_table_links,
    game_management_button,
)
from .deadlines import format_confirmation_deadline
from .ports import AdminConfigProvider, DeferredTransport, EventRepository, UserRepository


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _public_urls(value: object) -> dict[str, str]:
    raw = _mapping(value, "public_urls")
    urls = {key: item for key, item in raw.items() if isinstance(item, str) and item.startswith("https://")}
    if len(urls) != len(raw):
        raise ValueError("public_urls contains an invalid URL")
    return urls


class UploadCompletionService:
    def __init__(
        self,
        events: EventRepository,
        users: UserRepository,
        config: AdminConfigProvider,
        transport: DeferredTransport,
    ) -> None:
        self.events = events
        self.users = users
        self.config = config
        self.transport = transport

    @staticmethod
    def _requester(callback: Mapping[str, Any]) -> tuple[Platform, int, int] | None:
        raw = callback.get("requester")
        if raw is None:
            return None
        requester = _mapping(raw, "requester")
        platform = Platform(str(requester["platform"]))
        user_id = requester["user_id"]
        profile_user_id = requester["profile_user_id"]
        if type(user_id) is not int or type(profile_user_id) is not int:
            raise ValueError("requester IDs must be integers")
        return platform, user_id, profile_user_id

    async def _send(
        self,
        callback: Mapping[str, Any],
        *,
        operation_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        requester = self._requester(callback)
        if requester is None:
            return
        platform, user_id, profile_user_id = requester
        profile = await self.users.get(platform, profile_user_id)
        if profile is not None and profile.last_delivery_operation_id == operation_id:
            return
        await self.transport.send(
            platform=platform,
            user_id=user_id,
            request_id=operation_id,
            text=text,
            buttons=buttons,
        )
        await self.users.claim_delivery(platform, profile_user_id, operation_id, buttons)

    async def _notify_leaders(
        self,
        event: Event,
        leaders: Sequence[BotIdentity],
        requester: tuple[Platform, int, int] | None,
        job_id: str,
    ) -> None:
        requester_identity = None if requester is None else (requester[0], requester[2])
        buttons = [Button(label="Управление игрой", value=f"ag:manage:{event.event_id}")]
        for leader in leaders:
            if (leader.platform, leader.platform_user_id) == requester_identity:
                continue
            profile = await self.users.get(leader.platform, leader.platform_user_id)
            operation_id = f"{job_id}:leader:{leader.platform.value}:{leader.platform_user_id}"
            if profile is not None and profile.last_delivery_operation_id == operation_id:
                continue
            await self.transport.send(
                platform=leader.platform,
                user_id=leader.platform_user_id,
                request_id=operation_id,
                text=f"Вы добавлены мастером игры {event_reference(event.name, event.game_date)}.",
                buttons=buttons,
            )
            await self.users.claim_delivery(leader.platform, leader.platform_user_id, operation_id, buttons)

    async def _event_created(
        self,
        callback: Mapping[str, Any],
        urls: dict[str, str],
        job_id: str,
    ) -> None:
        if set(urls) != {"master", "public", "pass"}:
            raise ValueError("event creation requires master, public, and pass URLs")
        event_data = dict(_mapping(callback.get("event"), "event"))
        event_data.update(
            {
                "public_registration_url": urls["master"],
                "public_table_public_url": urls["public"],
                "pass_table_public_url": urls["pass"],
            }
        )
        event = Event.model_validate(event_data)
        leaders_raw = callback.get("leaders", [])
        if not isinstance(leaders_raw, list):
            raise ValueError("leaders must be a list")
        leaders = [BotIdentity.model_validate(item) for item in leaders_raw]
        existing = await self.events.get(event.event_id)
        if existing is None:
            await self.events.create(event, leaders)
        else:
            event = existing
        requester = self._requester(callback)
        await self._notify_leaders(event, leaders, requester, job_id)

        telegram_username, vk_group_id = (
            await self.config.get_secret("TG_BOT_USERNAME"),
            await self.config.get_secret("VK_GROUP_ID"),
        )
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", telegram_username) is None:
            raise RuntimeError("TG_BOT_USERNAME is invalid")
        if not vk_group_id.isdecimal() or int(vk_group_id) <= 0:
            raise RuntimeError("VK_GROUP_ID is invalid")
        if event.signup_token is None or event.game_date is None:
            raise RuntimeError("new game has no signup token or date")
        master_label = callback.get("master_label")
        masters_pending = callback.get("masters_pending") is True
        master_line = "Мастера: пока не назначены\n\n" if masters_pending else ""
        if isinstance(master_label, str) and master_label:
            master_line = f"Мастер: {master_label}\n\n"
        if event.registration_open_at is None:
            registration_line = (
                f"Статус: {STATUS_REGISTRATION}. Игроки уже могут записываться, но пока не могут подтверждать участие."
            )
        else:
            registration_line = (
                "Открытие регистрации запланировано на "
                f"{format_confirmation_deadline(event.registration_open_at)} (МСК). "
                "До этого времени игра не будет видна игрокам."
            )
        close_line = (
            ""
            if event.scheduled_close_at is None
            else (
                "\n\nАвтоматическое закрытие запланировано на "
                f"{format_confirmation_deadline(event.scheduled_close_at)} (МСК)."
            )
        )
        text = (
            f"✅ Игра создана.\n\nНазвание:\n{event.name}\n\n"
            f"Дата игры: {format_game_date(event.game_date)}\n\n"
            f"Количество игроков: {event.player_amount}\n\n"
            f"{master_line}{registration_line}{close_line}\n\n"
            f"Telegram — начать запись:\nhttps://t.me/{telegram_username}?start={event.signup_token}\n\n"
            f"VK — открыть чат с ботом:\nhttps://vk.me/club{vk_group_id}\n\n"
            f"{event_table_links(event)}"
        )
        await self._send(
            callback,
            operation_id=f"{job_id}:complete",
            text=text,
            buttons=[game_management_button(event.event_id)],
        )

    async def _event_tables(
        self,
        callback: Mapping[str, Any],
        urls: dict[str, str],
        job_id: str,
    ) -> None:
        event_id = str(callback["event_id"])
        component = str(callback["component"])
        path = str(callback["resource_path"])
        expected_role = "public" if component == "public" else "pass"
        if set(urls) != {expected_role}:
            raise ValueError("table completion has unexpected upload results")
        if component == "public":
            await self.events.set_public_table(event_id, path, urls["public"])
        elif component == "pass":
            await self.events.set_pass_table(event_id, path, urls["pass"])
        else:
            raise ValueError("unknown table component")
        event = await self.events.get(event_id)
        if event is None or event.public_table_public_url is None or event.pass_table_public_url is None:
            return
        link_version = hashlib.sha256(
            f"{event.public_table_public_url}\0{event.pass_table_public_url}".encode()
        ).hexdigest()[:16]
        await self._send(
            callback,
            operation_id=f"tables:{event_id}:{link_version}",
            text=f"Таблицы игры {event_reference(event.name, event.game_date)}\n\n{event_table_links(event)}",
            buttons=[game_management_button(event_id)],
        )

    async def _achievement(
        self,
        callback: Mapping[str, Any],
        urls: dict[str, str],
    ) -> None:
        if set(urls) != {"achievement"}:
            raise ValueError("achievement completion has unexpected upload results")
        event_id = str(callback["event_id"])
        path = str(callback["resource_path"])
        await self.events.set_achievement(event_id, path, urls["achievement"])
        event = await self.events.get(event_id)
        if event is None:
            return
        link_version = hashlib.sha256(urls["achievement"].encode()).hexdigest()[:16]
        await self._send(
            callback,
            operation_id=f"achievement:{event_id}:{link_version}",
            text=f"Ачивки игры {event_reference(event.name, event.game_date)}\n\n{urls['achievement']}",
            buttons=[game_management_button(event_id)],
        )

    async def handle(self, payload: Mapping[str, Any]) -> None:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported upload completion schema")
        job_id = str(payload.get("job_id", ""))
        if not job_id:
            raise ValueError("upload completion has no job ID")
        callback = _mapping(payload.get("callback"), "callback")
        if payload.get("success") is not True:
            await self._send(
                callback,
                operation_id=f"{job_id}:failed",
                text="Не удалось загрузить файл. Попробуйте повторить действие позже.",
            )
            return
        urls = _public_urls(payload.get("public_urls"))
        kind = callback.get("kind")
        if kind == "event_created":
            await self._event_created(callback, urls, job_id)
        elif kind == "event_tables":
            await self._event_tables(callback, urls, job_id)
        elif kind == "achievement":
            await self._achievement(callback, urls)
        else:
            raise ValueError("unknown upload completion kind")
