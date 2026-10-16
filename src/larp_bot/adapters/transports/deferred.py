from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import cast
from urllib.parse import urlsplit

import httpx

from larp_bot.adapters.telegram.callbacks import encode_callback
from larp_bot.adapters.vk import vk_keyboard
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
TELEGRAM_ADMINISTRATOR_PERMISSION_FIELDS = (
    "can_manage_chat",
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
    "can_post_messages",
    "can_edit_messages",
    "can_pin_messages",
    "can_manage_topics",
    "can_manage_direct_messages",
    "can_manage_tags",
    "can_send_welcome_messages",
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

    async def _call(
        self,
        *,
        method: str,
        payload: dict[str, object],
        request_id: str,
    ) -> object:
        body = json.dumps(
            {
                "request_id": request_id,
                "method": method,
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
            if (
                method == "sendMessage"
                and description is not None
                and any(marker in description.casefold() for marker in TELEGRAM_UNREACHABLE_DESCRIPTIONS)
            ):
                raise UnreachableRecipient(f"telegram {payload.get('chat_id')}: {description}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram API returned an invalid success response")
        return result.get("result")

    @staticmethod
    def _message_payload(chat_id: int, text: str, buttons: Sequence[Button]) -> dict[str, object]:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.label, "callback_data": encode_callback(button.value)}] for button in buttons
                ]
            }
        return payload

    async def send_message(
        self,
        *,
        chat_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> int:
        result = await self._call(
            method="sendMessage",
            payload=self._message_payload(chat_id, text, buttons),
            request_id=request_id,
        )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise RuntimeError("Telegram sendMessage response has no message_id")
        return cast(int, result["message_id"])

    async def send(
        self,
        *,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        await self.send_message(
            chat_id=user_id,
            request_id=request_id,
            text=text,
            buttons=buttons,
        )

    async def edit_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        payload = self._message_payload(chat_id, text, buttons)
        payload["message_id"] = message_id
        await self._call(method="editMessageText", payload=payload, request_id=request_id)

    async def delete_message(self, *, chat_id: int, message_id: int, request_id: str) -> None:
        await self._call(
            method="deleteMessage",
            payload={"chat_id": chat_id, "message_id": message_id},
            request_id=request_id,
        )

    async def answer_callback(self, *, callback_query_id: str, request_id: str, text: str = "") -> None:
        payload: dict[str, object] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        await self._call(method="answerCallbackQuery", payload=payload, request_id=request_id)

    async def get_chat_member(
        self,
        *,
        chat_id: int,
        user_id: int,
        request_id: str,
    ) -> dict[str, object] | None:
        try:
            result = await self._call(
                method="getChatMember",
                payload={"chat_id": chat_id, "user_id": user_id},
                request_id=request_id,
            )
        except httpx.HTTPStatusError as exc:
            description = _telegram_error_description(exc.response)
            missing_user = description is not None and "user not found" in description.casefold()
            if exc.response.status_code == 400 and missing_user:
                return None
            raise
        if not isinstance(result, dict):
            raise RuntimeError("Telegram getChatMember response is not an object")
        return cast(dict[str, object], result)

    async def ensure_primary_invite_link(self, *, chat_id: int, request_id: str) -> str:
        chat = await self._call(
            method="getChat",
            payload={"chat_id": chat_id},
            request_id=f"{request_id}:get",
        )
        if not isinstance(chat, dict):
            raise RuntimeError("Telegram getChat response is not an object")
        existing = chat.get("invite_link")
        if isinstance(existing, str) and existing:
            return existing
        created = await self._call(
            method="exportChatInviteLink",
            payload={"chat_id": chat_id},
            request_id=f"{request_id}:export",
        )
        if not isinstance(created, str) or not created:
            raise RuntimeError("Telegram exportChatInviteLink response is not a link")
        return created

    async def promote_title_only_admin(self, *, chat_id: int, user_id: int, request_id: str) -> None:
        permissions = {field: False for field in TELEGRAM_ADMINISTRATOR_PERMISSION_FIELDS}
        permissions["can_manage_chat"] = True
        await self._call(
            method="promoteChatMember",
            payload={
                "chat_id": chat_id,
                "user_id": user_id,
                **permissions,
            },
            request_id=request_id,
        )

    async def set_administrator_title(
        self,
        *,
        chat_id: int,
        user_id: int,
        title: str,
        request_id: str,
    ) -> None:
        await self._call(
            method="setChatAdministratorCustomTitle",
            payload={"chat_id": chat_id, "user_id": user_id, "custom_title": title},
            request_id=request_id,
        )

    async def kick_member(self, *, chat_id: int, user_id: int, request_id: str) -> bool:
        member = await self.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            request_id=f"{request_id}:member",
        )
        if member is None:
            return False
        status = member.get("status")
        if status == "kicked":
            await self._call(
                method="unbanChatMember",
                payload={"chat_id": chat_id, "user_id": user_id, "only_if_banned": True},
                request_id=f"{request_id}:unban",
            )
            return True
        if status == "left" or (status == "restricted" and member.get("is_member") is False):
            return False
        if status == "creator" or (status == "administrator" and member.get("can_be_edited") is not True):
            return False
        if status == "administrator":
            permissions = {field: False for field in TELEGRAM_ADMINISTRATOR_PERMISSION_FIELDS}
            await self._call(
                method="promoteChatMember",
                payload={"chat_id": chat_id, "user_id": user_id, **permissions},
                request_id=f"{request_id}:demote",
            )
        await self._call(
            method="banChatMember",
            payload={"chat_id": chat_id, "user_id": user_id, "revoke_messages": False},
            request_id=f"{request_id}:ban",
        )
        await self._call(
            method="unbanChatMember",
            payload={"chat_id": chat_id, "user_id": user_id, "only_if_banned": True},
            request_id=f"{request_id}:unban",
        )
        return True


class VkApiTransport:
    API_BASE = "https://api.vk.com/method"
    REQUEST_ATTEMPTS = 2
    KEYBOARD_ERROR_CODES = frozenset({911, 912})
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
        for attempt in range(self.REQUEST_ATTEMPTS):
            try:
                response = await self.client.post(f"{self.API_BASE}/{method}", data=data)
                break
            except httpx.TimeoutException:
                if attempt + 1 == self.REQUEST_ATTEMPTS:
                    raise
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
        keyboard = vk_keyboard(buttons)
        data: dict[str, str | int] = {
            "access_token": self.access_token,
            "v": self.api_version,
            "peer_id": user_id,
            "random_id": random_id,
            "message": text if keyboard is not None or not buttons else self._text_keyboard(text, buttons),
        }
        if keyboard is not None:
            data["keyboard"] = keyboard
        payload = await self._post("messages.send", data)
        error = self._api_error(payload)
        if error is not None and error[0] in self.KEYBOARD_ERROR_CODES and keyboard is not None:
            # Community message tokens can send text but cannot enable the VK
            # "Chat bot feature" setting. VK may also reject a keyboard when a
            # button violates a constraint not represented in our domain model.
            # Keep the bot usable by retrying without the keyboard and exposing
            # the exact callback values as text commands.
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
