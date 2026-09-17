from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import cast
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
from larp_bot.application.navigation import CONFIRM_PARTICIPATION, MAIN_MENU, MAIN_MENU_LABEL
from larp_bot.application.services import (
    ConfirmationNotificationService,
    OrderedMutationService,
    RegistrationCatalog,
    confirmation_success_text,
    confirmed_notification_text,
    is_plain_chat_link,
)
from larp_bot.application.statistics import StatisticsService
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
    PlayerAmountPayload,
    ProfileSyncPayload,
    Registration,
    ReplyContext,
    TelegramUser,
    VkUser,
)
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore

NO_MASTERS_SECTION = (
    "Если вы решите не участвовать в игре, пожалуйста сообщите об этом мастерам. "
    "Если вы не придете на игру, ничего нам не сообщив, к вам могут быть применены санкции Ролевого Клуба"
)


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


class FailingProfileStatistics:
    def __init__(
        self,
        registrations: MemoryRegistrationRepository,
        event_id: str,
        participant_key: str,
    ) -> None:
        self.registrations = registrations
        self.event_id = event_id
        self.participant_key = participant_key
        self.signup_was_persisted = False

    async def sync_profile(self, command: OrderedRegistrationCommand) -> str:
        del command
        self.signup_was_persisted = await self.registrations.get(self.event_id, self.participant_key) is not None
        raise RuntimeError("statistics unavailable")


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
            [Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)],
        ),
        queued(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A"), 2),
        queued(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B"), 3),
        queued(
            event,
            Operation.CANCEL,
            EmptyPayload(),
            4,
            [Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)],
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
    assert saved_user.last_bot_buttons == [Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)]


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
async def test_profile_sync_failure_does_not_block_signup_in_an_independent_fifo_group(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    sync = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id="profile:telegram:1",
        operation=Operation.SYNC_PROFILE,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        payload=ProfileSyncPayload(names=["Player"]),
    )
    enlist = queued(
        event,
        Operation.ENLIST,
        EnlistPayload(display_name="Player", wish_play="A"),
        2,
        [Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)],
    )
    consumer = FakeConsumer([QueueEnvelope(sync, "receipt-sync"), enlist])
    transport = MemoryDeferredTransport()
    statistics = FailingProfileStatistics(tables, event.event_id, "a" * 43)
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(
            events := MemoryEventRepository([event]),
            RegistrationCatalog(events, tables, showcase),
        ),
        users,
        transport,
        max_seconds=2,
        statistics=cast(StatisticsService, statistics),
    )

    with pytest.raises(RuntimeError, match="statistics unavailable"):
        await worker.run()

    assert statistics.signup_was_persisted
    assert consumer.deleted == ["receipt-2"]
    assert len(transport.sent) == 1
    assert "Вы записались" in transport.sent[0][3]


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
    event.confirmation_deadline = datetime(2026, 9, 10, 16, tzinfo=UTC)
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
        f"Вы записались на игру «{event.name}»!\n\n"
        "Сейчас вы в основном составе.\n\n"
        f"Актуальный список записавшихся и состав можно посмотреть здесь: {link}\n\n"
        "Когда будете уверены, что сможете прийти на игру, подтвердите участие до 10.09.26 19:00 (мск).\n\n"
        "Пожелания по соигрокам всегда можно изменить позже через «🎭 Пожелания по игре».\n\n"
        f"{NO_MASTERS_SECTION}"
    )
    assert transport.sent[1][3] == (
        f"Вы записались на игру «{event.name}»!\n\n"
        "Сейчас вы в запасе. Перед вами: 1.\n\n"
        "Если вы перейдёте в основной состав, бот сообщит об этом отдельным сообщением.\n\n"
        f"Актуальный список записавшихся и состав можно посмотреть здесь: {link}\n\n"
        "Когда будете уверены, что сможете прийти на игру, подтвердите участие до 10.09.26 19:00 (мск).\n\n"
        "Пожелания по соигрокам всегда можно изменить позже через «🎭 Пожелания по игре».\n\n"
        f"{NO_MASTERS_SECTION}"
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
                [Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)],
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
        f"Вы записались на игру «{event.name}»!\n\n"
        "Сейчас вы в основном составе.\n\n"
        "Актуальный список записавшихся и состав можно посмотреть здесь: "
        f"{stored_event.public_table_public_url}\n\n"
        "Подтверждение участия пока не открыто. Мы обязательно пришлём вам уведомление, "
        "когда можно будет подтвердиться.\n\n"
        "Пожелания по соигрокам всегда можно изменить позже через «🎭 Пожелания по игре».\n\n"
        f"{NO_MASTERS_SECTION}"
    )
    assert transport.sent[0][4] == (Button(label=MAIN_MENU_LABEL, value=MAIN_MENU),)


