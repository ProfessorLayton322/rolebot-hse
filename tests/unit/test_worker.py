from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from larp_bot.adapters.memory import (
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
)
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.application.navigation import CONFIRM_PARTICIPATION, MAIN_MENU
from larp_bot.application.services import (
    ConfirmationNotificationService,
    OrderedMutationService,
    RegistrationCatalog,
    confirmed_notification_text,
    is_plain_chat_link,
)
from larp_bot.application.worker import OrderedWorker
from larp_bot.domain.models import (
    AttendanceStatus,
    Button,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    ReplyContext,
    TelegramUser,
    VkUser,
)
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore


class FakeConsumer:
    def __init__(self, envelopes: Sequence[QueueEnvelope]) -> None:
        self.envelopes = list(envelopes)
        self.deleted: list[str] = []

    async def receive(self, *, max_messages: int, wait_seconds: int) -> Sequence[object]:
        del wait_seconds
        batch, self.envelopes = self.envelopes[:max_messages], self.envelopes[max_messages:]
        return batch

    async def delete(self, receipt_handle: str) -> None:
        self.deleted.append(receipt_handle)


class FailingTransport(MemoryDeferredTransport):
    async def send(self, **kwargs: object) -> None:
        del kwargs
        raise RuntimeError("transport unavailable")


def queued(
    event: Event,
    operation: Operation,
    payload: EnlistPayload | CharacterWishPayload | ConfirmationDeadlinePayload | NotificationPayload | EmptyPayload,
    index: int,
    buttons: Sequence[Button] = (),
) -> QueueEnvelope:
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=operation,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key=(
            "a" * 43
            if operation in {Operation.ENLIST, Operation.CONFIRM, Operation.UPDATE_CHARACTER_WISH, Operation.CANCEL}
            else None
        ),
        payload=payload,
        reply_context=ReplyContext(buttons=list(buttons)),
    )
    return QueueEnvelope(command=command, receipt_handle=f"receipt-{index}")


@pytest.mark.asyncio
async def test_worker_processes_order_and_suppresses_duplicate_delivery(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1, last_bot_buttons=[Button(label="Old", value="old:button")]))
    envelopes = [
        queued(
            event,
            Operation.ENLIST,
            EnlistPayload(display_name="Player", wish_play="A"),
            1,
            [Button(label=MAIN_MENU, value=MAIN_MENU)],
        ),
        queued(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A"), 2),
        queued(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B"), 3),
        queued(event, Operation.CANCEL, EmptyPayload(), 4),
    ]
    consumer = FakeConsumer(envelopes)
    transport = MemoryDeferredTransport()
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, RegistrationCatalog(events, tables, showcase)),
        users,
        transport,
        max_seconds=2,
    )
    assert await worker.run() == 4
    result = await tables.get(event.event_id, "a" * 43)
    assert result is not None
    assert result.character_wish == "B"
    assert result.attendance_status is AttendanceStatus.CANCELLED
    assert consumer.deleted == [f"receipt-{index}" for index in range(1, 5)]
    assert len(transport.sent) == 4
    assert [(button.label, button.value) for button in transport.sent[0][4]] == [(MAIN_MENU, MAIN_MENU)]
    saved_user = await users.get(Platform.TELEGRAM, 1)
    assert saved_user is not None and saved_user.last_bot_buttons == []


