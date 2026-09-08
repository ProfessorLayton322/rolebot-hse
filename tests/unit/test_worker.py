from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

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
    BotIdentity,
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
    Registration,
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
            if operation
            in {
                Operation.ENLIST,
                Operation.UPDATE_PROFILE,
                Operation.CONFIRM,
                Operation.UPDATE_CHARACTER_WISH,
                Operation.CANCEL,
            }
            else None
        ),
        payload=payload,
        reply_context=ReplyContext(buttons=list(buttons)),
    )
    return QueueEnvelope(command=command, receipt_handle=f"receipt-{index}")


async def add_command_author_as_leader(events: MemoryEventRepository, event: Event) -> None:
    await events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=1),
    )


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
        queued(
            event,
            Operation.CANCEL,
            EmptyPayload(),
            4,
            [Button(label=MAIN_MENU, value=MAIN_MENU)],
        ),
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
    assert [(button.label, button.value) for button in transport.sent[0][4]] == [
        (CONFIRM_PARTICIPATION, f"select:confirm:{event.event_id}")
    ]
    saved_user = await users.get(Platform.TELEGRAM, 1)
    assert saved_user is not None
    assert saved_user.last_bot_buttons == [Button(label=MAIN_MENU, value=MAIN_MENU)]


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
async def test_stale_profile_update_failure_is_silent(disk_store: MemoryDiskStore, event: Event) -> None:
    event.archived_at = datetime.now(UTC)
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    consumer = FakeConsumer([queued(event, Operation.UPDATE_PROFILE, EmptyPayload(), 1)])
    transport = MemoryDeferredTransport()
    events = MemoryEventRepository([event])
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(
            events,
            RegistrationCatalog(events, MemoryRegistrationRepository(), YandexDiskShowcaseRepository(disk_store)),
            users,
            "participant-secret",
        ),
        users,
        transport,
        max_seconds=2,
    )

    assert await worker.run() == 1
    assert consumer.deleted == ["receipt-1"]
    assert transport.sent == []


@pytest.mark.asyncio
async def test_enlist_delivery_reports_main_or_reserve_position_from_ordered_state(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    event.player_amount = 1
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, player_amount=event.player_amount)
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    await users.save(TelegramUser(tg_id=2))
    commands = [
        OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=event.event_id,
            operation=Operation.ENLIST,
            platform=Platform.TELEGRAM,
            platform_user_id=user_id,
            participant_key=character * 43,
            payload=EnlistPayload(display_name=f"Player {user_id}", wish_play="A"),
            reply_context=ReplyContext(text_success="legacy signup response"),
        )
        for user_id, character in ((1, "a"), (2, "b"))
    ]
    consumer = FakeConsumer(
        [QueueEnvelope(command=command, receipt_handle=f"receipt-{index}") for index, command in enumerate(commands)]
    )
    transport = MemoryDeferredTransport()
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, RegistrationCatalog(events, tables, showcase)),
        users,
        transport,
        max_seconds=2,
    )

    assert await worker.run() == 2
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.public_table_public_url is not None
    link = stored_event.public_table_public_url
    assert transport.sent[0][3] == (
        f"Вы записались на игру {event.name}! Вы попали в основной состав игровой сессии!\n\n"
        f"Посмотрите, кто ещё зарегистрирован вместе с вами: {link}\n\n"
        "Когда вы будете уверены, что сможете прийти на нашу игру - подтвердите своё участие"
    )
    assert transport.sent[1][3] == (
        f"Вы записались на игру {event.name}! Прямо сейчас вы находитесь в запасе, но сможете попасть "
        "в основную игровую сессию, если 1 человек до вас отменят своё участие.\n\n"
        f"Подробнее можете посмотреть в таблице игроков: {link}\n\n"
        "Когда вы будете уверены, что сможете прийти на нашу игру - подтвердите своё участие"
    )
    assert all(
        [(button.label, button.value) for button in sent[4]]
        == [(CONFIRM_PARTICIPATION, f"select:confirm:{event.event_id}")]
        for sent in transport.sent
    )