@pytest.mark.asyncio
async def test_signup_and_confirmation_list_masters_and_warn_about_no_shows(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    catalog = RegistrationCatalog(events, MemoryRegistrationRepository(), showcase)
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Иван Иванов",
            vk_url="https://vk.com/id1",
            crossplay=True,
            larp_experience=True,
            needs_pass=False,
        )
    )
    await users.save(TelegramUser(tg_id=21, full_name="Смирнова Анна Петровна", telegram_handle="@anna_master"))
    await users.save(VkUser(vk_id=42, full_name="Орлов Борис"))
    await users.save(TelegramUser(tg_id=23, full_name="Жукова Вера"))
    # VK master 77 has no bot profile and Telegram master 23 has no public username.
    for platform, master_id in ((Platform.TELEGRAM, 21), (Platform.VK, 42), (Platform.TELEGRAM, 23), (Platform.VK, 77)):
        await events.add_leader(event.event_id, BotIdentity(platform=platform, platform_user_id=master_id))
    key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    payloads: list[tuple[Operation, EnlistPayload | CharacterWishPayload]] = [
        (Operation.ENLIST, EnlistPayload(display_name="Иван Иванов", wish_play="A")),
        (Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")),
    ]
    consumer = FakeConsumer(
        [
            QueueEnvelope(
                command=OrderedRegistrationCommand(
                    operation_id=str(uuid4()),
                    event_id=event.event_id,
                    operation=operation,
                    platform=Platform.TELEGRAM,
                    platform_user_id=1,
                    participant_key=key,
                    payload=payload,
                    reply_context=ReplyContext(buttons=[Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)]),
                ),
                receipt_handle=f"receipt-{index}",
            )
            for index, (operation, payload) in enumerate(payloads)
        ]
    )
    transport = MemoryDeferredTransport()
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, catalog, users, secret),
        users,
        transport,
        ConfirmationNotificationService(events, catalog, users, transport, secret),
        max_seconds=2,
    )

    assert await worker.run() == 2
    masters = (
        "Мастера игры:\n"
        "https://vk.com/id77\n"
        "Жукова Вера\n"
        "Орлов Борис — https://vk.com/id42\n"
        "Смирнова Анна — https://t.me/anna_master\n\n"
        "Если вы решите не участвовать в игре, пожалуйста сообщите об этом мастерам: "
        "https://vk.com/id77 https://vk.com/id42 https://t.me/anna_master . "
        "Если вы не придете на игру, ничего нам не сообщив, к вам могут быть применены санкции Ролевого Клуба"
    )
    signup, confirmation = (sent[3] for sent in transport.sent)
    assert signup.startswith(f"Вы записались на игру «{event.name}»!\n\nСейчас вы в основном составе.\n\n")
    assert signup.endswith(f"«🎭 Пожелания по игре».\n\n{masters}")
    assert confirmation == (
        f"✅ Участие в игре «{event.name}» подтверждено.\n\n"
        "Мастера скоро отправят вам ссылку на чат игры!\n\n"
        "Пожелания по персонажу:\nDoctor\n\n"
        "Позже их можно изменить через «🎭 Пожелания по игре».\n\n"
        f"{masters}"
    )


