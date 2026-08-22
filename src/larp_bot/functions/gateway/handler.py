from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx

from larp_bot.adapters.runtime_config import RuntimeConfigProvider
from larp_bot.adapters.telegram import parse_telegram_update, telegram_inline_payload
from larp_bot.adapters.vk import parse_vk_event
from larp_bot.domain.models import Platform
from larp_bot.domain.security import constant_time_valid_signature, sign_request
from larp_bot.functions.bootstrap import AppContainer, build_container, iam_token_from_context

LOGGER = logging.getLogger("larp_bot.gateway")
_container: AppContainer | None = None
# AppContainer owns async HTTP clients and their keep-alive connections. A
# persistent Runner keeps those resources on the same event loop for every
# invocation handled by a warm Function process.
_runner = asyncio.Runner()


def _response(status: int, body: str | dict[str, Any]) -> dict[str, Any]:
    serialized = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    content_type = "text/plain; charset=utf-8" if isinstance(body, str) else "application/json; charset=utf-8"
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type},
        "body": serialized,
    }


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in event.get("headers", {}).items()}


def _body(event: dict[str, Any]) -> bytes:
    body = event.get("body", "")
    if not isinstance(body, str):
        raise ValueError("request body must be text")
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()


async def _container_instance(context: Any) -> AppContainer:
    global _container
    iam_token = iam_token_from_context(context)
    if _container is None:
        _container = await build_container(iam_token=iam_token)
    elif iam_token is not None:
        _container.config.set_iam_token(iam_token)
    return _container


def _verify_cloudflare(event: dict[str, Any], body: bytes, secret: str, *, now: int | None = None) -> tuple[str, int]:
    headers = _headers(event)
    timestamp = headers.get("x-gateway-timestamp", "")
    request_id = headers.get("x-gateway-request-id", "")
    supplied = headers.get("x-gateway-signature", "")
    if not timestamp.isdigit() or not request_id:
        raise PermissionError("missing signed transport metadata")
    current = int(time.time()) if now is None else now
    if abs(current - int(timestamp)) > 60:
        raise PermissionError("stale transport request")
    path = str(event.get("path") or event.get("requestContext", {}).get("http", {}).get("path") or "/webhooks/telegram")
    expected = sign_request(secret, timestamp, request_id, "POST", path, body)
    if not constant_time_valid_signature(expected, supplied):
        raise PermissionError("invalid transport signature")
    deadline = headers.get("x-telegram-inline-deadline-ms", "")
    if not deadline.isdigit():
        raise PermissionError("missing inline deadline")
    return request_id, int(deadline)


async def _telegram(event: dict[str, Any], body: bytes, app: AppContainer) -> dict[str, Any]:
    secret = await app.config.get_secret("CF_TO_YANDEX_HMAC_SECRET")
    request_id, deadline_ms = _verify_cloudflare(event, body, secret)
    try:
        update = json.loads(body)
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})
    if not isinstance(update, dict):
        return _response(400, {"error": "invalid update"})
    inbound = parse_telegram_update(update)
    result = await app.conversation.handle(inbound)
    LOGGER.info(
        "telegram_update_handled",
        extra={
            "request_id": request_id,
            "platform": Platform.TELEGRAM.value,
            "platform_update_id": inbound.update_id,
            "command_enqueued": result.command_enqueued,
        },
    )
    if result.silent:
        return _response(200, {"delivery": "deferred", "request_id": request_id})

    now_ms = int(time.time() * 1000)
    inline_safe = (
        not result.deferred
        and not result.command_enqueued
        and now_ms + app.settings.inline_safety_margin_ms < deadline_ms
    )
    if inline_safe:
        return _response(
            200,
            {
                "delivery": "inline",
                "telegram": telegram_inline_payload(result, inbound.chat_id or inbound.identity.platform_user_id),
            },
        )
    await app.transport.send(
        platform=Platform.TELEGRAM,
        user_id=inbound.chat_id or inbound.identity.platform_user_id,
        request_id=f"{request_id}:ack",
        text=result.text,
        buttons=result.buttons,
    )
    return _response(200, {"delivery": "deferred", "request_id": request_id})


