from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from larp_bot.adapters.lockbox import LockboxConfigProvider
from larp_bot.functions.bootstrap import iam_token_from_context


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        self.requests.append((url, headers))
        return FakeResponse({"entries": [{"key": "VALUE", "textValue": "secret"}]})


def test_iam_token_is_read_from_function_context() -> None:
    assert iam_token_from_context(SimpleNamespace(token={"access_token": "context-token"})) == "context-token"
    assert iam_token_from_context(SimpleNamespace(token=SimpleNamespace(access_token="attribute-token"))) == (
        "attribute-token"
    )
    assert iam_token_from_context(SimpleNamespace(token={})) is None


@pytest.mark.asyncio
async def test_lockbox_uses_context_token_without_metadata_request() -> None:
    client = FakeClient()
    provider = LockboxConfigProvider(
        "secret-id",
        client=client,  # type: ignore[arg-type]
        iam_token="context-token",
    )

    assert await provider.get_secret("VALUE") == "secret"
    assert client.requests == [
        (
            "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets/secret-id/payload",
            {"Authorization": "Bearer context-token"},
        )
    ]


@pytest.mark.asyncio
async def test_lockbox_falls_back_to_metadata_without_function_context() -> None:
    metadata_url = LockboxConfigProvider.METADATA_TOKEN_URL

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == metadata_url:
            return httpx.Response(200, json={"access_token": "metadata-token"})
        assert request.headers["Authorization"] == "Bearer metadata-token"
        return httpx.Response(200, json={"entries": [{"key": "VALUE", "textValue": "secret"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LockboxConfigProvider("secret-id", client=client)
        assert await provider.get_secret("VALUE") == "secret"