@pytest.mark.asyncio
async def test_enlist_delivery_uses_closed_confirmation_text_and_keeps_default_button(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    event.status = EventStatus.CREATED
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    consumer = FakeConsumer(
        [
            queued(
                event,
                Operation.ENLIST,
                EnlistPayload(display_name="Player", wish_play="A"),
                1,
                [Button(label=MAIN_MENU, value=MAIN_MENU)],
            )
        ]
    )
    transport = MemoryDeferredTransport()
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, RegistrationCatalog(events, tables, showcase)),
        users,
        transport,
        max_seconds=2,
    )

    assert await worker.run() == 1
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.public_table_public_url is not None
    assert transport.sent[0][3] == (
        f"Вы записались на игру {event.name}! Вы попали в основной состав игровой сессии!\n\n"
        "Посмотрите, кто ещё зарегистрирован вместе с вами: "
        f"{stored_event.public_table_public_url}\n\n"
        "Игра пока закрыта для подтверждения участия. Мы обязательно пришлём вам уведомление, "
        "когда можно будет подтвердиться - пожалуйста, не забудьте это сделать, когда будете "
        "уверены, что сможете прийти на нашу игру"
    )
    assert transport.sent[0][4] == (Button(label=MAIN_MENU, value=MAIN_MENU),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "actor_id"),
    [(Operation.CANCEL, 1), (Operation.REMOVE_PARTICIPANT, 9)],
)
async def test_reserve_promotion_is_notified_exactly_once_after_cancel_or_removal(
    operation: Operation,
    actor_id: int,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    event.player_amount = 1
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, player_amount=event.player_amount)
    events = MemoryEventRepository([event])
    if operation is Operation.REMOVE_PARTICIPANT:
        await events.add_leader(
            event.event_id,
            BotIdentity(platform=Platform.TELEGRAM, platform_user_id=actor_id),
        )
    first_key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    reserve_key = participant_key(secret, Platform.TELEGRAM, 2, event.event_id)
    signed_up_at = datetime(2026, 9, 1, tzinfo=UTC)
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=first_key,
                display_name="First",
                wish_play="A",
                created_at=signed_up_at,
            ),
            Registration(
                event_id=event.event_id,
                participant_key=reserve_key,
                display_name="Reserve",
                wish_play="A",
                created_at=signed_up_at + timedelta(minutes=1),
            ),
        ]
    )
    users = MemoryUserRepository()
    for user_id in {1, 2, actor_id}:
        await users.save(TelegramUser(tg_id=user_id))
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=operation,
        platform=Platform.TELEGRAM,
        platform_user_id=actor_id,
        participant_key=first_key,
        payload=EmptyPayload(),
        reply_context=ReplyContext(text_success="Операция завершена"),
    )
    catalog = RegistrationCatalog(events, tables, showcase)
    transport = MemoryDeferredTransport()
    notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
    mutations = OrderedMutationService(events, catalog)

    first_consumer = FakeConsumer([QueueEnvelope(command=command, receipt_handle="receipt-first")])
    first_worker = OrderedWorker(
        first_consumer,
        mutations,
        users,
        transport,
        notifications,
        max_seconds=2,
    )
    assert await first_worker.run() == 1
    expected = []
    if operation is Operation.REMOVE_PARTICIPANT:
        expected.append((1, f"Ваша заявка на игру {event.name} была удалена"))
    expected.extend(
        [
            (
                2,
                f"Вы попали в основной состав игровой сессии игры {event.name}!\n\n"
                "Посмотрите актуальный состав игроков: https://disk.example/public/2",
            ),
            (actor_id, "Операция завершена"),
        ]
    )
    assert [(sent[1], sent[3]) for sent in transport.sent] == expected

    # Unrelated later deliveries must not erase the dedicated promotion marker.
    await users.claim_delivery(Platform.TELEGRAM, 2, "unrelated-delivery")
    if operation is Operation.REMOVE_PARTICIPANT:
        await users.claim_delivery(Platform.TELEGRAM, 1, "unrelated-removed-player-delivery")
    retry_consumer = FakeConsumer([QueueEnvelope(command=command, receipt_handle="receipt-retry")])
    retry_worker = OrderedWorker(
        retry_consumer,
        mutations,
        users,
        transport,
        notifications,
        max_seconds=2,
    )
    assert await retry_worker.run() == 1
    assert len(transport.sent) == len(expected)
    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        assert workbook.active["B2"].value == "Reserve"
        assert workbook.active["A3"].value == "ЗАПАС"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_bulk_removal_notifies_each_waiting_player_exactly_once(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    users = MemoryUserRepository()
    players = [TelegramUser(tg_id=2), VkUser(vk_id=3), TelegramUser(tg_id=4)]
    await users.save(TelegramUser(tg_id=1))
    for player in players:
        await users.save(player)
    waiting_keys = [
        participant_key(secret, Platform.TELEGRAM, 2, event.event_id),
        participant_key(secret, Platform.VK, 3, event.event_id),
    ]
    confirmed_key = participant_key(secret, Platform.TELEGRAM, 4, event.event_id)
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=key,
                display_name=f"Waiting {index}",
                wish_play="A",
            )
            for index, key in enumerate(waiting_keys)
        ]
        + [
            Registration(
                event_id=event.event_id,
                participant_key=confirmed_key,
                display_name="Confirmed",
                wish_play="A",
                attendance_status=AttendanceStatus.CONFIRMED,
            )
        ]
    )
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=Operation.REMOVE_WAITING_PARTICIPANTS,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        payload=EmptyPayload(),
    )
    catalog = RegistrationCatalog(events, tables, showcase)
    transport = MemoryDeferredTransport()
    notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
    mutations = OrderedMutationService(events, catalog)

    consumer = FakeConsumer([QueueEnvelope(command=command, receipt_handle="receipt-first")])
    worker = OrderedWorker(consumer, mutations, users, transport, notifications, max_seconds=2)
    assert await worker.run() == 1

    notification = (
        f"Ваша завяка на игру {event.name} была аннулирована, поскольку вы не успели "
        "подтвердить или отменить участие до дедлайна"
    )
    assert [(sent[0], sent[1], sent[3]) for sent in transport.sent] == [
        (Platform.TELEGRAM, 2, notification),
        (Platform.VK, 3, notification),
        (Platform.TELEGRAM, 1, "Удалено не подтвердивших участие: 2"),
    ]
    assert await tables.get(event.event_id, confirmed_key) is not None
    assert [await tables.get(event.event_id, key) for key in waiting_keys] == [None, None]

    await users.claim_delivery(Platform.TELEGRAM, 2, "unrelated-telegram-delivery")
    await users.claim_delivery(Platform.VK, 3, "unrelated-vk-delivery")
    retry = FakeConsumer([QueueEnvelope(command=command, receipt_handle="receipt-retry")])
    retry_worker = OrderedWorker(retry, mutations, users, transport, notifications, max_seconds=2)
    assert await retry_worker.run() == 1
    assert len(transport.sent) == 3


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
    await add_command_author_as_leader(events, event)
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
    for waiting_user in waiting_users:
        platform = Platform.TELEGRAM if isinstance(waiting_user, TelegramUser) else Platform.VK
        uid = waiting_user.tg_id if isinstance(waiting_user, TelegramUser) else waiting_user.vk_id
        saved_user = await users.get(platform, uid)
        assert saved_user is not None
        assert saved_user.last_bot_buttons == [
            Button(label=CONFIRM_PARTICIPATION, value=f"select:confirm:{event.event_id}")
        ]
    assert consumer.deleted == ["receipt-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "operation", "payload"),
    [
        (EventStatus.CREATED, Operation.OPEN_REGISTRATION, EmptyPayload()),
        (
            EventStatus.CONFIRMATION_OPEN,
            Operation.OPEN_CONFIRMATION,
            ConfirmationDeadlinePayload(deadline=datetime(2026, 9, 10, 16, tzinfo=UTC)),
        ),
        (EventStatus.CLOSED, Operation.CLOSE_EVENT, EmptyPayload()),
    ],
)
async def test_unchanged_game_status_sends_no_notifications(
    status: EventStatus,
    operation: Operation,
    payload: ConfirmationDeadlinePayload | EmptyPayload,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    event.status = status
    event.registrations_migrated_at = datetime.now(UTC)
    if isinstance(payload, ConfirmationDeadlinePayload):
        event.confirmation_deadline = payload.deadline
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    tables = MemoryRegistrationRepository()
    catalog = RegistrationCatalog(events, tables, YandexDiskShowcaseRepository(disk_store))
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    await users.save(TelegramUser(tg_id=2))
    await tables.enlist(
        event.event_id,
        operation_id="enlist-player",
        participant_key=participant_key(secret, Platform.TELEGRAM, 2, event.event_id),
        display_name="Player",
        wish_play="A",
    )
    consumer = FakeConsumer([queued(event, operation, payload, 1)])
    transport = MemoryDeferredTransport()
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, catalog),
        users,
        transport,
        ConfirmationNotificationService(events, catalog, users, transport, secret),
        max_seconds=2,
    )

    assert await worker.run() == 1
    assert transport.sent == []
    assert consumer.deleted == ["receipt-1"]