async def _vk(body: bytes, app: AppContainer) -> dict[str, Any]:
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})
    if not isinstance(event, dict):
        return _response(400, {"error": "invalid event"})
    expected_secret = await app.config.get_secret("VK_CALLBACK_SECRET")
    expected_group = int(await app.config.get_secret("VK_GROUP_ID"))
    if event.get("secret") != expected_secret or event.get("group_id") != expected_group:
        return _response(403, {"error": "invalid VK callback authentication"})
    if event.get("type") == "confirmation":
        confirmation = await app.config.get_secret("VK_CONFIRMATION_STRING")
        return _response(200, confirmation)
    if event.get("type") != "message_new":
        return _response(200, "ok")
    inbound = parse_vk_event(event)
    result = await app.conversation.handle(inbound)
    LOGGER.info(
        "vk_update_handled",
        extra={
            "request_id": inbound.update_id,
            "platform": Platform.VK.value,
            "platform_update_id": inbound.update_id,
            "command_enqueued": result.command_enqueued,
        },
    )
    if not result.silent:
        await app.transport.send(
            platform=Platform.VK,
            user_id=inbound.peer_id or inbound.identity.platform_user_id,
            request_id=f"{inbound.update_id}:ack",
            text=result.text,
            buttons=result.buttons,
        )
    return _response(200, "ok")


async def _vk_confirmation_from_config(
    event: dict[str, Any],
    config: RuntimeConfigProvider,
) -> dict[str, Any]:
    expected_secret = await config.get_secret("VK_CALLBACK_SECRET")
    expected_group = int(await config.get_secret("VK_GROUP_ID"))
    if event.get("secret") != expected_secret or event.get("group_id") != expected_group:
        return _response(403, {"error": "invalid VK callback authentication"})
    confirmation = await config.get_secret("VK_CONFIRMATION_STRING")
    return _response(200, confirmation)


async def _vk_confirmation(event: dict[str, Any], context: Any) -> dict[str, Any]:
    required = {
        "config_url": "RUNTIME_CONFIG_URL",
        "audience": "RUNTIME_CONFIG_AUDIENCE",
        "service_account_id": "RUNTIME_SERVICE_ACCOUNT_ID",
    }
    missing = [environment for environment in required.values() if not os.getenv(environment)]
    if missing:
        raise RuntimeError(f"missing required runtime config: {', '.join(missing)}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        config = RuntimeConfigProvider(
            os.environ[required["config_url"]],
            os.environ[required["audience"]],
            os.environ[required["service_account_id"]],
            client=client,
            iam_token=iam_token_from_context(context),
        )
        return await _vk_confirmation_from_config(event, config)


def _reject_obviously_unauthenticated_vk(body: bytes) -> dict[str, Any] | None:
    """Reject requests that cannot possibly satisfy the VK callback contract.

    This check deliberately uses only the presence and shape of the credentials.
    Their values are still verified against the runtime config by ``_vk``. Keeping this
    preflight independent of the application container lets the public endpoint
    reject malformed traffic even while downstream services are starting.
    """
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})
    if not isinstance(event, dict):
        return _response(400, {"error": "invalid event"})
    if not isinstance(event.get("secret"), str) or type(event.get("group_id")) is not int:
        return _response(403, {"error": "invalid VK callback authentication"})
    return None


async def async_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        body = _body(event)
        if len(body) > 256 * 1024:
            return _response(413, {"error": "body too large"})
        method = str(event.get("httpMethod", "POST")).upper()
        if method != "POST":
            return _response(405, {"error": "method not allowed"})
        path = str(event.get("path", ""))
        if path.endswith("/telegram"):
            app = await _container_instance(context)
            return await _telegram(event, body, app)
        if path.endswith("/vk"):
            rejected = _reject_obviously_unauthenticated_vk(body)
            if rejected is not None:
                return rejected
            vk_event: dict[str, Any] = json.loads(body)
            if vk_event.get("type") == "confirmation":
                return await _vk_confirmation(vk_event, context)
            app = await _container_instance(context)
            return await _vk(body, app)
        return _response(404, {"error": "not found"})
    except PermissionError as exc:
        LOGGER.warning("transport_auth_rejected", extra={"reason": str(exc)})
        return _response(403, {"error": "forbidden"})
    except ValueError as exc:
        LOGGER.info("invalid_update", extra={"reason": str(exc)})
        return _response(400, {"error": "invalid request"})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _runner.run(async_handler(event, context))
