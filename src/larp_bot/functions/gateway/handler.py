from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

from larp_bot.adapters.runtime_config import RuntimeConfigProvider
from larp_bot.adapters.telegram import parse_telegram_group_update, parse_telegram_update, telegram_inline_payload
from larp_bot.adapters.vk import parse_vk_event
from larp_bot.application.ports import AdminConfigProvider
from larp_bot.domain.models import Platform
from larp_bot.domain.security import constant_time_valid_signature, sign_request
from larp_bot.functions.container import AppContainer, iam_token_from_context
from larp_bot.functions.runtime_mode import RuntimeGate, runtime_controls_configured

LOGGER = logging.getLogger("larp_bot.gateway")
_container: AppContainer | None = None
_confirmation_config: AdminConfigProvider | None = None
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
        if os.getenv("CLOUD_PROVIDER") == "aws":
            from larp_bot.functions.aws_bootstrap import build_aws_container

            _container = await build_aws_container()
        else:
            from larp_bot.functions.bootstrap import build_container

            _container = await build_container(iam_token=iam_token)
    elif iam_token is not None:
        _container.config.set_iam_token(iam_token)
        _container.disk_uploader.set_iam_token(iam_token)
    return _container


def _verify_cloudflare_transport(
    event: dict[str, Any],
    body: bytes,
    secret: str,
    *,
    now: int | None = None,
) -> str:
    headers = _headers(event)
    timestamp = headers.get("x-gateway-timestamp", "")
    request_id = headers.get("x-gateway-request-id", "")
    supplied = headers.get("x-gateway-signature", "")
    if not timestamp.isdigit() or not request_id:
        raise PermissionError("missing signed transport metadata")
    current = int(time.time()) if now is None else now
    if abs(current - int(timestamp)) > 60:
        raise PermissionError("stale transport request")
    path = str(
        event.get("path")
        or event.get("rawPath")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or "/webhooks/telegram"
    )
    expected = sign_request(secret, timestamp, request_id, "POST", path, body)
    if not constant_time_valid_signature(expected, supplied):
        raise PermissionError("invalid transport signature")
    return request_id


def _verify_cloudflare(event: dict[str, Any], body: bytes, secret: str, *, now: int | None = None) -> tuple[str, int]:
    request_id = _verify_cloudflare_transport(event, body, secret, now=now)
    headers = _headers(event)
    deadline = headers.get("x-telegram-inline-deadline-ms", "")
    if not deadline.isdigit():
        raise PermissionError("missing inline deadline")
    return request_id, int(deadline)


async def _telegram(event: dict[str, Any], body: bytes, app: AppContainer) -> dict[str, Any]:
    secret_key = "CF_TO_AWS_HMAC_SECRET" if os.getenv("CLOUD_PROVIDER") == "aws" else "CF_TO_YANDEX_HMAC_SECRET"
    secret = await app.config.get_secret(secret_key)
    request_id, deadline_ms = _verify_cloudflare(event, body, secret)
    try:
        update = json.loads(body)
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})
    if not isinstance(update, dict):
        return _response(400, {"error": "invalid update"})
    group_update = parse_telegram_group_update(update)
    if group_update is not None:
        await app.telegram_chat_management.handle(group_update)
        return _response(200, {"delivery": "deferred", "request_id": request_id})
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
    config: AdminConfigProvider,
) -> dict[str, Any]:
    expected_secret = await config.get_secret("VK_CALLBACK_SECRET")
    expected_group = int(await config.get_secret("VK_GROUP_ID"))
    if event.get("secret") != expected_secret or event.get("group_id") != expected_group:
        return _response(403, {"error": "invalid VK callback authentication"})
    confirmation = await config.get_secret("VK_CONFIRMATION_STRING")
    return _response(200, confirmation)


