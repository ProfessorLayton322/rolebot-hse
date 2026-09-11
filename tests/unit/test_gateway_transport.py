from __future__ import annotations

import functools
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from larp_bot.adapters.memory import MemoryDeferredTransport
from larp_bot.adapters.runtime_config import RuntimeConfigProvider
from larp_bot.adapters.telegram import parse_telegram_update
from larp_bot.adapters.vk import parse_vk_event
from larp_bot.domain.models import BotResponse, Button
from larp_bot.domain.security import sign_request
from larp_bot.functions.gateway.handler import (
    _telegram,
    _verify_cloudflare,
    _vk,
    _vk_confirmation,
    _vk_confirmation_from_config,
    async_handler,
)


class FakeConfig:
    def __init__(self) -> None:
        self.values = {
            "CF_TO_YANDEX_HMAC_SECRET": "transport-secret",
            "VK_CALLBACK_SECRET": "vk-secret",
            "VK_GROUP_ID": "42",
            "VK_CONFIRMATION_STRING": "confirmation-value",
        }

    async def get_secret(self, key: str) -> str:
        return self.values[key]


class FakeConversation:
    def __init__(self, response: BotResponse) -> None:
        self.response = response
        self.calls = 0

    async def handle(self, message: Any) -> BotResponse:
        self.calls += 1
        return self.response


def telegram_body() -> bytes:
    return json.dumps(
        {
            "update_id": 10,
            "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "/start"},
        }
    ).encode()


def test_telegram_username_is_collected_when_available() -> None:
    inbound = parse_telegram_update(
        {
            "update_id": 10,
            "callback_query": {
                "from": {"id": 1, "username": "ivan_player"},
                "message": {"chat": {"id": 1}},
                "data": "enlist:wish-play:skip",
            },
        }
    )

    assert inbound.telegram_username == "ivan_player"


@pytest.mark.parametrize(
    "content",
    [
        {"sticker": {"file_id": "sticker-id"}},
        {"photo": [{"file_id": "photo-id"}]},
        {"location": {"latitude": 55.75, "longitude": 37.62}},
        {"audio": {"file_id": "audio-id"}},
        {"document": {"file_id": "document-id"}},
    ],
)
def test_telegram_media_is_marked_as_unsupported(content: dict[str, Any]) -> None:
    inbound = parse_telegram_update(
        {
            "update_id": 10,
            "message": {
                "from": {"id": 1, "username": "ivan_player"},
                "chat": {"id": 1},
                **content,
            },
        }
    )

    assert inbound.has_unsupported_content
    assert inbound.text == ""


def test_telegram_photo_caption_is_not_treated_as_plain_text() -> None:
    inbound = parse_telegram_update(
        {
            "update_id": 10,
            "message": {
                "from": {"id": 1},
                "chat": {"id": 1},
                "photo": [{"file_id": "photo-id"}],
                "caption": "Это не ответ на вопрос бота",
            },
        }
    )

    assert inbound.has_unsupported_content


@pytest.mark.parametrize(
    "content",
    [
        {"attachments": [{"type": "photo", "photo": {"id": 1}}]},
        {"attachments": [{"type": "sticker", "sticker": {"sticker_id": 1}}]},
        {"attachments": [{"type": "audio", "audio": {"id": 1}}]},
        {"attachments": [{"type": "doc", "doc": {"id": 1}}]},
        {"geo": {"coordinates": {"latitude": 55.75, "longitude": 37.62}}},
    ],
)
def test_vk_media_is_marked_as_unsupported(content: dict[str, Any]) -> None:
    inbound = parse_vk_event(
        {
            "event_id": "event-1",
            "object": {
                "message": {
                    "from_id": 1,
                    "peer_id": 1,
                    "text": "Подпись к вложению",
                    **content,
                }
            },
        }
    )

    assert inbound.has_unsupported_content


def signed_event(body: bytes, *, deadline_ms: int, timestamp: int | None = None) -> dict[str, Any]:
    request_id = "request-1"
    stamp = str(timestamp or int(time.time()))
    path = "/webhooks/telegram"
    signature = sign_request("transport-secret", stamp, request_id, "POST", path, body)
    return {
        "path": path,
        "headers": {
            "X-Gateway-Request-Id": request_id,
            "X-Gateway-Timestamp": stamp,
            "X-Gateway-Signature": signature,
            "X-Telegram-Inline-Deadline-Ms": str(deadline_ms),
        },
    }


def fake_app(response: BotResponse) -> Any:
    return SimpleNamespace(
        config=FakeConfig(),
        conversation=FakeConversation(response),
        transport=MemoryDeferredTransport(),
        settings=SimpleNamespace(inline_safety_margin_ms=100),
    )


def test_bad_cloudflare_signature_is_rejected() -> None:
    body = telegram_body()
    event = signed_event(body, deadline_ms=int(time.time() * 1000) + 1000)
    event["headers"]["X-Gateway-Signature"] = "bad"
    with pytest.raises(PermissionError):
        _verify_cloudflare(event, body, "transport-secret")


def test_stale_cloudflare_signature_is_rejected() -> None:
    body = telegram_body()
    event = signed_event(body, deadline_ms=1, timestamp=10)
    with pytest.raises(PermissionError):
        _verify_cloudflare(event, body, "transport-secret", now=100)


