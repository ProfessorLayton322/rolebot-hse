from __future__ import annotations

from datetime import date

import pytest

from larp_bot.adapters.memory import (
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryUserRepository,
    StaticAdminProvider,
)
from larp_bot.application.upload_completion import UploadCompletionService
from larp_bot.domain.models import Event, Platform, TelegramUser


@pytest.mark.asyncio
async def test_completion_creates_event_and_sends_links_once() -> None:
    events = MemoryEventRepository()
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    service = UploadCompletionService(
        events,
        users,
        StaticAdminProvider(secrets={"TG_BOT_USERNAME": "role_bot", "VK_GROUP_ID": "123"}),
        transport,
    )
    payload = {
        "schema_version": 1,
        "job_id": "12345678-1234-1234-1234-123456789abc",
        "success": True,
        "public_urls": {
            "master": "https://disk.example/master",
            "public": "https://disk.example/public",
            "pass": "https://disk.example/pass",
        },
        "callback": {
            "kind": "event_created",
            "event": {
                "event_id": "event-1234",
                "name": "Осенняя игра",
                "game_date": "2026-10-10",
                "player_amount": 30,
                "disk_resource_path": "disk:/larp-bot/events/event-1234/master.xlsx",
                "public_table_resource_path": "disk:/larp-bot/events/event-1234/public.xlsx",
                "pass_table_resource_path": "disk:/larp-bot/passes/event-1234.xlsx",
                "registration_open_at": None,
                "signup_token": "signup-token",
            },
            "leaders": [{"platform": "telegram", "platform_user_id": 42}],
            "requester": {"platform": "telegram", "user_id": 42, "profile_user_id": 42},
        },
    }

    await service.handle(payload)
    await service.handle(payload)

    event = await events.get("event-1234")
    assert event is not None
    assert event.public_registration_url == "https://disk.example/master"
    assert event.public_table_public_url == "https://disk.example/public"
    assert event.pass_table_public_url == "https://disk.example/pass"
    assert events.leaders["event-1234"] == {(Platform.TELEGRAM, 42)}
    assert len(transport.sent) == 1
    assert "✅ Игра создана." in transport.sent[0][3]
    assert "https://disk.example/pass" in transport.sent[0][3]


@pytest.mark.asyncio
async def test_completion_persists_achievement_before_sending_link() -> None:
    event = Event(
        event_id="event-1234",
        name="Осенняя игра",
        game_date=date(2026, 10, 10),
        disk_resource_path="disk:/larp-bot/events/event-1234/master.xlsx",
        public_registration_url="https://disk.example/master",
    )
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    service = UploadCompletionService(events, users, StaticAdminProvider(), transport)

    payload = {
        "schema_version": 1,
        "job_id": "12345678-1234-1234-1234-123456789abc",
        "success": True,
        "public_urls": {"achievement": "https://disk.example/achievement"},
        "callback": {
            "kind": "achievement",
            "event_id": event.event_id,
            "resource_path": "disk:/larp-bot/achievements/event-1234.pptx",
            "requester": {"platform": "telegram", "user_id": 42, "profile_user_id": 42},
        },
    }

    await service.handle(payload)
    await service.handle(payload | {"job_id": "22345678-1234-1234-1234-123456789abc"})

    persisted = await events.get(event.event_id)
    assert persisted is not None
    assert persisted.achievement_public_url == "https://disk.example/achievement"
    assert len(transport.sent) == 1
    assert transport.sent[0][3].endswith("https://disk.example/achievement")


@pytest.mark.asyncio
async def test_failed_upload_sends_retry_message_without_a_link() -> None:
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    service = UploadCompletionService(MemoryEventRepository(), users, StaticAdminProvider(), transport)

    await service.handle(
        {
            "schema_version": 1,
            "job_id": "12345678-1234-1234-1234-123456789abc",
            "success": False,
            "public_urls": {},
            "callback": {
                "kind": "achievement",
                "requester": {"platform": "telegram", "user_id": 42, "profile_user_id": 42},
            },
        }
    )

    assert len(transport.sent) == 1
    assert transport.sent[0][3] == "Не удалось загрузить файл. Попробуйте повторить действие позже."