def _confirmation_config_instance(context: Any) -> AdminConfigProvider:
    """Return the warm-process provider that answers VK confirmation callbacks.

    It stays independent of the application container so VK can confirm the server
    while YDB and other downstream services start. Reusing one provider keeps its
    60-second secret cache, so repeated or forged confirmations cost at most one
    identity exchange and one runtime-config fetch per warm process per minute.
    """
    global _confirmation_config
    iam_token = iam_token_from_context(context)
    if _confirmation_config is None:
        if os.getenv("CLOUD_PROVIDER") == "aws":
            import boto3

            from larp_bot.adapters.aws import SsmRuntimeConfigProvider

            prefix = os.getenv("AWS_PARAMETER_PREFIX")
            if not prefix:
                raise RuntimeError("missing required runtime config: AWS_PARAMETER_PREFIX")
            _confirmation_config = SsmRuntimeConfigProvider(boto3.client("ssm"), prefix)
            return _confirmation_config
        required = {
            "config_url": "RUNTIME_CONFIG_URL",
            "audience": "RUNTIME_CONFIG_AUDIENCE",
            "service_account_id": "RUNTIME_SERVICE_ACCOUNT_ID",
        }
        missing = [environment for environment in required.values() if not os.getenv(environment)]
        if missing:
            raise RuntimeError(f"missing required runtime config: {', '.join(missing)}")
        # The provider's own HTTP client lives on the persistent Runner loop, like
        # the application container's clients.
        _confirmation_config = RuntimeConfigProvider(
            os.environ[required["config_url"]],
            os.environ[required["audience"]],
            os.environ[required["service_account_id"]],
            iam_token=iam_token,
        )
    elif iam_token is not None:
        set_iam_token = getattr(_confirmation_config, "set_iam_token", None)
        if callable(set_iam_token):
            set_iam_token(iam_token)
    return _confirmation_config


async def _vk_confirmation(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return await _vk_confirmation_from_config(event, _confirmation_config_instance(context))


async def _verify_cloudflare_vk(
    event: dict[str, Any],
    body: bytes,
    context: Any,
) -> AdminConfigProvider:
    config = _confirmation_config_instance(context)
    secret_key = "CF_TO_AWS_HMAC_SECRET" if os.getenv("CLOUD_PROVIDER") == "aws" else "CF_TO_YANDEX_HMAC_SECRET"
    secret = await config.get_secret(secret_key)
    _verify_cloudflare_transport(event, body, secret)
    return config


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
        request_context = event.get("requestContext")
        http_context = request_context.get("http", {}) if isinstance(request_context, dict) else {}
        method = str(event.get("httpMethod") or http_context.get("method") or "POST").upper()
        if method != "POST":
            return _response(405, {"error": "method not allowed"})
        path = str(event.get("path") or event.get("rawPath") or http_context.get("path") or "")
        if path.endswith("/telegram"):
            app = await _container_instance(context)
            return await _telegram(event, body, app)
        if path.endswith("/vk-cloudflare"):
            rejected = _reject_obviously_unauthenticated_vk(body)
            if rejected is not None:
                return rejected
            vk_event: dict[str, Any] = json.loads(body)
            config = await _verify_cloudflare_vk(event, body, context)
            if vk_event.get("type") == "confirmation":
                return await _vk_confirmation_from_config(vk_event, config)
            app = await _container_instance(context)
            return await _vk(body, app)
        if path.endswith("/vk"):
            # The direct route remains only as the Yandex rollback target while
            # VK is moved to Cloudflare. AWS accepts VK exclusively through the
            # separately signed Cloudflare path.
            if os.getenv("CLOUD_PROVIDER") == "aws":
                return _response(404, {"error": "not found"})
            rejected = _reject_obviously_unauthenticated_vk(body)
            if rejected is not None:
                return rejected
            vk_event = json.loads(body)
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
    if (
        os.getenv("CLOUD_PROVIDER") == "aws" or runtime_controls_configured()
    ) and not RuntimeGate.from_environment().accepts_application_work:
        return _response(503, {"error": "backend is not active"})
    return _runner.run(async_handler(event, context))