def test_confirmation_text_shortens_only_the_wish_echo_to_fit_the_message_limit(event: Event) -> None:
    wish = "🎭" * 3000
    masters = [(BotIdentity(platform=Platform.VK, platform_user_id=42), VkUser(vk_id=42, full_name="Орлов Борис"))]

    text = confirmation_success_text(event, wish, in_main_roster=True, masters=masters)

    echo = text.split("Пожелания по персонажу:\n", 1)[1].split("\n\nПозже их можно изменить", 1)[0]
    assert 4095 <= len(text.encode("utf-16-le")) // 2 <= 4096
    assert echo.endswith("…")
    assert wish.startswith(echo.removesuffix("…"))
    assert text.endswith(
        "Орлов Борис — https://vk.com/id42\n\n"
        "Если вы решите не участвовать в игре, пожалуйста сообщите об этом мастерам: https://vk.com/id42 . "
        "Если вы не придете на игру, ничего нам не сообщив, к вам могут быть применены санкции Ролевого Клуба"
    )


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
        expected.append((1, f"Ваша запись на игру «{event.name}» удалена."))
    expected.extend(
        [
            (
                2,
                f"Место освободилось: вы перешли из запаса в основной состав игры «{event.name}».\n\n"
                "После подтверждения участия мастера отправят вам ссылку на чат игры отдельным сообщением.\n\n"
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
async def test_capacity_increase_notifies_every_promoted_player_exactly_once(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    actor_id = 9
    event.player_amount = 1
    event.confirmed_notifications = ["https://t.me/+GameChat_123"]
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, player_amount=event.player_amount)
    events = MemoryEventRepository([event])
    await events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=actor_id),
    )
    identities = [
        (Platform.TELEGRAM, 1),
        (Platform.TELEGRAM, 2),
        (Platform.VK, 3),
        (Platform.TELEGRAM, 4),
    ]
    keys = [participant_key(secret, platform, user_id, event.event_id) for platform, user_id in identities]
    signed_up_at = datetime(2026, 9, 1, tzinfo=UTC)
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=key,
                display_name=f"Player {index}",
                wish_play="A",
                attendance_status=(AttendanceStatus.CONFIRMED if index == 3 else AttendanceStatus.WAITING),
                created_at=signed_up_at + timedelta(minutes=index),
            )
            for index, key in enumerate(keys, start=1)
        ]
    )
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=actor_id))
    for platform, user_id in identities:
        if platform is Platform.TELEGRAM:
            await users.save(TelegramUser(tg_id=user_id))
        else:
            await users.save(VkUser(vk_id=user_id))
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=Operation.UPDATE_PLAYER_AMOUNT,
        platform=Platform.TELEGRAM,
        platform_user_id=actor_id,
        payload=PlayerAmountPayload(player_amount=3),
        reply_context=ReplyContext(text_success="Количество игроков изменено: 3"),
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

    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    promotion = (
        f"Количество мест увеличилось: вы перешли из запаса в основной состав игры «{event.name}».\n\n"
        f"Посмотрите актуальный состав игроков: {stored_event.public_table_public_url}"
    )
    assert [(sent[0], sent[1], sent[3]) for sent in transport.sent] == [
        (Platform.TELEGRAM, 2, promotion),
        (Platform.VK, 3, promotion),
        (
            Platform.VK,
            3,
            confirmed_notification_text(event.name, event.confirmed_notifications[0]),
        ),
        (Platform.TELEGRAM, actor_id, "Количество игроков изменено: 3"),
    ]
    assert stored_event.last_participant_removal_operation_id == command.operation_id
    assert stored_event.participant_removal_keys == keys[1:3]
    assert stored_event.participant_removal_notified_keys == keys[1:3]

    for platform, user_id in identities[1:3]:
        await users.claim_delivery(platform, user_id, f"unrelated-{platform.value}-{user_id}")
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
    assert len(transport.sent) == 4


