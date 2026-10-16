from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from larp_bot.domain.models import BotIdentity, Platform


class SsmRuntimeConfigProvider:
    """Read narrowly-scoped AWS runtime secrets and cache them per warm process."""

    SECRET_NAMES: ClassVar[Mapping[str, str]] = {
        "TG_BOT_TOKEN": "tg-bot-token",
        "TG_BOT_USERNAME": "tg-bot-username",
        "TG_WEBHOOK_SECRET": "tg-webhook-secret",
        "VK_ACCESS_TOKEN": "vk-access-token",
        "VK_CALLBACK_SECRET": "vk-callback-secret",
        "VK_CONFIRMATION_STRING": "vk-confirmation-string",
        "VK_GROUP_ID": "vk-group-id",
        "YANDEX_DISK_TOKEN": "yandex-disk-token",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "google-service-account-json",
        "GOOGLE_APPS_SCRIPT_HMAC_SECRET": "google-apps-script-hmac-secret",
        "PARTICIPANT_KEY_HMAC_SECRET": "participant-key-hmac-secret",
        "CF_TO_AWS_HMAC_SECRET": "cloudflare-ingress-hmac-secret",
        "AWS_TO_CF_EGRESS_HMAC_SECRET": "cloudflare-egress-hmac-secret",
    }
    ENVIRONMENT_NAMES: ClassVar[set[str]] = {
        "TG_ADMIN_IDS",
        "VK_ADMIN_IDS",
        "TG_GAMEMASTER_IDS",
        "VK_GAMEMASTER_IDS",
    }

    def __init__(
        self,
        client: Any,
        parameter_prefix: str,
        *,
        cache_seconds: float = 300.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.client = client
        self.parameter_prefix = parameter_prefix.rstrip("/") + "/"
        self.cache_seconds = cache_seconds
        self.environment = os.environ if environment is None else environment
        self._values: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def set_iam_token(self, token: str) -> None:
        """Compatibility no-op for the Yandex container lifecycle."""

        del token

    async def _refresh(self, key: str) -> None:
        async with self._lock:
            if time.monotonic() < self._expires_at.get(key, 0.0):
                return
            name = self.parameter_prefix + self.SECRET_NAMES[key]
            response = await asyncio.to_thread(self.client.get_parameter, Name=name, WithDecryption=True)
            parameter = response.get("Parameter", {})
            value = parameter.get("Value")
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"runtime parameter is empty: {name}")
            self._values[key] = value
            self._expires_at[key] = time.monotonic() + self.cache_seconds

    async def get_secret(self, key: str) -> str:
        if key in self.ENVIRONMENT_NAMES:
            value = self.environment.get(key)
            if not value:
                raise RuntimeError(f"runtime environment entry is missing: {key}")
            return value
        if key not in self.SECRET_NAMES:
            raise RuntimeError(f"runtime parameter is not allowed: {key}")
        if time.monotonic() >= self._expires_at.get(key, 0.0):
            await self._refresh(key)
        try:
            return self._values[key]
        except KeyError as exc:
            raise RuntimeError(f"runtime parameter is not allowed: {key}") from exc

    async def _configured_ids(self, platform: Platform, *, role: str) -> set[int]:
        prefix = "TG" if platform is Platform.TELEGRAM else "VK"
        key = f"{prefix}_{role}_IDS"
        raw = await self.get_secret(key)
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{key} must be a JSON numeric array") from exc
        if not isinstance(values, list) or any(type(item) is not int or item <= 0 for item in values):
            raise RuntimeError(f"{key} must be a JSON array of positive IDs")
        return set(values)

    async def list_admins(self) -> Sequence[BotIdentity]:
        telegram_ids, vk_ids = await asyncio.gather(
            self._configured_ids(Platform.TELEGRAM, role="ADMIN"),
            self._configured_ids(Platform.VK, role="ADMIN"),
        )
        return [
            *(BotIdentity(platform=Platform.TELEGRAM, platform_user_id=value) for value in sorted(telegram_ids)),
            *(BotIdentity(platform=Platform.VK, platform_user_id=value) for value in sorted(vk_ids)),
        ]

    async def list_gamemasters(self) -> Sequence[BotIdentity]:
        telegram_ids, vk_ids = await asyncio.gather(
            self._configured_ids(Platform.TELEGRAM, role="GAMEMASTER"),
            self._configured_ids(Platform.VK, role="GAMEMASTER"),
        )
        return [
            *(BotIdentity(platform=Platform.TELEGRAM, platform_user_id=value) for value in sorted(telegram_ids)),
            *(BotIdentity(platform=Platform.VK, platform_user_id=value) for value in sorted(vk_ids)),
        ]

    async def is_admin(self, platform: Platform, user_id: int) -> bool:
        return user_id in await self._configured_ids(platform, role="ADMIN")

    async def is_gamemaster(self, platform: Platform, user_id: int) -> bool:
        return user_id in await self._configured_ids(platform, role="GAMEMASTER")
