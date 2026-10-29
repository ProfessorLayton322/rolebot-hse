from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from larp_bot.adapters.memory import (
    MemoryCommandPublisher,
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryTelegramChatGateway,
    MemoryUserRepository,
    StaticAdminProvider,
)
from larp_bot.adapters.telegram import TelegramGroupUpdate, parse_telegram_group_update
from larp_bot.application.chat_management import TelegramChatManagementService, telegram_administrator_title
from larp_bot.application.services import (
    CANCELLED_CONFIRMED_PREFIX,
    CANCELLED_WAITING_PREFIX,
    ConfirmationNotificationService,
    RegistrationCatalog,
    RegistrationService,
)
from larp_bot.domain.models import (
    BotIdentity,
    EmptyPayload,
    Event,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    TelegramBotPermissionError,
    TelegramUser,
    VkUser,
)
from larp_bot.domain.security import participant_key


def make_event(index: int, *, archived: bool = False) -> Event:
    moment = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
    return Event(
        event_id=f"event-{index:04d}",
        name=f"Игра {index}",
        disk_resource_path=f"disk:/larp-bot/events/event-{index:04d}.xlsx",
        public_registration_url=f"https://disk.example/{index}",
        archived_at=moment if archived else None,
        created_at=moment,
        updated_at=moment,
    )


def chat_service(
    events: MemoryEventRepository,
    tables: MemoryRegistrationRepository | None = None,
    users: MemoryUserRepository | None = None,
    admins: StaticAdminProvider | None = None,
    gateway: MemoryTelegramChatGateway | None = None,
) -> tuple[TelegramChatManagementService, MemoryTelegramChatGateway, RegistrationService]:
    tables = tables or MemoryRegistrationRepository()
    users = users or MemoryUserRepository()
    catalog = RegistrationCatalog(events, tables, None)  # type: ignore[arg-type]
    registrations = RegistrationService(
        events,
        catalog,
        MemoryCommandPublisher(),
        "participant-secret",
        users,
    )
    gateway = gateway or MemoryTelegramChatGateway()
    return (
        TelegramChatManagementService(
            events,
            registrations,
            gateway,
            users,
            admins or StaticAdminProvider(),
        ),
        gateway,
        registrations,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Иванов Иван", "Иван Иванов"),
        ("Смирнова Анастасия", "Анастасия С."),
        ("Александровский Иван", "Иван А."),
        ("Оченьдлиннаяфамилия Иван", "Иван О."),
        ("  Иванов   Иван   Иванович ", "Иван Иванов"),
        ("Семёнова-Звенигородская Ева-Лотта", "Ева-Лотта С."),
        ("Александров Иван", "Иван Александров"),
        ("Александрова Иван", "Иван А."),
        ("Семёнова-Звенигородская Павел", "Павел С."),
        ("Иванова Александра-Мария", "Александра-М… И."),
        ("Иванов", "Иванов"),
        ("Оченьдлиннаяфамилия", "Оченьдлиннаяфами"),
        ("", "Игрок"),
        ("   ", "Игрок"),
    ],
)
def test_telegram_administrator_title_follows_name_fallbacks(name: str, expected: str) -> None:
    assert telegram_administrator_title(name) == expected
    assert len(expected) <= 16


def test_group_update_parser_detects_admin_grant_tie_and_join() -> None:
    admin_grant = parse_telegram_group_update(
        {
            "update_id": 1,
            "my_chat_member": {
                "chat": {"id": -1001, "type": "supergroup"},
                "old_chat_member": {"status": "member"},
                "new_chat_member": {"status": "administrator", "can_manage_chat": True},
            },
        }
    )
    tie = parse_telegram_group_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 10,
                "from": {"id": 42},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "/tie@rolebot_test_bot",
            },
        }
    )
    join = parse_telegram_group_update(
        {
            "update_id": 3,
            "chat_member": {
                "chat": {"id": -1001, "type": "supergroup"},
                "old_chat_member": {"status": "left", "user": {"id": 7}},
                "new_chat_member": {
                    "status": "member",
                    "user": {"id": 7, "username": "ivan_player", "is_bot": False},
                },
            },
        }
    )

    assert admin_grant is not None and admin_grant.action == "bot_admin_granted"
    assert tie is not None and tie.action == "tie_command" and tie.actor_user_id == 42
    assert join is not None and join.action == "member_joined"
    assert join.member_user_id == 7 and join.member_username == "ivan_player"


