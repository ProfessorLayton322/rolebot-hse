from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from larp_bot.adapters.transports.deferred import CloudflareTelegramEgress, VkApiTransport
from larp_bot.domain.models import Button, UnreachableRecipient


@pytest.mark.asyncio
async def test_vk_transport_sends_keyboard_when_chatbot_feature_is_enabled() -> None:
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"response": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        await transport.send(
            user_id=42,
            request_id="request-1",
            text="Choose",
            buttons=[Button(label="Visible", value="callback:value")],
        )

    assert len(requests) == 1
    assert "keyboard" in requests[0]
    assert requests[0]["message"] == ["Choose"]


@pytest.mark.asyncio
async def test_vk_transport_retries_with_text_commands_when_chatbot_feature_is_disabled() -> None:
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode()))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 912,
                        "error_msg": "This is a chat bot feature",
                    }
                },
            )
        return httpx.Response(200, json={"response": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        await transport.send(
            user_id=42,
            request_id="request-2",
            text="Choose",
            buttons=[
                Button(label="Same", value="Same"),
                Button(label="Visible", value="callback:value"),
            ],
        )

    assert len(requests) == 2
    assert "keyboard" in requests[0]
    assert "keyboard" not in requests[1]
    assert requests[0]["random_id"] == requests[1]["random_id"]
    fallback = requests[1]["message"][0]
    assert fallback.startswith("Choose\n\nДоступные команды")
    assert "• Same" in fallback
    assert "• Visible\n  Команда: callback:value" in fallback


@pytest.mark.asyncio
async def test_vk_transport_preserves_non_chatbot_api_error_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"error_code": 15, "error_msg": "Access denied"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        with pytest.raises(RuntimeError, match="VK API error code 15: Access denied"):
            await transport.send(user_id=42, request_id="request-3", text="Hello")


@pytest.mark.asyncio
async def test_vk_profile_resolver_uses_numeric_profile_without_api_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        user_id = await transport.resolve_user_id("https://vk.ru/id42")

    assert user_id == 42


@pytest.mark.asyncio
async def test_vk_profile_resolver_resolves_vanity_profile() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"response": [{"id": 84}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        user_id = await transport.resolve_user_id("vk.com/game_master")

    assert user_id == 84
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/users.get")
    assert parse_qs(requests[0].content.decode())["user_ids"] == ["game_master"]


@pytest.mark.asyncio
async def test_vk_transport_reports_a_closed_conversation_as_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 901,
                    "error_msg": "Can't send messages for users without permission",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        with pytest.raises(UnreachableRecipient, match="vk 42"):
            await transport.send(user_id=42, request_id="request-4", text="Hello")


@pytest.mark.asyncio
async def test_telegram_egress_reports_a_blocked_recipient_as_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            403,
            json={
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        with pytest.raises(UnreachableRecipient, match="telegram 42"):
            await egress.send(user_id=42, request_id="request-5", text="Hello")


@pytest.mark.asyncio
async def test_telegram_egress_keeps_egress_worker_rejections_retryable() -> None:
    # The Worker answers its own signature and payload failures with plain text.
    # Those must stay retryable instead of dropping a notification silently.
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, text="Forbidden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await egress.send(user_id=42, request_id="request-6", text="Hello")


@pytest.mark.asyncio
async def test_telegram_egress_keeps_malformed_message_rejections_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: message is too long"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await egress.send(user_id=42, request_id="request-7", text="Hello")
