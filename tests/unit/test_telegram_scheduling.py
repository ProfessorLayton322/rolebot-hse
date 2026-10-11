import json
from datetime import date, datetime
from typing import Any

import httpx
import pytest

from larp_bot.adapters.telegram import parse_telegram_update, telegram_inline_payload
from larp_bot.adapters.transports.deferred import CloudflareTelegramEgress
from larp_bot.application.deadlines import MOSCOW_TIMEZONE
from larp_bot.domain.models import BotResponse, Event, EventStatus
from tests.conftest import MemoryDiskStore
from tests.unit.test_conversation import engine_setup, inbound


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["inline", "deferred"])
@pytest.mark.parametrize("shortcut", [False, True])
async def test_confirmation_schedule_with_real_uuid_has_deliverable_replies(
    monkeypatch: pytest.MonkeyPatch,
    disk_store: MemoryDiskStore,
    event: Event,
    delivery: str,
    shortcut: bool,
) -> None:
    class IncidentTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 9, 29, 9, 58, tzinfo=MOSCOW_TIMEZONE).astimezone(tz)

    monkeypatch.setattr("larp_bot.application.conversation_admin_dialog.datetime", IncidentTime)
    monkeypatch.setattr("larp_bot.application.conversation_admin_games.datetime", IncidentTime)
    scheduled = event.model_copy(
        update={
            "event_id": "2ea09780-4241-469a-829f-227e67b83473",
            "status": EventStatus.CREATED,
            "game_date": date(2026, 10, 3),
            "registration_open_at": datetime(2026, 9, 25, 18, tzinfo=MOSCOW_TIMEZONE),
        }
    )
    engine, _, _, _ = await engine_setup(disk_store, scheduled, admin=True)
    payloads: list[dict[str, Any]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content)["payload"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        egress = CloudflareTelegramEgress("https://egress.example", "hmac", client=client)

        async def deliver(response: BotResponse) -> list[str]:
            assert response.text and not response.silent
            if delivery == "inline":
                payload = telegram_inline_payload(response, 1)
            else:
                await egress.send_message(
                    chat_id=1, request_id="schedule", text=response.text, buttons=response.buttons
                )
                payload = payloads[-1]
            callbacks = [
                button["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for button in row
            ]
            assert all(1 <= len(value.encode()) <= 64 for value in callbacks), callbacks
            return callbacks

        async def click(update_id: int, value: str) -> BotResponse:
            return await engine.handle(
                parse_telegram_update({"update_id": update_id, "callback_query": {"from": {"id": 1}, "data": value}})
            )

        prompt = await engine.handle(inbound(1, f"ag:schedule-confirmation:{scheduled.event_id}", callback=True))
        callbacks = await deliver(prompt)
        if shortcut:
            deadline = await click(2, callbacks[0])
        else:
            deadline = await engine.handle(inbound(2, "29.09.26 18:00"))
        assert "Введите дедлайн подтверждения" in deadline.text
        await deliver(deadline)
        saved = await engine.handle(inbound(3, "02.10.26 18:00"))
        assert "дедлайн сохранены" in saved.text
        await deliver(saved)
        stored = await engine.events.get(scheduled.event_id)
        assert stored is not None
        assert stored.confirmation_open_at == datetime(2026, 9, 29, 18, tzinfo=MOSCOW_TIMEZONE)

        prompt = await engine.handle(inbound(4, f"ag:schedule-confirmation:{scheduled.event_id}", callback=True))
        callbacks = await deliver(prompt)
        cleared = await click(5, callbacks[0])
        assert "открытие подтверждения отменено" in cleared.text
        await deliver(cleared)

        registration = await engine.handle(inbound(6, f"ag:schedule-registration:{scheduled.event_id}", callback=True))
        callbacks = await deliver(registration)
        cleared_registration = await click(7, callbacks[0])
        assert "регистрация открыта" in cleared_registration.text
        await deliver(cleared_registration)
