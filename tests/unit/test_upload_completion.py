from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from larp_bot.adapters.memory import (
    MemoryCommandPublisher,
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryUserRepository,
    StaticAdminProvider,
)
from larp_bot.application.services import daily_pass_table_path
from larp_bot.application.table_links import public_table_links
from larp_bot.application.upload_completion import UploadCompletionService
from larp_bot.domain.models import DailyPassRefreshPayload, Event, Operation, Platform, TelegramUser


@pytest.mark.asyncio
async def test_completion_creates_event_and_sends_links_once() -> None:
    events = MemoryEventRepository()
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    publisher = MemoryCommandPublisher()
    service = UploadCompletionService(
        events,
        users,
        StaticAdminProvider(secrets={"TG_BOT_USERNAME": "role_bot", "VK_GROUP_ID": "123"}),
        transport,
        publisher,
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
    assert len(publisher.commands) == 1
    assert publisher.commands[0].operation is Operation.REFRESH_DAILY_PASS_TABLE
    assert publisher.commands[0].payload == DailyPassRefreshPayload(game_date=date(2026, 10, 10))


@pytest.mark.asyncio
async def test_yandex_failure_keeps_google_tables_as_the_only_visible_links() -> None:
    events = MemoryEventRepository()
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    service = UploadCompletionService(
        events,
        users,
        StaticAdminProvider(secrets={"TG_BOT_USERNAME": "role_bot", "VK_GROUP_ID": "123"}),
        transport,
        MemoryCommandPublisher(),
    )
    google = {
        "master": "https://drive.example/master",
        "public": "https://drive.example/public",
        "pass": "https://drive.example/pass",
    }
    await service.handle(
        {
            "schema_version": 1,
            "job_id": "12345678-1234-1234-1234-123456789abc",
            "success": False,
            "public_urls": {"master": "https://disk.example/master"},
            "error": "Yandex Disk timed out",
            "callback": {
                "kind": "event_created",
                "google_public_urls": google,
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
                "leaders": [],
                "requester": {"platform": "telegram", "user_id": 42, "profile_user_id": 42},
            },
        }
    )

    event = await events.get("event-1234")
    assert event is not None
    assert event.master_table_google_url == google["master"]
    assert event.public_table_google_url == google["public"]
    assert event.pass_table_google_url == google["pass"]
    assert event.public_registration_url == "https://disk.example/master"
    assert event.public_table_public_url == google["public"]
    assert event.pass_table_public_url == google["pass"]

    admin_text = transport.sent[0][3]
    assert all(url in admin_text for url in google.values())
    assert "https://disk.example/master" not in admin_text
    player_text = public_table_links(event).for_player()
    assert player_text == f"Google Drive: {google['public']}"


@pytest.mark.asyncio
async def test_completion_sends_the_stable_achievement_link_for_each_new_export_job() -> None:
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
    service = UploadCompletionService(events, users, StaticAdminProvider(), transport, MemoryCommandPublisher())

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
    await service.handle(payload)
    await service.handle(
        payload
        | {
            "job_id": "22345678-1234-1234-1234-123456789abc",
            "public_urls": {"achievement": "https://disk.example/unexpected-new-link"},
        }
    )

    persisted = await events.get(event.event_id)
    assert persisted is not None
    assert persisted.achievement_public_url == "https://disk.example/achievement"
    assert len(transport.sent) == 2
    assert all(message[3].endswith("https://disk.example/achievement") for message in transport.sent)


@pytest.mark.asyncio
async def test_failed_upload_sends_retry_message_without_a_link() -> None:
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    service = UploadCompletionService(
        MemoryEventRepository(), users, StaticAdminProvider(), transport, MemoryCommandPublisher()
    )

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


@pytest.mark.asyncio
async def test_daily_pass_completion_sends_the_published_link_once_with_a_safe_admin_callback() -> None:
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    publisher = MemoryCommandPublisher()
    service = UploadCompletionService(MemoryEventRepository(), users, StaticAdminProvider(), transport, publisher)
    game_date = date(2026, 11, 14)
    payload = {
        "schema_version": 1,
        "job_id": "12345678-1234-1234-1234-123456789abc",
        "success": True,
        "public_urls": {"pass": "https://disk.example/daily-passes"},
        "callback": {
            "kind": "daily_passes",
            "game_date": game_date.isoformat(),
            "resource_path": daily_pass_table_path(game_date),
            "requester": {"platform": "telegram", "user_id": 42, "profile_user_id": 42},
        },
    }

    await service.handle(payload)
    await service.handle(payload)

    assert len(transport.sent) == 1
    assert transport.sent[0][3] == "Таблица пропусков на 14.11.26\n\nhttps://disk.example/daily-passes"
    callback = transport.sent[0][4][0].value
    assert 1 <= len(callback.encode("utf-8")) <= 64
    assert len(publisher.commands) == 1
    command = publisher.commands[0]
    assert command.operation is Operation.REFRESH_DAILY_PASS_TABLE
    assert command.event_id == "daily-passes:2026-11-14"
    assert command.platform is Platform.SYSTEM
    assert command.payload == DailyPassRefreshPayload(game_date=game_date)


@pytest.mark.asyncio
async def test_daily_pass_completion_sends_link_before_queueing_its_consistency_refresh() -> None:
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42))
    transport = MemoryDeferredTransport()
    publisher = AsyncMock()
    publisher.publish.side_effect = RuntimeError("queue unavailable")
    service = UploadCompletionService(
        MemoryEventRepository(),
        users,
        StaticAdminProvider(),
        transport,
        publisher,
    )
    game_date = date(2026, 11, 14)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.handle(
            {
                "schema_version": 1,
                "job_id": "12345678-1234-1234-1234-123456789abc",
                "success": True,
                "public_urls": {"pass": "https://disk.example/daily-passes"},
                "callback": {
                    "kind": "daily_passes",
                    "game_date": game_date.isoformat(),
                    "resource_path": daily_pass_table_path(game_date),
                    "requester": {"platform": "telegram", "user_id": 42, "profile_user_id": 42},
                },
            }
        )

    assert len(transport.sent) == 1
    assert transport.sent[0][3].endswith("https://disk.example/daily-passes")
