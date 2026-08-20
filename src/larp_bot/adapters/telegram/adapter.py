from __future__ import annotations

from typing import Any

from larp_bot.domain.models import BotIdentity, BotResponse, InboundMessage, Platform


class InvalidTelegramUpdate(ValueError):
    pass


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
            callback=data,
            chat_id=chat_id,
        )
    message = update.get("message")
    if not isinstance(message, dict):
        raise InvalidTelegramUpdate("only message and callback_query updates are supported")
    sender = message.get("from", {})
    chat = message.get("chat", {})
    user_id = sender.get("id")
    chat_id = chat.get("id")
    message_text = message.get("text")
    if type(user_id) is not int or type(chat_id) is not int or not isinstance(message_text, str):
        raise InvalidTelegramUpdate("invalid message")
    return InboundMessage(
        identity=BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id),
        update_id=str(update_id),
        text=message_text,
        chat_id=chat_id,
    )


def telegram_inline_payload(response: BotResponse, chat_id: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": response.text,
    }
    if response.buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button.label, "callback_data": button.value}] for button in response.buttons]
        }
    return payload