@pytest.mark.asyncio
async def test_delivery_failure_keeps_fifo_message_retryable(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    consumer = FakeConsumer([queued(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A"), 1)])
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(
            events := MemoryEventRepository([event]),
            RegistrationCatalog(events, tables, showcase),
        ),
        users,
        FailingTransport(),
        max_seconds=2,
    )
    with pytest.raises(RuntimeError, match="transport unavailable"):
        await worker.run()
    assert consumer.deleted == []
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_text"),
    [
        (
            Operation.OPEN_CONFIRMATION,
            "Подтверждение на игру Лесной предел открыто! Дедлайн подтверждения - 10.09.26 19:00",
        ),
        (
            Operation.SEND_CONFIRMATION_REMINDER,
            "Напоминаем о необходимости подтвердить или отменить участие в игре Лесной предел до 10.09.26 19:00!",
        ),
    ],
)
async def test_confirmation_notifications_go_only_to_waiting_players(
    operation: Operation,
    expected_text: str,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    deadline = datetime(2026, 9, 10, 16, tzinfo=UTC)
    event.status = EventStatus.CREATED if operation is Operation.OPEN_CONFIRMATION else EventStatus.CONFIRMATION_OPEN
    event.confirmation_deadline = None if operation is Operation.OPEN_CONFIRMATION else deadline
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(events, tables, showcase)
    users = MemoryUserRepository()
    waiting_users = [TelegramUser(tg_id=2), VkUser(vk_id=3)]
    confirmed_user = TelegramUser(tg_id=4)
    cancelled_user = VkUser(vk_id=5)
    for user in [*waiting_users, confirmed_user, cancelled_user, TelegramUser(tg_id=1)]:
        await users.save(user)
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        uid = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
        if uid == 1:
            continue
        key = participant_key(secret, platform, uid, event.event_id)
        await tables.enlist(
            event.event_id,
            operation_id=f"enlist-{platform.value}-{uid}",
            participant_key=key,
            display_name=f"Player {uid}",
            wish_play="A",
        )
        if user is confirmed_user:
            await tables.confirm(
                event.event_id,
                operation_id="confirm-4",
                participant_key=key,
                character_wish="Doctor",
            )
        if user is cancelled_user:
            await tables.cancel(event.event_id, operation_id="cancel-5", participant_key=key)

    payload = (
        ConfirmationDeadlinePayload(deadline=deadline) if operation is Operation.OPEN_CONFIRMATION else EmptyPayload()
    )
    envelope = queued(event, operation, payload, 1)
    consumer = FakeConsumer([envelope])
    transport = MemoryDeferredTransport()
    notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, catalog),
        users,
        transport,
        notifications,
        max_seconds=2,
    )

    assert await worker.run() == 1
    assert {(sent[0], sent[1], sent[3]) for sent in transport.sent} == {
        (Platform.TELEGRAM, 2, expected_text),
        (Platform.VK, 3, expected_text),
    }
    assert all(
        [(button.label, button.value) for button in sent[4]]
        == [(CONFIRM_PARTICIPATION, f"select:confirm:{event.event_id}")]
        for sent in transport.sent
    )
    assert consumer.deleted == ["receipt-1"]


@pytest.mark.parametrize(
    "value",
    [
        "https://t.me/+GameChat_123",
        "https://telegram.me/joinchat/GameChat-123",
        "https://t.me/public_game_chat",
        "https://vk.me/join/GameChat_123-=",
    ],
)
def test_plain_chat_links_get_game_invitation(value: str) -> None:
    assert is_plain_chat_link(value)
    assert confirmed_notification_text("Лесной предел", value) == (
        "Вы подтвердили своё участие в игре Лесной предел! "
        f"Пожалуйста, добавьтесь в чат игры, мы вас очень ждём: {value}"
    )


@pytest.mark.parametrize(
    "value",
    [
        "Встречаемся у главного входа в 18:30",
        "Чат игры: https://t.me/+GameChat_123",
        "https://vk.com/id1",
        "https://example.com/join/GameChat_123",
        "https://t.me/+GameChat_123?start=1",
        "https://t.me:443/+GameChat_123",
        "https://vk.me/join//GameChat_123",
    ],
)
def test_arbitrary_or_non_chat_messages_stay_unchanged(value: str) -> None:
    assert not is_plain_chat_link(value)
    assert confirmed_notification_text("Лесной предел", value) == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_text"),
    [
        ("Сбор игроков в 18:30 у главного входа.", "Сбор игроков в 18:30 у главного входа."),
        (
            "https://vk.me/join/GameChat_123-=",
            "Вы подтвердили своё участие в игре Лесной предел! Пожалуйста, добавьтесь в чат игры, "
            "мы вас очень ждём: https://vk.me/join/GameChat_123-=",
        ),
    ],
)
async def test_confirmed_notifications_go_only_to_confirmed_players(
    message: str,
    expected_text: str,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(events, tables, showcase)
    users = MemoryUserRepository()
    waiting_user = TelegramUser(tg_id=2)
    confirmed_users = [TelegramUser(tg_id=3), VkUser(vk_id=4)]
    cancelled_user = VkUser(vk_id=5)
    for user in [waiting_user, *confirmed_users, cancelled_user, TelegramUser(tg_id=1)]:
        await users.save(user)
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        uid = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
        if uid == 1:
            continue
        key = participant_key(secret, platform, uid, event.event_id)
        await tables.enlist(
            event.event_id,
            operation_id=f"enlist-{platform.value}-{uid}",
            participant_key=key,
            display_name=f"Player {uid}",
            wish_play="A",
        )
        if user in confirmed_users or user is cancelled_user:
            await tables.confirm(
                event.event_id,
                operation_id=f"confirm-{platform.value}-{uid}",
                participant_key=key,
                character_wish="Doctor",
            )
        if user is cancelled_user:
            await tables.cancel(
                event.event_id,
                operation_id="cancel-vk-5",
                participant_key=key,
            )

    envelope = queued(event, Operation.SEND_CONFIRMED_NOTIFICATION, NotificationPayload(text=message), 1)
    consumer = FakeConsumer([envelope])
    transport = MemoryDeferredTransport()
    notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, catalog),
        users,
        transport,
        notifications,
        max_seconds=2,
    )

    assert await worker.run() == 1
    assert {(sent[0], sent[1], sent[3]) for sent in transport.sent} == {
        (Platform.TELEGRAM, 3, expected_text),
        (Platform.VK, 4, expected_text),
    }
    assert all(
        [(button.label, button.value) for button in sent[4]] == [(MAIN_MENU, MAIN_MENU)] for sent in transport.sent
    )
    assert consumer.deleted == ["receipt-1"]