def test_body_integrity_is_verified() -> None:
    body = telegram_body()
    event = signed_event(body, deadline_ms=1)
    with pytest.raises(PermissionError):
        _verify_cloudflare(event, body + b" ", "transport-secret")


@pytest.mark.asyncio
async def test_fast_response_is_inline_only() -> None:
    body = telegram_body()
    app = fake_app(BotResponse(text="hello", buttons=[Button(label="A", value="a")]))
    event = signed_event(body, deadline_ms=int(time.time() * 1000) + 10_000)
    result = await _telegram(event, body, app)
    contract = json.loads(result["body"])
    assert contract["delivery"] == "inline"
    assert app.transport.sent == []


@pytest.mark.asyncio
async def test_expired_deadline_is_deferred_only() -> None:
    body = telegram_body()
    app = fake_app(BotResponse(text="hello"))
    event = signed_event(body, deadline_ms=int(time.time() * 1000) - 1)
    result = await _telegram(event, body, app)
    assert json.loads(result["body"])["delivery"] == "deferred"
    assert len(app.transport.sent) == 1


@pytest.mark.asyncio
async def test_ordered_command_ack_is_never_inline() -> None:
    body = telegram_body()
    app = fake_app(BotResponse(text="queued", deferred=True, command_enqueued=True))
    event = signed_event(body, deadline_ms=int(time.time() * 1000) + 10_000)
    result = await _telegram(event, body, app)
    assert json.loads(result["body"])["delivery"] == "deferred"
    assert len(app.transport.sent) == 1


@pytest.mark.asyncio
async def test_vk_confirmation_and_authentication() -> None:
    app = fake_app(BotResponse(text="unused"))
    body = json.dumps({"type": "confirmation", "group_id": 42, "secret": "vk-secret"}).encode()
    result = await _vk(body, app)
    assert result["body"] == "confirmation-value"
    bad = await _vk(
        json.dumps({"type": "confirmation", "group_id": 43, "secret": "vk-secret"}).encode(),
        app,
    )
    assert bad["statusCode"] == 403
    bad_secret = await _vk(
        json.dumps({"type": "confirmation", "group_id": 42, "secret": "wrong"}).encode(),
        app,
    )
    assert bad_secret["statusCode"] == 403


@pytest.mark.asyncio
async def test_obviously_unauthenticated_vk_request_does_not_initialize_container() -> None:
    event = {
        "httpMethod": "POST",
        "path": "/webhooks/vk",
        "headers": {"Content-Type": "application/json"},
        "body": "{}",
        "isBase64Encoded": False,
    }
    with patch(
        "larp_bot.functions.gateway.handler._container_instance",
        new_callable=AsyncMock,
    ) as container_instance:
        result = await async_handler(event, SimpleNamespace(token={}))

    assert result["statusCode"] == 403
    container_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_vk_confirmation_reads_only_runtime_config() -> None:
    event = {"type": "confirmation", "group_id": 42, "secret": "vk-secret"}
    result = await _vk_confirmation_from_config(event, FakeConfig())

    assert result["statusCode"] == 200
    assert result["body"] == "confirmation-value"


@pytest.mark.asyncio
async def test_vk_confirmation_route_does_not_initialize_container() -> None:
    event = {
        "httpMethod": "POST",
        "path": "/webhooks/vk",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"type": "confirmation", "group_id": 42, "secret": "vk-secret"}),
        "isBase64Encoded": False,
    }
    confirmation_response = {"statusCode": 200, "headers": {}, "body": "confirmation-value"}
    with (
        patch(
            "larp_bot.functions.gateway.handler._vk_confirmation",
            new_callable=AsyncMock,
            return_value=confirmation_response,
        ) as confirmation,
        patch(
            "larp_bot.functions.gateway.handler._container_instance",
            new_callable=AsyncMock,
        ) as container_instance,
    ):
        result = await async_handler(event, SimpleNamespace(token={}))

    assert result == confirmation_response
    confirmation.assert_awaited_once()
    container_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_vk_confirmations_share_one_cached_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == RuntimeConfigProvider.TOKEN_EXCHANGE_URL:
            return httpx.Response(200, json={"access_token": "identity-token"})
        return httpx.Response(200, json={"values": FakeConfig().values})

    monkeypatch.setenv("RUNTIME_CONFIG_URL", "https://config.example/runtime/config")
    monkeypatch.setenv("RUNTIME_CONFIG_AUDIENCE", "https://config.example/runtime/config")
    monkeypatch.setenv("RUNTIME_SERVICE_ACCOUNT_ID", "service-account-id")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with (
            patch("larp_bot.functions.gateway.handler._confirmation_config", None),
            patch(
                "larp_bot.functions.gateway.handler.RuntimeConfigProvider",
                functools.partial(RuntimeConfigProvider, client=client),
            ),
        ):
            context = SimpleNamespace(token={"access_token": "context-token"})
            forged = {"type": "confirmation", "group_id": 42, "secret": "forged"}
            for _ in range(3):
                assert (await _vk_confirmation(forged, context))["statusCode"] == 403
            genuine = {"type": "confirmation", "group_id": 42, "secret": "vk-secret"}
            result = await _vk_confirmation(genuine, context)

    assert result["body"] == "confirmation-value"
    # One identity exchange and one runtime-config fetch serve every callback in the cache window.
    assert len(requests) == 2