@pytest.mark.asyncio
async def test_admin_grant_shows_paginated_unarchived_games() -> None:
    events = MemoryEventRepository([*(make_event(index) for index in range(7)), make_event(99, archived=True)])
    service, gateway, _ = chat_service(events)

    await service.handle(TelegramGroupUpdate("1", "bot_admin_granted", -1001))

    assert len(gateway.sent) == 1
    buttons = gateway.sent[0][3]
    assert len([button for button in buttons if button.value.startswith("tie:e:")]) == 5
    assert buttons[-1].value == "tie:p:1"
    assert all(len(button.value.encode()) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_tie_requires_bot_privilege_and_explicit_chat_admin_right() -> None:
    event = make_event(1)
    events = MemoryEventRepository([event])
    service, gateway, _ = chat_service(events)
    command = TelegramGroupUpdate("1", "tie_command", -1001, actor_user_id=42)
    gateway.members[(-1001, 42)] = {
        "status": "administrator",
        "can_manage_chat": True,
        "custom_title": "Иванов Иван",
    }

    await service.handle(command)
    assert gateway.sent[-1][2] == "Недостаточно привилегий для вызова этой команды"
    assert gateway.sent[-1][3] == ()

    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=42))
    await service.handle(TelegramGroupUpdate("2", "tie_command", -1001, actor_user_id=42))
    assert gateway.sent[-1][2] == "Недостаточно прав."

    gateway.members[(-1001, 42)]["can_delete_messages"] = True
    await service.handle(TelegramGroupUpdate("3", "tie_command", -1001, actor_user_id=42))
    assert gateway.sent[-1][2].startswith("Выберите игру")


@pytest.mark.asyncio
async def test_moderator_cannot_invoke_tie_or_use_a_stale_event_button() -> None:
    event = make_event(1)
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42, is_gamemaster=True))
    service, gateway, _ = chat_service(
        events,
        users=users,
        admins=StaticAdminProvider(tg_gamemaster_ids={42}),
    )
    gateway.members[(-1001, 42)] = {"status": "creator"}

    await service.handle(TelegramGroupUpdate("1", "tie_command", -1001, actor_user_id=42))

    assert gateway.sent == [
        (-1001, "telegram-chat:1:privilege-denied", "Недостаточно привилегий для вызова этой команды", (), 1)
    ]

    await service.handle(
        TelegramGroupUpdate(
            "2",
            "tie_callback",
            -1001,
            actor_user_id=42,
            message_id=77,
            callback_query_id="callback-1",
            callback_data=TelegramChatManagementService._page_buttons([event], 0)[0].value,
        )
    )

    stored = await events.get(event.event_id)
    assert stored is not None and stored.telegram_chat_id is None
    assert gateway.export_count == 0
    assert gateway.answered[-1][2] == "Недостаточно привилегий для вызова этой команды"


@pytest.mark.asyncio
async def test_master_only_sees_led_games_while_bot_admin_sees_all_games() -> None:
    led_event = make_event(1)
    other_event = make_event(2)
    events = MemoryEventRepository([led_event, other_event])
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=42, telegram_handle="@master", linked_vk_id=84))
    await users.save(VkUser(vk_id=84, telegram_handle="@master"))
    await events.add_leader(
        led_event.event_id,
        BotIdentity(platform=Platform.VK, platform_user_id=84),
    )
    master_service, master_gateway, _ = chat_service(events, users=users)
    master_gateway.members[(-1001, 42)] = {"status": "creator"}

    await master_service.handle(TelegramGroupUpdate("1", "tie_command", -1001, actor_user_id=42))

    assert [button.label for button in master_gateway.sent[-1][3]] == [led_event.name]

    await master_service.handle(
        TelegramGroupUpdate(
            "2",
            "tie_callback",
            -1001,
            actor_user_id=42,
            message_id=77,
            callback_query_id="callback-other-event",
            callback_data=TelegramChatManagementService._page_buttons([other_event], 0)[0].value,
        )
    )

    stored_other = await events.get(other_event.event_id)
    assert stored_other is not None and stored_other.telegram_chat_id is None
    assert master_gateway.answered[-1][2] == "Недостаточно привилегий для вызова этой команды"

    admin_service, admin_gateway, _ = chat_service(events, admins=StaticAdminProvider(tg_ids={99}))
    admin_gateway.members[(-1001, 99)] = {"status": "creator"}

    await admin_service.handle(TelegramGroupUpdate("3", "tie_command", -1001, actor_user_id=99))

    assert [button.label for button in admin_gateway.sent[-1][3]] == [led_event.name, other_event.name]


