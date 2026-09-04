from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from larp_bot.domain.models import Platform


class RuntimeConfigProvider:
    """Fetch short-lived runtime configuration from the Cloudflare egress Worker."""

    TOKEN_EXCHANGE_URL = "https://auth.yandex.cloud/oauth/token"
    METADATA_TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(
        self,
        config_url: str,
        audience: str,
        service_account_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        cache_seconds: float = 60.0,
        iam_token: str | None = None,
    ) -> None:
        self.config_url = config_url
        self.audience = audience
        self.service_account_id = service_account_id
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
        if not isinstance(token, str) or not token:
            raise RuntimeError("metadata service returned no IAM access token")
        return token

    async def _identity_token(self) -> str:
        iam_token = await self._iam_token()
        response = await self.client.post(
            self.TOKEN_EXCHANGE_URL,
            data={
                "actor_token": iam_token,
                "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": self.audience,
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "requested_token_type": "urn:ietf:params:oauth:token-type:id_token",
                "subject_token": self.service_account_id,
                "subject_token_type": "urn:yandex-cloud:token-type:subject_id",
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token") or payload.get("id_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Yandex identity exchange returned no ID token")
        return token

    async def _refresh(self) -> None:
        async with self._lock:
            if time.monotonic() < self._expires_at:
                return
            token = await self._identity_token()
            response = await self.client.get(
                self.config_url,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            values: Any = response.json().get("values")
            if not isinstance(values, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not value for key, value in values.items()
            ):
                raise RuntimeError("runtime config endpoint returned an invalid payload")
            self._values = values
            self._expires_at = time.monotonic() + self.cache_seconds

    async def get_secret(self, key: str) -> str:
        if time.monotonic() >= self._expires_at:
            await self._refresh()
        try:
            return self._values[key]
        except KeyError as exc:
            raise RuntimeError(f"runtime config entry is missing: {key}") from exc

    async def _id_is_configured(self, platform: Platform, user_id: int, *, role: str) -> bool:
        prefix = "TG" if platform is Platform.TELEGRAM else "VK"
        key = f"{prefix}_{role}_IDS"
        raw = await self.get_secret(key)
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{key} must be a JSON numeric array") from exc
        if not isinstance(values, list) or any(type(item) is not int for item in values):
            raise RuntimeError(f"{key} must be a JSON numeric array")
        return user_id in values

    async def is_admin(self, platform: Platform, user_id: int) -> bool:
        return await self._id_is_configured(platform, user_id, role="ADMIN")

    async def is_gamemaster(self, platform: Platform, user_id: int) -> bool:
        return await self._id_is_configured(platform, user_id, role="GAMEMASTER")
