from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import cast
from urllib.parse import urlsplit

import httpx

from larp_bot.domain.models import Button, Platform, UnreachableRecipient, normalize_vk_url
from larp_bot.domain.security import sign_request

# Telegram reports a permanently undeliverable chat with these texts. Any other
# rejection stays an error so a real defect, a throttle, or an outage is retried
# instead of silently dropping a notification.
TELEGRAM_UNREACHABLE_DESCRIPTIONS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation with a user",
)


def _telegram_error_description(response: httpx.Response) -> str | None:
    """Return the Telegram API error text that the egress Worker proxied.

    The Worker answers its own authentication and payload failures with plain
    text, so only a Telegram JSON error body may classify a recipient.
    """

    if "application/json" not in response.headers.get("Content-Type", ""):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return None
    description = payload.get("description")
    return description if isinstance(description, str) else None


class CloudflareTelegramEgress:
    PATH = "/telegram/send"

    def __init__(
        self,
        url: str,
        hmac_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url.rstrip("/") + self.PATH
        self.hmac_secret = hmac_secret
        self.client = client or httpx.AsyncClient(timeout=10.0)

    async def send(
        self,
        *,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        payload: dict[str, object] = {"chat_id": user_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": button.label, "callback_data": button.value}] for button in buttons]
            }
        body = json.dumps(
            {
                "request_id": request_id,
                "method": "sendMessage",
                "payload": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(time.time()))
        signature = sign_request(self.hmac_secret, timestamp, request_id, "POST", self.PATH, body)
        response = await self.client.post(
            self.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": request_id,
                "X-Timestamp": timestamp,
                "X-Signature": signature,
            },
        )
        if response.status_code in {400, 403}:
            description = _telegram_error_description(response)
            if description is not None and any(
                marker in description.casefold() for marker in TELEGRAM_UNREACHABLE_DESCRIPTIONS
            ):
                raise UnreachableRecipient(f"telegram {user_id}: {description}")
        response.raise_for_status()


class VkApiTransport:
    API_BASE = "https://api.vk.com/method"
    CHATBOT_FEATURE_DISABLED = 912
    # 901: the person forbade community messages; 902: their privacy settings
    # forbid this conversation. Both stay refused until the person changes them.
    UNREACHABLE_ERROR_CODES = frozenset({901, 902})

    def __init__(
        self,
        access_token: str,
        api_version: str = "5.199",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token
        self.api_version = api_version
        self.client = client or httpx.AsyncClient(timeout=10.0)

    async def _post(self, method: str, data: dict[str, str | int]) -> dict[str, object]:
        response = await self.client.post(f"{self.API_BASE}/{method}", data=data)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("VK API returned a non-object response")
        return cast(dict[str, object], payload)

    @staticmethod
    def _api_error(payload: dict[str, object]) -> tuple[object, str] | None:
        error = payload.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("error_code", "unknown")
        message = error.get("error_msg", "unknown error")
        return code, str(message)

    @staticmethod
    def _text_keyboard(text: str, buttons: Sequence[Button]) -> str:
        lines = [text, "", "Доступные команды (отправьте нужную строку сообщением):"]
        for button in buttons:
            lines.append(f"• {button.label}")
            if button.value != button.label:
                lines.append(f"  Команда: {button.value}")
        return "\n".join(lines)

    async def send(
        self,
        *,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        random_id = int.from_bytes(hashlib.sha256(request_id.encode()).digest()[:4], "big")
        keyboard = None
        if buttons:
            keyboard = json.dumps(
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
        data: dict[str, str | int] = {
            "access_token": self.access_token,
            "v": self.api_version,
            "peer_id": user_id,
            "random_id": random_id,
            "message": text,
        }
        if keyboard is not None:
            data["keyboard"] = keyboard
        payload = await self._post("messages.send", data)
        error = self._api_error(payload)
        if error is not None and error[0] == self.CHATBOT_FEATURE_DISABLED and keyboard is not None:
            # Community message tokens can send text but cannot enable the VK
            # "Chat bot feature" setting. Keep the bot usable until an owner
            # enables it in the community UI: retry without the keyboard and
            # expose the exact callback values as text commands.
            data.pop("keyboard")
            data["message"] = self._text_keyboard(text, buttons)
            payload = await self._post("messages.send", data)
            error = self._api_error(payload)
        if error is not None and error[0] in self.UNREACHABLE_ERROR_CODES:
            raise UnreachableRecipient(f"vk {user_id}: {error[1]}")
        if error is not None:
            raise RuntimeError(f"VK API error code {error[0]}: {error[1]}")

    async def resolve_user_id(self, profile: str) -> int | None:
        normalized = normalize_vk_url(profile)
        screen_name = urlsplit(normalized).path.strip("/")
        if screen_name.startswith("id") and screen_name[2:].isdigit():
            user_id = int(screen_name[2:])
            return user_id if user_id > 0 else None
        payload = await self._post(
            "users.get",
            {
                "access_token": self.access_token,
                "v": self.api_version,
                "user_ids": screen_name,
            },
        )
        error = self._api_error(payload)
        if error is not None:
            raise RuntimeError(f"VK API error code {error[0]}: {error[1]}")
        response = payload.get("response")
        if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
            return None
        resolved_id = response[0].get("id")
        if not isinstance(resolved_id, int) or isinstance(resolved_id, bool) or resolved_id <= 0:
            return None
        return resolved_id


class MultiplexedDeferredTransport:
    def __init__(self, telegram: CloudflareTelegramEgress, vk: VkApiTransport) -> None:
        self.telegram = telegram
        self.vk = vk

    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        if platform is Platform.TELEGRAM:
            await self.telegram.send(user_id=user_id, request_id=request_id, text=text, buttons=buttons)
        elif platform is Platform.VK:
            await self.vk.send(user_id=user_id, request_id=request_id, text=text, buttons=buttons)
        else:
            raise ValueError("system commands do not have a bot transport")
