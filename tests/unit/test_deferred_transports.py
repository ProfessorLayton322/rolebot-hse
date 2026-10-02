from __future__ import annotations

import json
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
async def test_vk_transport_packs_large_keyboard_within_vk_row_limits() -> None:
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"response": 1})

    buttons = [Button(label=f"Button {index}", value=f"callback:{index}") for index in range(40)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        await transport.send(user_id=42, request_id="request-large", text="Choose", buttons=buttons)

    keyboard = json.loads(requests[0]["keyboard"][0])
    rows = keyboard["buttons"]
    assert len(rows) == 10
    assert all(len(row) == 4 for row in rows)
    assert [json.loads(button["action"]["payload"])["value"] for row in rows for button in row] == [
        button.value for button in buttons
    ]


@pytest.mark.asyncio
async def test_vk_transport_sends_oversized_keyboard_as_text_commands() -> None:
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"response": 1})

    buttons = [Button(label=f"Button {index}", value=f"callback:{index}") for index in range(41)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        await transport.send(user_id=42, request_id="request-oversized", text="Choose", buttons=buttons)

    assert len(requests) == 1
    assert "keyboard" not in requests[0]
    assert "Button 40\n  Команда: callback:40" in requests[0]["message"][0]


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
async def test_vk_transport_retries_without_keyboard_when_vk_rejects_its_format() -> None:
    requests: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode()))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"error": {"error_code": 911, "error_msg": "Keyboard format is invalid"}},
            )
        return httpx.Response(200, json={"response": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = VkApiTransport("token", client=client)
        await transport.send(
            user_id=42,
            request_id="request-invalid-keyboard",
            text="Choose",
            buttons=[Button(label="Visible", value="callback:value")],
        )

    assert len(requests) == 2
    assert "keyboard" in requests[0]
    assert "keyboard" not in requests[1]
    assert requests[0]["random_id"] == requests[1]["random_id"]
    assert "Visible\n  Команда: callback:value" in requests[1]["message"][0]


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


@pytest.mark.asyncio
async def test_telegram_egress_reuses_existing_primary_invite_without_exporting() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        return httpx.Response(200, json={"ok": True, "result": {"invite_link": "https://t.me/+stable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        link = await egress.ensure_primary_invite_link(chat_id=-1001, request_id="invite-1")

    assert link == "https://t.me/+stable"
    assert methods == ["getChat"]


@pytest.mark.asyncio
async def test_telegram_egress_exports_primary_invite_only_when_missing() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        methods.append(method)
        result: object = {} if method == "getChat" else "https://t.me/+created"
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        link = await egress.ensure_primary_invite_link(chat_id=-1001, request_id="invite-2")

    assert link == "https://t.me/+created"
    assert methods == ["getChat", "exportChatInviteLink"]


@pytest.mark.asyncio
async def test_telegram_egress_promotes_title_only_admin_with_no_explicit_rights() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        await egress.promote_title_only_admin(chat_id=-1001, user_id=77, request_id="promote-1")

    payload = requests[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["can_manage_chat"] is True
    assert payload["can_delete_messages"] is False
    assert payload["can_promote_members"] is False
    assert payload["can_restrict_members"] is False


@pytest.mark.asyncio
async def test_telegram_egress_kick_demotes_bans_and_unbans_bot_promoted_admin() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        methods.append(method)
        result: object = {"status": "administrator", "can_be_edited": True} if method == "getChatMember" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)
        assert await egress.kick_member(chat_id=-1001, user_id=77, request_id="kick-1")

    assert methods == ["getChatMember", "promoteChatMember", "banChatMember", "unbanChatMember"]
