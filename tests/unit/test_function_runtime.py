from __future__ import annotations

import asyncio
import importlib
from types import ModuleType
from typing import Any

import pytest


def _module(name: str) -> ModuleType:
    return importlib.import_module(name)


def test_gateway_reuses_event_loop_between_warm_invocations(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _module("larp_bot.functions.gateway.handler")
    loops: list[asyncio.AbstractEventLoop] = []

    async def fake_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
        del event, context
        loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        return {"statusCode": 200}

    monkeypatch.setattr(gateway, "async_handler", fake_handler)

    assert gateway.handler({}, None) == {"statusCode": 200}
    assert gateway.handler({}, None) == {"statusCode": 200}
    assert loops[0] is loops[1]
    assert not loops[0].is_closed()


def test_ordered_worker_reuses_event_loop_between_warm_invocations(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _module("larp_bot.functions.ordered_worker.handler")
    loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(context: Any) -> dict[str, int]:
        del context
        loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        return {"processed": 0}

    monkeypatch.setattr(worker, "_run", fake_run)

    assert worker.handler({}, None) == {"processed": 0}
    assert worker.handler({}, None) == {"processed": 0}
    assert loops[0] is loops[1]
    assert not loops[0].is_closed()
