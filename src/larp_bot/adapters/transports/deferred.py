from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence

import httpx

from larp_bot.domain.models import Button, Platform
from larp_bot.domain.security import sign_request


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
        response.raise_for_status()


class VkApiTransport:
    API = "https://api.vk.com/method/messages.send"

    def __init__(
        self,
        access_token: str,
        api_version: str = "5.199",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token
        self.api_version = api_version
        self.client = client or httpx.AsyncClient(timeout=10.0)

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
        data = {
            "access_token": self.access_token,
            "v": self.api_version,
            "peer_id": user_id,
            "random_id": random_id,
            "message": text,
        }
        if keyboard is not None:
            data["keyboard"] = keyboard
        response = await self.client.post(
            self.API,
            data=data,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"VK API error code {payload['error'].get('error_code', 'unknown')}")


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
