from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from larp_bot.domain.models import BotIdentity, BotResponse, InboundMessage, Platform

from .callbacks import decode_callback, encode_callback


class InvalidTelegramUpdate(ValueError):
    pass


@dataclass(frozen=True)
class TelegramGroupUpdate:
    update_id: str
    action: str
    chat_id: int
    actor_user_id: int | None = None
    message_id: int | None = None
    callback_query_id: str | None = None
    callback_data: str | None = None
    member_user_id: int | None = None
    member_is_bot: bool = False
    member_username: str | None = None


def _chat_id(value: object) -> int | None:
    if not isinstance(value, dict) or value.get("type") not in {"group", "supergroup"}:
        return None
    chat_id = value.get("id")
    return chat_id if type(chat_id) is int else None


def _member_can_send(member: object) -> bool:
    if not isinstance(member, dict):
        return False
    if member.get("status") == "creator":
        return True
    return member.get("status") == "administrator" and member.get("can_manage_chat") is True


def parse_telegram_group_update(update: dict[str, Any]) -> TelegramGroupUpdate | None:
    """Parse group-only updates before the private conversation adapter sees them."""

    update_id = update.get("update_id")
    if type(update_id) is not int:
        raise InvalidTelegramUpdate("update_id is required")
    serialized_update_id = str(update_id)

    membership = update.get("my_chat_member")
    if isinstance(membership, dict):
        chat_id = _chat_id(membership.get("chat"))
        if chat_id is None:
            return TelegramGroupUpdate(serialized_update_id, "ignored", 0)
        granted = _member_can_send(membership.get("new_chat_member")) and not _member_can_send(
            membership.get("old_chat_member")
        )
        return TelegramGroupUpdate(
            serialized_update_id,
            "bot_admin_granted" if granted else "ignored",
            chat_id,
        )

    membership = update.get("chat_member")
    if isinstance(membership, dict):
        chat_id = _chat_id(membership.get("chat"))
        if chat_id is None:
            return TelegramGroupUpdate(serialized_update_id, "ignored", 0)
        old_member = membership.get("old_chat_member")
        new_member = membership.get("new_chat_member")
        if not isinstance(old_member, dict) or not isinstance(new_member, dict):
            raise InvalidTelegramUpdate("invalid chat_member")
        old_status = old_member.get("status")
        new_status = new_member.get("status")
        was_present = old_status not in {"left", "kicked"} and not (
            old_status == "restricted" and old_member.get("is_member") is False
        )
        is_present = new_status == "member" or (new_status == "restricted" and new_member.get("is_member") is True)
        joined = not was_present and is_present
        member = new_member.get("user")
        if joined and isinstance(member, dict) and type(member.get("id")) is int:
            return TelegramGroupUpdate(
                serialized_update_id,
                "member_joined",
                chat_id,
                member_user_id=member["id"],
                member_is_bot=member.get("is_bot") is True,
                member_username=member.get("username") if isinstance(member.get("username"), str) else None,
            )
        return TelegramGroupUpdate(serialized_update_id, "ignored", chat_id)

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        if not isinstance(message, dict):
            return None
        chat_id = _chat_id(message.get("chat"))
        if chat_id is None:
            return None
        sender = callback.get("from")
        user_id = sender.get("id") if isinstance(sender, dict) else None
        data = callback.get("data")
        message_id = message.get("message_id")
        callback_query_id = callback.get("id")
        if (
            type(user_id) is not int
            or not isinstance(data, str)
            or type(message_id) is not int
            or not isinstance(callback_query_id, str)
        ):
            raise InvalidTelegramUpdate("invalid group callback_query")
        return TelegramGroupUpdate(
            serialized_update_id,
            "tie_callback" if data.startswith("tie:") else "ignored",
            chat_id,
            actor_user_id=user_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
            callback_data=data,
        )

    message = update.get("message")
    if isinstance(message, dict):
        chat_id = _chat_id(message.get("chat"))
        if chat_id is None:
            return None
        sender = message.get("from")
        user_id = sender.get("id") if isinstance(sender, dict) else None
        text = message.get("text")
        message_id = message.get("message_id")
        is_tie = isinstance(text, str) and re.fullmatch(r"/tie(?:@[A-Za-z0-9_]{5,32})?", text.strip()) is not None
        if is_tie and type(user_id) is int:
            return TelegramGroupUpdate(
                serialized_update_id,
                "tie_command",
                chat_id,
                actor_user_id=user_id,
                message_id=message_id if type(message_id) is int else None,
            )
        return TelegramGroupUpdate(serialized_update_id, "ignored", chat_id)
    return None


def parse_telegram_update(update: dict[str, Any]) -> InboundMessage:
    update_id = update.get("update_id")
    if type(update_id) is not int:
        raise InvalidTelegramUpdate("update_id is required")
    if "callback_query" in update:
        callback = update["callback_query"]
        sender = callback.get("from", {})
        message = callback.get("message", {})
        chat = message.get("chat", {})
        user_id = sender.get("id")
        data = callback.get("data")
        chat_id = chat.get("id", user_id)
        if type(user_id) is not int or not isinstance(data, str) or type(chat_id) is not int:
            raise InvalidTelegramUpdate("invalid callback_query")
        return InboundMessage(
            identity=BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id),
            update_id=str(update_id),
            callback=decode_callback(data),
            chat_id=chat_id,
            telegram_username=sender.get("username"),
        )
    message = update.get("message")
    if not isinstance(message, dict):
        raise InvalidTelegramUpdate("only message and callback_query updates are supported")
    sender = message.get("from", {})
    chat = message.get("chat", {})
    user_id = sender.get("id")
    chat_id = chat.get("id")
    message_text = message.get("text")
    if type(user_id) is not int or type(chat_id) is not int:
        raise InvalidTelegramUpdate("invalid message")
    if message_text is not None and not isinstance(message_text, str):
        raise InvalidTelegramUpdate("invalid message text")
    return InboundMessage(
        identity=BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id),
        update_id=str(update_id),
        text=message_text or "",
        has_unsupported_content=message_text is None,
        chat_id=chat_id,
        telegram_username=sender.get("username"),
    )


def telegram_inline_payload(response: BotResponse, chat_id: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": response.text,
    }
    if response.buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": button.label, "callback_data": encode_callback(button.value)}] for button in response.buttons
            ]
        }
    return payload
