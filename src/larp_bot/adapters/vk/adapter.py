from __future__ import annotations

import json
from typing import Any

from larp_bot.domain.models import BotIdentity, InboundMessage, Platform


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
    return InboundMessage(
        identity=BotIdentity(platform=Platform.VK, platform_user_id=from_id),
        update_id=event_id,
        text=message_text,
        callback=callback,
        peer_id=peer_id,
    )


def vk_keyboard(buttons: list[Any]) -> str | None:
    if not buttons:
        return None
    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": button.label,
                            "payload": json.dumps({"value": button.value}, ensure_ascii=False),
                        },
                        "color": "primary",
                    }
                ]
                for button in buttons
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