@pytest.mark.asyncio
async def test_tie_persists_and_reuses_exactly_one_primary_invite_link() -> None:
    event = make_event(1)
    events = MemoryEventRepository([event])
    service, gateway, registrations = chat_service(events)
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=42))
    gateway.members[(-1001, 42)] = {"status": "creator"}
    await service.handle(TelegramGroupUpdate("1", "tie_command", -1001, actor_user_id=42))
    event_button = next(button for button in gateway.sent[-1][3] if button.value.startswith("tie:e:"))

    await service.handle(
        TelegramGroupUpdate(
            "2",
            "tie_callback",
            -1001,
            actor_user_id=42,
            message_id=77,
            callback_query_id="callback-1",
            callback_data=event_button.value,
        )
    )

    stored = await events.get(event.event_id)
    assert stored is not None
    assert stored.telegram_chat_id == -1001
    assert stored.telegram_chat_invite_link == "https://t.me/+chat1001"
    assert gateway.export_count == 1
    assert gateway.deleted[-1][:2] == (-1001, 77)
    assert gateway.sent[-1][2] == (
        f"Чат был привязан к игре {event.name}.\n\n"
        "Ссылка сохранена и поставлена в очередь на отправку подтверждённым игрокам основного состава."
    )
    publisher = registrations.publisher
    assert isinstance(publisher, MemoryCommandPublisher)
    assert len(publisher.commands) == 1
    invitation = publisher.commands[0]
    assert invitation.operation is Operation.SEND_CONFIRMED_NOTIFICATION
    assert invitation.event_id == event.event_id
    assert invitation.platform is Platform.TELEGRAM
    assert invitation.platform_user_id == 42
    assert isinstance(invitation.payload, NotificationPayload)
    assert invitation.payload.text == stored.telegram_chat_invite_link
    assert invitation.participant_key is None

    await service.handle(TelegramGroupUpdate("3", "tie_command", -1001, actor_user_id=42))
    repeated_button = next(button for button in gateway.sent[-1][3] if button.value.startswith("tie:e:"))
    await service.handle(
        TelegramGroupUpdate(
            "4",
            "tie_callback",
            -1001,
            actor_user_id=42,
            message_id=78,
            callback_query_id="callback-2",
            callback_data=repeated_button.value,
        )
    )
    assert gateway.export_count == 1
    repeated = await events.get(event.event_id)
    assert repeated is not None
    assert repeated.telegram_chat_invite_link == stored.telegram_chat_invite_link
    assert len(publisher.commands) == 1


@pytest.mark.asyncio
async def test_tie_handles_missing_bot_admin_permission_and_resumes_after_grant() -> None:
    class PermissionAwareGateway(MemoryTelegramChatGateway):
        invite_permission = False

        async def ensure_primary_invite_link(self, *, chat_id: int, request_id: str) -> str:
            if not self.invite_permission:
                raise TelegramBotPermissionError("not enough rights to manage chat invite link")
            return await super().ensure_primary_invite_link(chat_id=chat_id, request_id=request_id)

    event = make_event(1)
    events = MemoryEventRepository([event])
    gateway = PermissionAwareGateway()
    service, _, registrations = chat_service(events, gateway=gateway)
    await events.add_leader(event.event_id, BotIdentity(platform=Platform.TELEGRAM, platform_user_id=42))
    gateway.members[(-1001, 42)] = {"status": "creator"}
    await service.handle(TelegramGroupUpdate("1", "tie_command", -1001, actor_user_id=42))
    event_button = next(button for button in gateway.sent[-1][3] if button.value.startswith("tie:e:"))

    await service.handle(
        TelegramGroupUpdate(
            "2",
            "tie_callback",
            -1001,
            actor_user_id=42,
            message_id=77,
            callback_query_id="callback-1",
            callback_data=event_button.value,
        )
    )

    stored = await events.get(event.event_id)
    assert stored is not None
    assert stored.telegram_chat_id == -1001
    assert stored.telegram_chat_invite_link is None
    assert gateway.answered[-1][2] == (
        "Назначьте бота администратором чата с правом приглашать пользователей и повторите /tie."
    )
    assert gateway.deleted == []
    publisher = registrations.publisher
    assert isinstance(publisher, MemoryCommandPublisher)
    assert publisher.commands == []

    gateway.invite_permission = True
    await service.handle(
        TelegramGroupUpdate(
            "3",
            "tie_callback",
            -1001,
            actor_user_id=42,
            message_id=77,
            callback_query_id="callback-2",
            callback_data=event_button.value,
        )
    )

    recovered = await events.get(event.event_id)
    assert recovered is not None
    assert recovered.telegram_chat_invite_link == "https://t.me/+chat1001"
    assert len(publisher.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("display_name", "expected_title"),
    [
        ("Иванов Иван", "Иван Иванов"),
        ("Смирнова Анастасия", "Анастасия С."),
        ("Иванова Александра-Мария", "Александра-М… И."),
    ],
)
async def test_joined_registration_is_promoted_by_stored_telegram_handle(
    display_name: str, expected_title: str
) -> None:
    event = make_event(1)
    event.telegram_chat_id = -1001
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    await tables.enlist(
        event.event_id,
        operation_id="enlist",
        participant_key="v" * 43,
        display_name=display_name,
        wish_play="A",
        telegram_profile="@ivan_player",
    )
    service, gateway, _ = chat_service(events, tables)

    await service.handle(
        TelegramGroupUpdate(
            "1",
            "member_joined",
            -1001,
            member_user_id=77,
            member_username="ivan_player",
        )
    )

    assert gateway.promoted[-1][:2] == (-1001, 77)
    assert gateway.titles[-1][:3] == (-1001, 77, expected_title)


