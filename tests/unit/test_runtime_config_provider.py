from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from larp_bot.adapters.runtime_config import RuntimeConfigProvider
from larp_bot.functions.bootstrap import iam_token_from_context


def test_iam_token_is_read_from_function_context() -> None:
    assert iam_token_from_context(SimpleNamespace(token={"access_token": "context-token"})) == "context-token"
    assert iam_token_from_context(SimpleNamespace(token=SimpleNamespace(access_token="attribute-token"))) == (
        "attribute-token"
    )
    assert iam_token_from_context(SimpleNamespace(token={})) is None


@pytest.mark.asyncio
async def test_runtime_config_exchanges_context_token_for_worker_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == RuntimeConfigProvider.TOKEN_EXCHANGE_URL:
            form = parse_qs(request.content.decode())
            assert form == {
                "actor_token": ["context-token"],
                "actor_token_type": ["urn:ietf:params:oauth:token-type:access_token"],
                "audience": ["https://config.example/runtime/config"],
                "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
                "requested_token_type": ["urn:ietf:params:oauth:token-type:id_token"],
                "subject_token": ["service-account-id"],
                "subject_token_type": ["urn:yandex-cloud:token-type:subject_id"],
            }
            return httpx.Response(200, json={"access_token": "identity-token"})
        assert request.headers["Authorization"] == "Bearer identity-token"
        return httpx.Response(200, json={"values": {"VALUE": "secret"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RuntimeConfigProvider(
            "https://config.example/runtime/config",
            "https://config.example/runtime/config",
            "service-account-id",
            client=client,
            iam_token="context-token",
        )
        assert await provider.get_secret("VALUE") == "secret"

    assert all(str(request.url) != RuntimeConfigProvider.METADATA_TOKEN_URL for request in requests)


@pytest.mark.asyncio
async def test_runtime_config_falls_back_to_metadata_without_function_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == RuntimeConfigProvider.METADATA_TOKEN_URL:
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "metadata-token"})
        if str(request.url) == RuntimeConfigProvider.TOKEN_EXCHANGE_URL:
            assert parse_qs(request.content.decode())["actor_token"] == ["metadata-token"]
            return httpx.Response(200, json={"access_token": "identity-token"})
        return httpx.Response(200, json={"values": {"VALUE": "secret"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RuntimeConfigProvider(
            "https://config.example/runtime/config",
            "https://config.example/runtime/config",
            "service-account-id",
            client=client,
        )
        assert await provider.get_secret("VALUE") == "secret"


@pytest.mark.asyncio
async def test_runtime_config_rejects_non_string_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == RuntimeConfigProvider.TOKEN_EXCHANGE_URL:
            return httpx.Response(200, json={"access_token": "identity-token"})
        return httpx.Response(200, json={"values": {"VALUE": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RuntimeConfigProvider(
            "https://config.example/runtime/config",
            "https://config.example/runtime/config",
            "service-account-id",
            client=client,
            iam_token="context-token",
        )
        with pytest.raises(RuntimeError, match="invalid payload"):
            await provider.get_secret("VALUE")