@pytest.mark.asyncio
async def test_confirmation_sift_notifies_promoted_and_demoted_players_exactly_once(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    base = datetime(2026, 1, 1, tzinfo=UTC)
    event.player_amount = 2
    event.confirmed_notifications = ["https://t.me/+GameChat_123"]
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    users = MemoryUserRepository()
    players: list[TelegramUser | VkUser] = [
        TelegramUser(tg_id=2),
        VkUser(vk_id=3),
        TelegramUser(tg_id=4),
    ]
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
                created_at=base + timedelta(seconds=index),
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
                created_at=base + timedelta(seconds=2),
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

    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    promotion = (
        f"Вы перешли из запаса в основной состав игры «{event.name}».\n\n"
        f"Посмотрите актуальный состав игроков: {stored_event.public_table_public_url}"
    )
    demotion = (
        f"Вы не успели подтвердить или отменить ваше участие в игре «{event.name}». "
        "Мы были вынуждены переместить вашу заявку в запас"
    )
    assert [(sent[0], sent[1], sent[3]) for sent in transport.sent] == [
        (Platform.TELEGRAM, 4, promotion),
        (
            Platform.TELEGRAM,
            4,
            confirmed_notification_text(event.name, event.confirmed_notifications[0]),
        ),
        (Platform.VK, 3, demotion),
        (
            Platform.TELEGRAM,
            1,
            "Заявки пересортированы: в основной состав перемещено 1, в запас — 1",
        ),
    ]
    assert await tables.get(event.event_id, confirmed_key) is not None
    assert all(
        registration is not None for registration in [await tables.get(event.event_id, key) for key in waiting_keys]
    )
    assert [row.participant_key for row in await tables.list_for_event(event.event_id)] == [
        confirmed_key,
        *waiting_keys,
    ]

    await users.claim_delivery(Platform.TELEGRAM, 4, "unrelated-telegram-delivery")
    await users.claim_delivery(Platform.VK, 3, "unrelated-vk-delivery")
    retry = FakeConsumer([QueueEnvelope(command=command, receipt_handle="receipt-retry")])
    retry_worker = OrderedWorker(retry, mutations, users, transport, notifications, max_seconds=2)
    assert await retry_worker.run() == 1
    assert len(transport.sent) == 4


class SiftFailingOnceRegistrationRepository(MemoryRegistrationRepository):
    def __init__(self, registrations: Sequence[Registration]) -> None:
        super().__init__(registrations)
        self.sift_failures = 1

    async def sift_waiting_to_end(self, event_id: str) -> None:
        if self.sift_failures:
            self.sift_failures -= 1
            raise RuntimeError("Can't set NULL or optional value to not null column: created_at")
        await super().sift_waiting_to_end(event_id)


@pytest.mark.asyncio
async def test_confirmation_sift_retry_notifies_moves_of_the_current_roster(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    base = datetime(2026, 1, 1, tzinfo=UTC)
    event.player_amount = 2
    event.confirmed_notifications = ["https://t.me/+GameChat_123"]
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    users = MemoryUserRepository()
    identities = [(Platform.TELEGRAM, 2), (Platform.VK, 3), (Platform.TELEGRAM, 4), (Platform.TELEGRAM, 5)]
    await users.save(TelegramUser(tg_id=1))
    for platform, user_id in identities:
        await users.save(TelegramUser(tg_id=user_id) if platform is Platform.TELEGRAM else VkUser(vk_id=user_id))
    keys = [participant_key(secret, platform, user_id, event.event_id) for platform, user_id in identities]
    tables = SiftFailingOnceRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=key,
                display_name=f"Player {index}",
                wish_play="A",
                attendance_status=AttendanceStatus.CONFIRMED if index == 2 else AttendanceStatus.WAITING,
                created_at=base + timedelta(seconds=index),
            )
            for index, key in enumerate(keys)
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
    with pytest.raises(RuntimeError, match="not null column"):
        await worker.run()
    assert consumer.deleted == []
    assert transport.sent == []

    # The first attempt planned to demote player 1. While the per-event FIFO
    # group was blocked, that player confirmed through their own group, so the
    # retry moves player 0 into the reserve instead.
    stored_registration = tables.rows[(event.event_id, keys[1])]
    stored_registration.attendance_status = AttendanceStatus.CONFIRMED

    retry = FakeConsumer([QueueEnvelope(command=command, receipt_handle="receipt-retry")])
    retry_worker = OrderedWorker(retry, mutations, users, transport, notifications, max_seconds=2)
    assert await retry_worker.run() == 1

    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    promotion = (
        f"Вы перешли из запаса в основной состав игры «{event.name}».\n\n"
        f"Посмотрите актуальный состав игроков: {stored_event.public_table_public_url}"
    )
    demotion = (
        f"Вы не успели подтвердить или отменить ваше участие в игре «{event.name}». "
        "Мы были вынуждены переместить вашу заявку в запас"
    )
    assert [(sent[0], sent[1], sent[3]) for sent in transport.sent] == [
        (Platform.TELEGRAM, 4, promotion),
        (
            Platform.TELEGRAM,
            4,
            confirmed_notification_text(event.name, event.confirmed_notifications[0]),
        ),
        (Platform.TELEGRAM, 2, demotion),
        (
            Platform.TELEGRAM,
            1,
            "Заявки пересортированы: в основной состав перемещено 1, в запас — 1",
        ),
    ]
    assert retry.deleted == ["receipt-retry"]
    assert [row.participant_key for row in await tables.list_for_event(event.event_id)] == [
        keys[1],
        keys[2],
        keys[0],
        keys[3],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_text", "expected_completion_text"),
    [
        (
            Operation.OPEN_CONFIRMATION,
            "Подтверждение участия в игре «Лесной предел» открыто! Подтвердите участие до 10.09.26 19:00 (мск).",
            "Установлен Статус «Подтверждение»",
        ),
        (
            Operation.SEND_CONFIRMATION_REMINDER,
            "Напоминаем: подтвердите участие в игре «Лесной предел» до 10.09.26 19:00 (мск).",
            "Напоминание о подтверждении отправлено",
        ),
    ],
)
async def test_confirmation_notifications_go_to_waiting_players_before_master_acknowledgement(
    operation: Operation,
    expected_text: str,
    expected_completion_text: str,
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
    assert {(sent[0], sent[1], sent[3]) for sent in transport.sent[:-1]} == {
        (Platform.TELEGRAM, 2, expected_text),
        (Platform.VK, 3, expected_text),
    }
    assert transport.sent[-1][0:2] == (Platform.TELEGRAM, 1)
    assert transport.sent[-1][3] == expected_completion_text
    assert all(
        [(button.label, button.value) for button in sent[4]]
        == [(CONFIRM_PARTICIPATION, f"select:confirm:{event.event_id}")]
        for sent in transport.sent[:-1]
    )
    assert transport.sent[-1][4] == ()
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
        f"Вы в основном составе игры «Лесной предел». Вступите в чат: {value}"
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
    assert (
        confirmed_notification_text("Лесной предел", value) == f"Сообщение от мастеров игры «Лесной предел»:\n\n{value}"
    )


def test_game_date_follows_the_name_in_confirmed_player_notifications() -> None:
    assert (
        confirmed_notification_text(
            "Лесной предел",
            "Сбор в 18:30",
            date(2026, 11, 14),
        )
        == "Сообщение от мастеров игры «Лесной предел» (14.11.26):\n\nСбор в 18:30"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_text"),
    [
        (
            "Сбор игроков в 18:30 у главного входа.",
            "Сообщение от мастеров игры «Лесной предел»:\n\nСбор игроков в 18:30 у главного входа.",
        ),
        (
            "https://vk.me/join/GameChat_123-=",
            "Вы в основном составе игры «Лесной предел». Вступите в чат: https://vk.me/join/GameChat_123-=",
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
    expected_recipients: set[tuple[Platform, int, str]] = set()
    if not is_plain_chat_link(message):
        expected_recipients = {
            (Platform.TELEGRAM, 3, expected_text),
            (Platform.VK, 4, expected_text),
        }
    assert {(sent[0], sent[1], sent[3]) for sent in transport.sent[:-1]} == expected_recipients
    assert all(
        [(button.label, button.value) for button in sent[4]] == [(MAIN_MENU_LABEL, MAIN_MENU)]
        for sent in transport.sent[:-1]
    )
    assert transport.sent[-1][0:2] == (Platform.TELEGRAM, 1)
    assert transport.sent[-1][3] == "Уведомление подтвердившим участие отправлено"
    assert transport.sent[-1][4] == ()
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.confirmed_notifications == [message]
    assert consumer.deleted == ["receipt-1"]


@pytest.mark.asyncio
async def test_all_player_notifications_go_to_waiting_and_confirmed_players(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    tables = MemoryRegistrationRepository()
    catalog = RegistrationCatalog(events, tables, YandexDiskShowcaseRepository(disk_store))
    users = MemoryUserRepository()
    waiting_user = TelegramUser(tg_id=2)
    confirmed_user = VkUser(vk_id=3)
    cancelled_user = TelegramUser(tg_id=4)
    for user in [waiting_user, confirmed_user, cancelled_user, TelegramUser(tg_id=1)]:
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
        if user in (confirmed_user, cancelled_user):
            await tables.confirm(
                event.event_id,
                operation_id=f"confirm-{platform.value}-{uid}",
                participant_key=key,
                character_wish="Doctor",
            )
        if user is cancelled_user:
            await tables.cancel(
                event.event_id,
                operation_id="cancel-telegram-4",
                participant_key=key,
            )

    message = "Сбор игроков в 18:30 у главного входа."
    consumer = FakeConsumer(
        [queued(event, Operation.SEND_ALL_PLAYERS_NOTIFICATION, NotificationPayload(text=message), 1)]
    )
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
    expected_text = f"Сообщение от мастеров игры «{event.name}»:\n\n{message}"
    assert {(sent[0], sent[1], sent[3]) for sent in transport.sent[:-1]} == {
        (Platform.TELEGRAM, 2, expected_text),
        (Platform.VK, 3, expected_text),
    }
    assert all(sent[4] == (Button(label=MAIN_MENU_LABEL, value=MAIN_MENU),) for sent in transport.sent[:-1])
    assert transport.sent[-1][0:2] == (Platform.TELEGRAM, 1)
    assert transport.sent[-1][3] == "Уведомление всем игрокам отправлено"
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.confirmed_notifications == []
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
                buttons=[Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)],
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
        (1, "Уведомление подтвердившим участие отправлено"),
        (1, "Уведомление подтвердившим участие отправлено"),
        (
            player.tg_id,
            f"✅ Участие в игре «{event.name}» подтверждено.\n\n"
            "Мастера скоро отправят вам ссылку на чат игры!\n\n"
            "Пожелания по персонажу:\nDoctor\n\n"
            "Позже их можно изменить через «🎭 Пожелания по игре».\n\n"
            f"{NO_MASTERS_SECTION}",
        ),
        (
            player.tg_id,
            "Сообщение от мастеров игры «Лесной предел»:\n\nСбор в 18:30",
        ),
        (
            player.tg_id,
            "Вы в основном составе игры «Лесной предел». Вступите в чат: https://t.me/+GameChat_123",
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
    assert saved_player.last_bot_buttons == [Button(label=MAIN_MENU_LABEL, value=MAIN_MENU)]
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
            reply_context=ReplyContext(text_success="legacy confirmation with unconditional chat-link promise"),
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
        f"✅ Участие в игре «{event.name}» подтверждено.\n\n"
        "Пожелания по персонажу:\nDoctor\n\n"
        "Позже их можно изменить через «🎭 Пожелания по игре».\n\n"
        f"{NO_MASTERS_SECTION}",
        "Сообщение от мастеров игры «Лесной предел»:\n\nСбор в 18:30",
    ]
    assert not any("ссылку на чат" in text for text in reserve_messages[:2])
    assert reserve_messages[-2] == (
        f"Место освободилось: вы перешли из запаса в основной состав игры «{event.name}».\n\n"
        "После подтверждения участия мастера отправят вам ссылку на чат игры отдельным сообщением.\n\n"
        "Посмотрите актуальный состав игроков: https://disk.example/public/2"
    )
    assert reserve_messages[-1] == (
        "Вы в основном составе игры «Лесной предел». Вступите в чат: https://t.me/+GameChat_123"
    )
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.last_confirmed_link_promotion_participant_key == keys[4]
    assert stored_event.last_confirmed_link_promotion_delivered_operation_id == cancel.command.operation_id


class FailingOnceForRecipientTransport(MemoryDeferredTransport):
    def __init__(self, platform: Platform, user_id: int) -> None:
        super().__init__()
        self.failing: tuple[Platform, int] | None = (platform, user_id)

    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        if (platform, user_id) == self.failing:
            self.failing = None
            raise RuntimeError("transport unavailable")
        await super().send(platform=platform, user_id=user_id, request_id=request_id, text=text, buttons=buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attendance_status", "status_text"),
    [(AttendanceStatus.WAITING, "не подтверждено"), (AttendanceStatus.CONFIRMED, "подтверждено")],
    ids=["waiting", "confirmed"],
)
async def test_cancellation_notifies_every_other_master_with_the_player_status_exactly_once(
    attendance_status: AttendanceStatus,
    status_text: str,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    event.game_date = date(2026, 11, 14)
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    # The cancelling player also masters the game and must not be told about themselves.
    for platform, user_id in [(Platform.TELEGRAM, 1), (Platform.TELEGRAM, 7), (Platform.VK, 8)]:
        await events.add_leader(event.event_id, BotIdentity(platform=platform, platform_user_id=user_id))
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1, full_name="Иванов Иван"))
    await users.save(TelegramUser(tg_id=7, full_name="Петров Пётр"))
    await users.save(VkUser(vk_id=8, full_name="Андреев Андрей"))
    player_key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=player_key,
                display_name="Иванов Иван",
                nickname="Ваня",
                wish_play="A",
                attendance_status=attendance_status,
            )
        ]
    )
    catalog = RegistrationCatalog(events, tables, showcase)
    # Masters are notified in name order, so the second master's failure
    # leaves the first one already notified.
    transport = FailingOnceForRecipientTransport(Platform.TELEGRAM, 7)
    notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
    mutations = OrderedMutationService(events, catalog)

    def cancel_command() -> OrderedRegistrationCommand:
        return OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=event.event_id,
            operation=Operation.CANCEL,
            platform=Platform.TELEGRAM,
            platform_user_id=1,
            participant_key=player_key,
            payload=EmptyPayload(),
            reply_context=ReplyContext(text_success="✅ Участие отменено."),
        )

    def worker_for(command: OrderedRegistrationCommand, receipt: str) -> OrderedWorker:
        return OrderedWorker(
            FakeConsumer([QueueEnvelope(command=command, receipt_handle=receipt)]),
            mutations,
            users,
            transport,
            notifications,
            max_seconds=2,
        )

    command = cancel_command()
    with pytest.raises(RuntimeError, match="transport unavailable"):
        await worker_for(command, "receipt-first").run()
    cancelled = await tables.get(event.event_id, player_key)
    assert cancelled is not None
    assert cancelled.attendance_status is AttendanceStatus.CANCELLED

    master_text = (
        f"Отмена участия в игре «{event.name}» (14.11.26)\n\n"
        "Игрок: Иванов Иван\n"
        "Никнейм: Ваня\n"
        f"Статус участия: {status_text}"
    )
    assert [(sent[0], sent[1], sent[3], sent[4]) for sent in transport.sent] == [
        (Platform.VK, 8, master_text, (Button(label=MAIN_MENU_LABEL, value=MAIN_MENU),)),
    ]

    # The retry still reports the status the player had before cancelling, and
    # later unrelated deliveries must not make an already notified master repeat.
    await users.claim_delivery(Platform.VK, 8, "unrelated-delivery")
    assert await worker_for(command, "receipt-retry").run() == 1
    assert await worker_for(command, "receipt-second-retry").run() == 1
    assert [(sent[0], sent[1], sent[3]) for sent in transport.sent] == [
        (Platform.VK, 8, master_text),
        (Platform.TELEGRAM, 7, master_text),
        (Platform.TELEGRAM, 1, "✅ Участие отменено."),
    ]

    # Cancelling an already cancelled registration changes nothing for the masters.
    assert await worker_for(cancel_command(), "receipt-repeated-cancel").run() == 1
    assert [(sent[1], sent[3]) for sent in transport.sent[3:]] == [(1, "✅ Участие отменено.")]