@pytest.mark.asyncio
async def test_cancelled_registration_is_not_promoted_on_join() -> None:
    event = make_event(1)
    event.telegram_chat_id = -1001
    events = MemoryEventRepository([event])
    key = participant_key("participant-secret", Platform.TELEGRAM, 77, event.event_id)
    tables = MemoryRegistrationRepository()
    await tables.enlist(
        event.event_id,
        operation_id="enlist",
        participant_key=key,
        display_name="Иванов Иван",
        wish_play="A",
    )
    await tables.cancel(event.event_id, operation_id="cancel", participant_key=key)
    service, gateway, _ = chat_service(events, tables)

    await service.handle(TelegramGroupUpdate("1", "member_joined", -1001, member_user_id=77))

    assert gateway.promoted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_prefix", "expected_kicks"),
    [(CANCELLED_CONFIRMED_PREFIX, 1), (CANCELLED_WAITING_PREFIX, 0)],
)
async def test_only_confirmed_cancellation_kicks_present_telegram_member(
    snapshot_prefix: str,
    expected_kicks: int,
) -> None:
    event = make_event(1)
    event.telegram_chat_id = -1001
    key = participant_key("participant-secret", Platform.TELEGRAM, 77, event.event_id)
    operation_id = "o" * 32
    event.last_participant_removal_operation_id = operation_id
    event.participant_removal_keys = [f"{snapshot_prefix}{key}"]
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=77))
    tables = MemoryRegistrationRepository()
    catalog = RegistrationCatalog(events, tables, None)  # type: ignore[arg-type]
    gateway = MemoryTelegramChatGateway()
    gateway.members[(-1001, 77)] = {"status": "administrator", "can_be_edited": True}
    notifications = ConfirmationNotificationService(
        events,
        catalog,
        users,
        MemoryDeferredTransport(),
        "participant-secret",
        telegram_chat=gateway,
    )
    command = OrderedRegistrationCommand(
        operation_id=operation_id,
        event_id=event.event_id,
        operation=Operation.CANCEL,
        platform=Platform.TELEGRAM,
        platform_user_id=77,
        participant_key=key,
        payload=EmptyPayload(),
    )

    await notifications.kick_cancelled_participant(command)

    assert len(gateway.kicked) == expected_kicks


@pytest.mark.asyncio
async def test_vk_cancellation_uses_telegram_member_learned_from_game_registration() -> None:
    event = make_event(1)
    event.telegram_chat_id = -1001
    operation_id = "o" * 32
    vk_key = participant_key("participant-secret", Platform.VK, 88, event.event_id)
    event.last_participant_removal_operation_id = operation_id
    event.participant_removal_keys = [f"{CANCELLED_CONFIRMED_PREFIX}{vk_key}"]
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(VkUser(vk_id=88, telegram_handle="@ivan_player"))
    await users.save(TelegramUser(tg_id=77, telegram_handle="@ivan_player"))
    tables = MemoryRegistrationRepository()
    await tables.enlist(
        event.event_id,
        operation_id="enlist",
        participant_key=vk_key,
        display_name="Иванов Иван",
        wish_play="A",
        telegram_profile="@ivan_player",
    )
    catalog = RegistrationCatalog(events, tables, None)  # type: ignore[arg-type]
    gateway = MemoryTelegramChatGateway()
    gateway.members[(-1001, 77)] = {"status": "administrator", "can_be_edited": True}
    notifications = ConfirmationNotificationService(
        events,
        catalog,
        users,
        MemoryDeferredTransport(),
        "participant-secret",
        telegram_chat=gateway,
    )
    command = OrderedRegistrationCommand(
        operation_id=operation_id,
        event_id=event.event_id,
        operation=Operation.CANCEL,
        platform=Platform.VK,
        platform_user_id=88,
        participant_key=vk_key,
        payload=EmptyPayload(),
    )

    await notifications.kick_cancelled_participant(command)

    assert gateway.kicked[-1][:2] == (-1001, 77)
