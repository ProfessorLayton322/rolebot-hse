from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from larp_bot.domain.models import BotIdentity, InboundMessage, Platform

VK_MAX_KEYBOARD_ROWS = 10
VK_MAX_BUTTONS_PER_ROW = 4
VK_MAX_KEYBOARD_BUTTONS = VK_MAX_KEYBOARD_ROWS * VK_MAX_BUTTONS_PER_ROW


class InvalidVkEvent(ValueError):
    pass


def parse_vk_event(event: dict[str, Any]) -> InboundMessage:
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise InvalidVkEvent("event_id is required")
    obj = event.get("object", {})
    message = obj.get("message", obj) if isinstance(obj, dict) else {}
    from_id = message.get("from_id")
    peer_id = message.get("peer_id", from_id)
    message_text = message.get("text", "")
    if type(from_id) is not int or type(peer_id) is not int or not isinstance(message_text, str):
        raise InvalidVkEvent("invalid message_new event")
    callback: str | None = None
    raw_payload = message.get("payload")
    if isinstance(raw_payload, str):
        try:
            decoded = json.loads(raw_payload)
            if isinstance(decoded, dict) and isinstance(decoded.get("value"), str):
                callback = decoded["value"]
        except json.JSONDecodeError:
            callback = None
    has_unsupported_content = (
        bool(message.get("attachments"))
        or message.get("geo") is not None
        or bool(message.get("fwd_messages"))
        or message.get("action") is not None
        or (callback is None and not message_text.strip())
    )
    return InboundMessage(
        identity=BotIdentity(platform=Platform.VK, platform_user_id=from_id),
        update_id=event_id,
        text=message_text,
        callback=callback,
        has_unsupported_content=has_unsupported_content,
        peer_id=peer_id,
    )


def vk_keyboard(buttons: Sequence[Any]) -> str | None:
    if not buttons or len(buttons) > VK_MAX_KEYBOARD_BUTTONS:
        return None
    buttons_per_row = (len(buttons) + VK_MAX_KEYBOARD_ROWS - 1) // VK_MAX_KEYBOARD_ROWS
    rows = []
    for offset in range(0, len(buttons), buttons_per_row):
        rows.append(
            [
                {
                    "action": {
                        "type": "text",
                        "label": button.label,
                        "payload": json.dumps({"value": button.value}, ensure_ascii=False),
                    },
                    "color": "primary",
                }
                for button in buttons[offset : offset + buttons_per_row]
            ]
        )
    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
