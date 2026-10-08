from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from larp_bot.adapters.memory import (
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
)
from larp_bot.adapters.telegram.adapter import telegram_inline_payload
from larp_bot.adapters.telegram.callbacks import decode_callback, encode_callback
from larp_bot.adapters.transports.deferred import CloudflareTelegramEgress
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.application.services import ConfirmationNotificationService, OrderedMutationService, RegistrationCatalog
from larp_bot.application.worker import OrderedWorker
from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    BotResponse,
    CharacterWishPayload,
    EnlistPayload,
    Event,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    Registration,
    TelegramUser,
    VkUser,
)
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore
from tests.unit.test_conversation import engine_setup, inbound
from tests.unit.test_worker import BlockedRecipientTransport, FailingOnceForRecipientTransport, FakeConsumer


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [Operation.ENLIST, Operation.CONFIRM])
@pytest.mark.parametrize("reserve", [False, True])
async def test_worker_notifies_global_masters_once_after_persistence(
    operation: Operation,
    reserve: bool,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    event.event_id = str(uuid4())
    event.player_amount = 1
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Иванов Иван",
            nickname="Ворон",
            vk_url="https://vk.com/id2",
            linked_vk_id=2,
            telegram_handle="@player",
            crossplay=True,
            larp_experience=True,
            needs_pass=False,
        )
    )
    await users.save(VkUser(vk_id=2, telegram_handle="@player"))
    await users.save(TelegramUser(tg_id=7, linked_vk_id=8, telegram_handle="@master"))
    await users.save(VkUser(vk_id=8, telegram_handle="@master"))
    await users.save(VkUser(vk_id=9))
    # Both local accounts of the player and one master have the event leader role.
    for platform, user_id in [
        (Platform.TELEGRAM, 1),
        (Platform.VK, 2),
        (Platform.TELEGRAM, 7),
        (Platform.VK, 8),
        (Platform.VK, 9),
    ]:
        await events.add_leader(event.event_id, BotIdentity(platform=platform, platform_user_id=user_id))
    secret = "secret"
    key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    earlier = datetime.now(UTC) - timedelta(days=1)
    registrations = []
    if reserve:
        registrations.append(
            Registration(
                event_id=event.event_id,
                participant_key="z" * 43,
                display_name="Петров Пётр",
                wish_play="Без пожеланий",
                created_at=earlier,
            )
        )
    if operation is Operation.CONFIRM:
        registrations.append(
            Registration(
                event_id=event.event_id,
                participant_key=key,
                display_name="Иванов Иван",
                nickname="Ворон",
                wish_play="Без пожеланий",
            )
        )
    tables = MemoryRegistrationRepository(registrations)
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, showcase)
    transport = MemoryDeferredTransport()
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=operation,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key=key,
        payload=(
            EnlistPayload(display_name="Иванов Иван", wish_play="Без пожеланий")
            if operation is Operation.ENLIST
            else CharacterWishPayload(character_wish="Врач")
        ),
    )
    for receipt in ("first", "retry"):
        # New service and worker instances simulate separate invocations.
        notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
        worker = OrderedWorker(
            FakeConsumer([QueueEnvelope(command=command, receipt_handle=receipt)]),
            OrderedMutationService(events, catalog, users, secret),
            users,
            transport,
            notifications,
            max_seconds=2,
        )
        assert await worker.run() == 1
        # Other deliveries overwrite the user's last marker but must not defeat deduplication.
        await users.claim_delivery(Platform.TELEGRAM, 7, "unrelated-operation")
    assert (await tables.get(event.event_id, key)).attendance_status is (
        AttendanceStatus.WAITING if operation is Operation.ENLIST else AttendanceStatus.CONFIRMED
    )
    assert [sent[0:2] for sent in transport.sent] == [(Platform.TELEGRAM, 1), (Platform.TELEGRAM, 7), (Platform.VK, 9)]
    for sent in transport.sent[1:]:
        assert "Иван Иванов (Ворон)" in sent[3]
        assert ("записался" if operation is Operation.ENLIST else "подтвердил") in sent[3]
        assert ("Запас, место 2." if reserve else "Основной состав, место 1.") in sent[3]
        button = sent[4][0]
        assert button.label == "Управление игрой"
        encoded = encode_callback(button.value)
        assert 1 <= len(encoded.encode("utf-8")) <= 64
        assert decode_callback(encoded) == f"ag:manage:{event.event_id}"
        for payload in (
            telegram_inline_payload(BotResponse(text=sent[3], buttons=list(sent[4])), sent[1]),
            CloudflareTelegramEgress._message_payload(sent[1], sent[3], sent[4]),
        ):
            final = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            assert 1 <= len(final.encode("utf-8")) <= 64
            assert decode_callback(final) == f"ag:manage:{event.event_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_delivery_failure_preserves_completed_recipients_and_registration(
    blocked: bool,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    events = MemoryEventRepository([event])
    for user_id in (7, 9):
        await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id))
    users = MemoryUserRepository()
    for user_id in (1, 7, 9):
        await users.save(TelegramUser(tg_id=user_id))
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=Operation.ENLIST,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key="a" * 43,
        payload=EnlistPayload(display_name="Иванов Иван", wish_play="Без пожеланий"),
    )
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key="a" * 43,
                display_name="Иванов Иван",
                wish_play="Без пожеланий",
                last_operation_id=command.operation_id,
            )
        ]
    )
    transport = (
        BlockedRecipientTransport((Platform.TELEGRAM, 9))
        if blocked
        else FailingOnceForRecipientTransport(Platform.TELEGRAM, 9)
    )
    catalog = RegistrationCatalog(events, tables, YandexDiskShowcaseRepository(disk_store))
    service = ConfirmationNotificationService(events, catalog, users, transport, "secret")
    if blocked:
        assert await service.notify_masters_of_participation(command) == 1
    else:
        with pytest.raises(RuntimeError, match="transport unavailable"):
            await service.notify_masters_of_participation(command)
    service = ConfirmationNotificationService(events, catalog, users, transport, "secret")
    assert await service.notify_masters_of_participation(command) == (0 if blocked else 1)
    assert [sent[1] for sent in transport.sent] == ([7] if blocked else [7, 9])
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None and registration.attendance_status is AttendanceStatus.WAITING
    assert registration.last_operation_id == command.operation_id


@pytest.mark.asyncio
async def test_management_callback_routes_with_production_uuid(disk_store: MemoryDiskStore, event: Event) -> None:
    event.event_id = str(uuid4())
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)
    callback = encode_callback(f"ag:manage:{event.event_id}")
    response = await engine.handle(inbound(1, decode_callback(callback), callback=True))
    assert event.name in response.text
    assert any(event.event_id in button.value for button in response.buttons)
    assert publisher.commands == []