@pytest.mark.parametrize(
    "value",
    [
        "https://t.me/+GameChat_123",
        "https://telegram.me/joinchat/GameChat-123",
        "https://t.me/public_game_chat",
        "https://vk.me/join/GameChat_123-=",
        "https://vk.me/join/AJQ1d4V/FAtt9xoXEwCAfx1q",
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
def test_arbitrary_or_non_chat_messages_get_broadcast_disclaimer(value: str) -> None:
    assert not is_plain_chat_link(value)
    assert confirmed_notification_text("Лесной предел", value) == (
        "Мастера разослали это сообщение всем подтвердившимся на игру Лесной предел:\n\n" + value
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_text"),
    [
        (
            "Сбор игроков в 18:30 у главного входа.",
            "Мастера разослали это сообщение всем подтвердившимся на игру Лесной предел:\n\n"
            "Сбор игроков в 18:30 у главного входа.",
        ),
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
    event.player_amount = 1
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
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
    expected_recipients = {(Platform.TELEGRAM, 3, expected_text)}
    if not is_plain_chat_link(message):
        expected_recipients.add((Platform.VK, 4, expected_text))
    assert {(sent[0], sent[1], sent[3]) for sent in transport.sent} == expected_recipients
    assert all(
        [(button.label, button.value) for button in sent[4]] == [(MAIN_MENU, MAIN_MENU)] for sent in transport.sent
    )
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.confirmed_notifications == [message]
    assert consumer.deleted == ["receipt-1"]


@pytest.mark.asyncio
async def test_newly_confirmed_participant_receives_all_stored_notifications(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, showcase)
    users = MemoryUserRepository()
    player = TelegramUser(tg_id=7)
    await users.save(player)
    key = participant_key(secret, Platform.TELEGRAM, player.tg_id, event.event_id)
    await tables.enlist(
        event.event_id,
        operation_id="enlist-player-7",
        participant_key=key,
        display_name="Player 7",
        wish_play="A",
    )

    confirm_operation_id = str(uuid4())
    confirm = QueueEnvelope(
        command=OrderedRegistrationCommand(
            operation_id=confirm_operation_id,
            event_id=event.event_id,
            operation=Operation.CONFIRM,
            platform=Platform.TELEGRAM,
            platform_user_id=player.tg_id,
            participant_key=key,
            payload=CharacterWishPayload(character_wish="Doctor"),
            reply_context=ReplyContext(
                text_success="✅ Участие подтверждено.",
                buttons=[Button(label=MAIN_MENU, value=MAIN_MENU)],
            ),
        ),
        receipt_handle="receipt-3",
    )
    consumer = FakeConsumer(
        [
            queued(
                event,
                Operation.SEND_CONFIRMED_NOTIFICATION,
                NotificationPayload(text="Сбор в 18:30"),
                1,
            ),
            queued(
                event,
                Operation.SEND_CONFIRMED_NOTIFICATION,
                NotificationPayload(text="https://t.me/+GameChat_123"),
                2,
            ),
            confirm,
        ]
    )
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

    assert await worker.run() == 3
    assert [(sent[1], sent[3]) for sent in transport.sent] == [
        (player.tg_id, "✅ Участие подтверждено."),
        (
            player.tg_id,
            "Мастера разослали это сообщение всем подтвердившимся на игру Лесной предел:\n\nСбор в 18:30",
        ),
        (
            player.tg_id,
            "Вы подтвердили своё участие в игре Лесной предел! Пожалуйста, добавьтесь в чат игры, "
            "мы вас очень ждём: https://t.me/+GameChat_123",
        ),
    ]
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.confirmed_notifications == [
        "Сбор в 18:30",
        "https://t.me/+GameChat_123",
    ]
    saved_player = await users.get(Platform.TELEGRAM, player.tg_id)
    assert saved_player is not None
    assert saved_player.last_delivery_operation_id == confirm_operation_id
    assert saved_player.last_bot_buttons == [Button(label=MAIN_MENU, value=MAIN_MENU)]
    assert consumer.deleted == ["receipt-1", "receipt-2", "receipt-3"]


@pytest.mark.asyncio
async def test_confirmed_reserve_gets_information_then_chat_link_after_cancellation(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    event.player_amount = 2
    event.confirmed_notifications = ["Сбор в 18:30", "https://t.me/+GameChat_123"]
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, showcase)
    users = MemoryUserRepository()
    keys: dict[int, str] = {}
    for user_id in (2, 3, 4):
        await users.save(TelegramUser(tg_id=user_id))
        key = participant_key(secret, Platform.TELEGRAM, user_id, event.event_id)
        keys[user_id] = key
        await tables.enlist(
            event.event_id,
            operation_id=f"enlist-{user_id}",
            participant_key=key,
            display_name=f"Player {user_id}",
            wish_play="A",
        )
        if user_id < 4:
            await tables.confirm(
                event.event_id,
                operation_id=f"confirm-{user_id}",
                participant_key=key,
                character_wish="Doctor",
            )

    confirm = QueueEnvelope(
        command=OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=event.event_id,
            operation=Operation.CONFIRM,
            platform=Platform.TELEGRAM,
            platform_user_id=4,
            participant_key=keys[4],
            payload=CharacterWishPayload(character_wish="Doctor"),
            reply_context=ReplyContext(text_success="✅ Участие подтверждено."),
        ),
        receipt_handle="receipt-confirm-reserve",
    )
    cancel = QueueEnvelope(
        command=OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=event.event_id,
            operation=Operation.CANCEL,
            platform=Platform.TELEGRAM,
            platform_user_id=2,
            participant_key=keys[2],
            payload=EmptyPayload(),
            reply_context=ReplyContext(text_success="✅ Участие отменено."),
        ),
        receipt_handle="receipt-cancel-main",
    )
    consumer = FakeConsumer([confirm, cancel])
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

    assert await worker.run() == 2
    reserve_messages = [sent[3] for sent in transport.sent if sent[1] == 4]
    assert reserve_messages[:2] == [
        "✅ Участие подтверждено.",
        "Мастера разослали это сообщение всем подтвердившимся на игру Лесной предел:\n\nСбор в 18:30",
    ]
    assert not any("добавьтесь в чат" in text for text in reserve_messages[:2])
    assert reserve_messages[-1] == (
        "Вы подтвердили своё участие в игре Лесной предел! Пожалуйста, добавьтесь в чат игры, "
        "мы вас очень ждём: https://t.me/+GameChat_123"
    )
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.last_confirmed_link_promotion_participant_key == keys[4]
    assert stored_event.last_confirmed_link_promotion_delivered_operation_id == cancel.command.operation_id
