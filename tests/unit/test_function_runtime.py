from __future__ import annotations

import asyncio
import importlib
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_worker_does_not_acknowledge_kick_when_continuation_cannot_be_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module("larp_bot.functions.ordered_worker.handler")
    container = SimpleNamespace(worker=AsyncMock(), publisher=AsyncMock())
    container.worker.run.return_value = 5
    container.publisher.continue_if_pending.side_effect = RuntimeError("queue unavailable")
    monkeypatch.setattr(module, "_container", container)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await module._run(None)
    container.publisher.continue_if_pending.assert_awaited_once_with(processed=5)

    container.publisher.continue_if_pending.side_effect = None
    assert await module._run(None) == {"processed": 5}


def test_registration_notifier_reuses_event_loop_between_warm_invocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = _module("larp_bot.functions.registration_notifier.handler")
    loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(context: Any) -> dict[str, int]:
        del context
        loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        return {"events_completed": 0, "notifications_delivered": 0}

    monkeypatch.setattr(notifier, "_run", fake_run)

    expected = {"events_completed": 0, "notifications_delivered": 0}
    assert notifier.handler({}, None) == expected
    assert notifier.handler({}, None) == expected
    assert loops[0] is loops[1]
    assert not loops[0].is_closed()


@pytest.mark.asyncio
async def test_registration_notifier_shares_one_due_event_read_between_all_dispatchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifier = _module("larp_bot.functions.registration_notifier.handler")
    due_events = [object()]
    container = SimpleNamespace(
        events=SimpleNamespace(list_due_lifecycle_events=AsyncMock(return_value=due_events)),
        registration_opening_notifications=SimpleNamespace(
            notify_due=AsyncMock(return_value=SimpleNamespace(events_completed=1, notifications_delivered=2))
        ),
        scheduled_confirmation_opening_dispatcher=SimpleNamespace(
            dispatch_due=AsyncMock(return_value=SimpleNamespace(commands_enqueued=3))
        ),
        scheduled_game_closing_dispatcher=SimpleNamespace(
            dispatch_due=AsyncMock(return_value=SimpleNamespace(commands_enqueued=4))
        ),
    )
    monkeypatch.setattr(notifier, "_container", container)

    result = await notifier._run(None)

    assert result == {
        "events_completed": 1,
        "notifications_delivered": 2,
        "confirmation_commands_enqueued": 3,
        "closing_commands_enqueued": 4,
    }
    container.events.list_due_lifecycle_events.assert_awaited_once()
    for service in (
        container.registration_opening_notifications.notify_due,
        container.scheduled_confirmation_opening_dispatcher.dispatch_due,
        container.scheduled_game_closing_dispatcher.dispatch_due,
    ):
        assert service.await_args.kwargs["due_events"] is due_events