@pytest.mark.asyncio
async def test_same_day_replacement_notifies_masters_of_the_replaced_game_exactly_once(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    secret = "participant-secret"
    event.game_date = date(2026, 11, 14)
    replaced_event = event.model_copy(
        update={
            "event_id": "event-b1",
            "name": "Другая игра",
            "disk_resource_path": "disk:/larp-bot/events/event-b1-other.xlsx",
            "public_registration_url": "https://disk.example/public/other",
        }
    )
    target_key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    replaced_key = participant_key(secret, Platform.TELEGRAM, 1, replaced_event.event_id)
    target_registration = Registration(
        event_id=event.event_id,
        participant_key=target_key,
        display_name="Иванов Иван",
        wish_play="A",
    )
    replaced_registration = Registration(
        event_id=replaced_event.event_id,
        participant_key=replaced_key,
        display_name="Иванов Иван",
        wish_play="A",
        character_wish="Врач",
        attendance_status=AttendanceStatus.CONFIRMED,
    )
    events = MemoryEventRepository([event, replaced_event])
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=9))
    await events.add_leader(replaced_event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=7))
    await events.add_leader(replaced_event.event_id, BotIdentity(platform=Platform.VK, platform_user_id=8))
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1, full_name="Иванов Иван Иванович"))
    await users.save(TelegramUser(tg_id=7, full_name="Петров Пётр"))
    await users.save(VkUser(vk_id=8, full_name="Андреев Андрей"))
    await users.save(TelegramUser(tg_id=9, full_name="Сидоров Сидор"))
    tables = MemoryRegistrationRepository([target_registration, replaced_registration])
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, [target_registration])
    await showcase.create_event_workbook(replaced_event.disk_resource_path, [replaced_registration])
    catalog = RegistrationCatalog(events, tables, showcase)
    transport = MemoryDeferredTransport()
    notifications = ConfirmationNotificationService(events, catalog, users, transport, secret)
    mutations = OrderedMutationService(events, catalog, users, secret)
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=Operation.CONFIRM,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key=target_key,
        payload=CharacterWishPayload(character_wish="Детектив", replaced_event_id=replaced_event.event_id),
    )

    for receipt in ("receipt-first", "receipt-retry"):
        worker = OrderedWorker(
            FakeConsumer([QueueEnvelope(command=command, receipt_handle=receipt)]),
            mutations,
            users,
            transport,
            notifications,
            max_seconds=2,
        )
        assert await worker.run() == 1

    assert await tables.get(replaced_event.event_id, replaced_key) is None
    master_text = (
        f"Отмена участия в игре «{replaced_event.name}» (14.11.26)\n\n"
        "Игрок: Иванов Иван\n"
        "Статус участия: подтверждено\n\n"
        "Причина: игрок подтвердил участие в другой игре в тот же день."
    )
    assert [(sent[0], sent[1], sent[3]) for sent in transport.sent if sent[1] != 1] == [
        (Platform.VK, 8, master_text),
        (Platform.TELEGRAM, 7, master_text),
    ]
