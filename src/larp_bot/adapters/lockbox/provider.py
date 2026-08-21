from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from larp_bot.domain.models import Platform


class LockboxConfigProvider:
    PAYLOAD_API = "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets"
    METADATA_TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(
        self,
        secret_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        cache_seconds: float = 60.0,
        iam_token: str | None = None,
    ) -> None:
        self.secret_id = secret_id
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self.cache_seconds = min(cache_seconds, 60.0)
        self._context_iam_token = iam_token
        self._values: dict[str, str] = {}
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def set_iam_token(self, token: str) -> None:
        self._context_iam_token = token

    async def _iam_token(self) -> str:
        if self._context_iam_token:
            return self._context_iam_token
        response = await self.client.get(self.METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        response.raise_for_status()
        token = response.json().get("access_token")
        if not isinstance(token, str):
            raise RuntimeError("metadata service returned no IAM access token")
        return token

    async def _refresh(self) -> None:
        async with self._lock:
            if time.monotonic() < self._expires_at:
                return
            token = await self._iam_token()
            response = await self.client.get(
                f"{self.PAYLOAD_API}/{self.secret_id}/payload",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            entries: list[dict[str, Any]] = response.json().get("entries", [])
            values = {
                str(entry["key"]): str(entry["textValue"])
                for entry in entries
                if "key" in entry and "textValue" in entry
            }
            self._values = values
            self._expires_at = time.monotonic() + self.cache_seconds

    async def get_secret(self, key: str) -> str:
        if time.monotonic() >= self._expires_at:
            await self._refresh()
        try:
            return self._values[key]
        except KeyError as exc:
            raise RuntimeError(f"Lockbox entry is missing: {key}") from exc

    async def is_admin(self, platform: Platform, user_id: int) -> bool:
        key = "TG_ADMIN_IDS" if platform is Platform.TELEGRAM else "VK_ADMIN_IDS"
        raw = await self.get_secret(key)
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{key} must be a JSON numeric array") from exc
        if not isinstance(values, list) or any(type(item) is not int for item in values):
            raise RuntimeError(f"{key} must be a JSON numeric array")
        return user_id in values
