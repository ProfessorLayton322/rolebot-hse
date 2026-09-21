from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest

from larp_bot.adapters.memory import (
    MemoryCommandPublisher,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
    MemoryVkUserIdResolver,
    StaticAdminProvider,
)
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.application.conversation import ADMIN, ConversationEngine
from larp_bot.application.services import (
    EventAdministrationService,
    OperationNotAllowed,
    OrderedMutationService,
    RegistrationCatalog,
    RegistrationService,
)
from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    CharacterWishPayload,
    EmptyPayload,
    Event,
    EventStatus,
    InboundMessage,
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

SECRET = "linked-identity-secret"


def linked_profiles(*, telegram_privileged: bool = False) -> tuple[TelegramUser, VkUser]:
    telegram = TelegramUser(
        tg_id=11,
        telegram_handle="@same_person",
        vk_url="https://vk.com/person",
        linked_vk_id=22,
        is_gamemaster=telegram_privileged,
    )
    vk = VkUser(vk_id=22, telegram_handle="@same_person")
    return telegram, vk


def game(event_id: str, *, game_date: date | None = None) -> Event:
    return Event(
        event_id=event_id,
        name=event_id,
        game_date=game_date,
        disk_resource_path=f"disk:/larp-bot/events/{event_id}.xlsx",
        public_registration_url=f"https://disk.example/{event_id}",
        status=EventStatus.CONFIRMATION_OPEN,
    )


async def save_linked(users: MemoryUserRepository, *, telegram_privileged: bool = False) -> tuple[TelegramUser, VkUser]:
    telegram, vk = linked_profiles(telegram_privileged=telegram_privileged)
    await users.save(telegram)
    await users.save(vk)
    return telegram, vk


def registration(event: Event, identity: BotIdentity, *, status: AttendanceStatus) -> Registration:
    return Registration(
        event_id=event.event_id,
        participant_key=participant_key(SECRET, identity.platform, identity.platform_user_id, event.event_id),
        display_name="Один пользователь",
        wish_play="Без пожеланий",
        attendance_status=status,
    )


def engine_for(
    users: MemoryUserRepository,
    events: MemoryEventRepository,
    tables: MemoryRegistrationRepository,
    disk_store: MemoryDiskStore,
    *,
    admins: StaticAdminProvider | None = None,
    resolver: MemoryVkUserIdResolver | None = None,
) -> tuple[ConversationEngine, RegistrationService]:
    showcase = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(events, tables, showcase)
    registrations = RegistrationService(events, catalog, MemoryCommandPublisher(), SECRET, users)
    administration = EventAdministrationService(events, showcase, catalog, users, SECRET)
    return (
        ConversationEngine(
            users,
            events,
            registrations,
            administration,
            admins or StaticAdminProvider(),
            vk_user_ids=resolver,
        ),
        registrations,
    )


@pytest.mark.asyncio
async def test_links_require_unique_reciprocal_profiles_and_break_on_either_mismatch() -> None:
    users = MemoryUserRepository()
    telegram, vk = await save_linked(users)

    assert await users.find_linked(telegram) == vk
    assert await users.find_linked(vk) == telegram

    vk.telegram_handle = "@different_person"
    await users.save(vk)
    assert await users.find_linked(telegram) is None
    assert await users.find_linked(vk) is None

    vk.telegram_handle = telegram.telegram_handle
    await users.save(vk)
    duplicate = telegram.model_copy(update={"tg_id": 12})
    await users.save(duplicate)
    assert await users.find_linked(telegram) is None
    assert await users.find_linked(duplicate) is None
    assert await users.find_linked(vk) is None


@pytest.mark.asyncio
async def test_vanity_vk_profile_is_resolved_and_stored_without_merging_local_rows(
    disk_store: MemoryDiskStore,
) -> None:
    users = MemoryUserRepository()
    telegram = TelegramUser(tg_id=11, telegram_handle="@same_person", vk_url="https://vk.com/person")
    await users.save(telegram)
    engine, _ = engine_for(
        users,
        MemoryEventRepository(),
        MemoryRegistrationRepository(),
        disk_store,
        resolver=MemoryVkUserIdResolver({"https://vk.com/person": 22}),
    )

    await engine._refresh_telegram_link(telegram, force_vk_resolution=True)

    stored = await users.get(Platform.TELEGRAM, telegram.tg_id)
    assert isinstance(stored, TelegramUser)
    assert stored.linked_vk_id == 22
    assert stored.dialog_state == "IDLE"


@pytest.mark.asyncio
async def test_games_registrations_and_event_leadership_are_visible_from_both_interfaces(
    disk_store: MemoryDiskStore,
) -> None:
    users = MemoryUserRepository()
    await save_linked(users)
    telegram_identity = BotIdentity(platform=Platform.TELEGRAM, platform_user_id=11)
    vk_identity = BotIdentity(platform=Platform.VK, platform_user_id=22)
    tg_game = game("event-tg")
    vk_game = game("event-vk")
    led_game = game("event-led")
    events = MemoryEventRepository([tg_game, vk_game, led_game])
    await events.add_leader(led_game.event_id, telegram_identity)
    tables = MemoryRegistrationRepository(
        [
            registration(tg_game, telegram_identity, status=AttendanceStatus.WAITING),
            registration(vk_game, vk_identity, status=AttendanceStatus.CONFIRMED),
        ]
    )
    _, service = engine_for(users, events, tables, disk_store)

    for identity in (telegram_identity, vk_identity):
        registered = await service.registered_games_page(
            identity.platform,
            identity.platform_user_id,
            archived=False,
        )
        participating = await service.participating_games_page(identity.platform, identity.platform_user_id)
        assert {event.event_id for event, _ in registered.rows} == {tg_game.event_id, vk_game.event_id}
        assert {event.event_id for event in participating.events} == {
            tg_game.event_id,
            vk_game.event_id,
            led_game.event_id,
        }


@pytest.mark.asyncio
async def test_one_sided_link_does_not_share_games_or_privileges(disk_store: MemoryDiskStore) -> None:
    users = MemoryUserRepository()
    telegram, vk = linked_profiles(telegram_privileged=True)
    vk.telegram_handle = "@not_reciprocal"
    await users.save(telegram)
    await users.save(vk)
    event = game("event-one")
    identity = BotIdentity(platform=Platform.TELEGRAM, platform_user_id=telegram.tg_id)
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository([registration(event, identity, status=AttendanceStatus.WAITING)])
    engine, service = engine_for(users, events, tables, disk_store)

    page = await service.registered_games_page(Platform.VK, vk.vk_id, archived=False)
    assert page.rows == []
    assert not await engine._has_admin_access(vk)


@pytest.mark.asyncio
async def test_all_privilege_sources_cross_the_link_but_dialogue_state_stays_local(
    disk_store: MemoryDiskStore,
) -> None:
    users = MemoryUserRepository()
    telegram, vk = await save_linked(users, telegram_privileged=True)
    telegram.dialog_state = "PROFILE_FULL_NAME_CYRILLIC"
    telegram.dialog_context = {"local": "telegram"}
    vk.dialog_state = "CO_PLAYER_EDIT"
    vk.dialog_context = {"local": "vk"}
    await users.save(telegram)
    await users.save(vk)
    event = game("event-role")
    events = MemoryEventRepository([event])
    await events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=telegram.tg_id),
    )
    engine, _ = engine_for(
        users,
        events,
        MemoryRegistrationRepository(),
        disk_store,
        admins=StaticAdminProvider(tg_ids={telegram.tg_id}),
    )

    assert await engine._has_admin_access(vk)
    assert await engine._is_admin(vk)
    assert await engine._is_event_leader(vk, event.event_id)

    response = await engine.handle(
        InboundMessage(
            identity=BotIdentity(platform=Platform.VK, platform_user_id=vk.vk_id),
            update_id="vk-local-menu",
            text="/menu",
        )
    )
    assert ADMIN in {button.value for button in response.buttons}
    stored_telegram = await users.get(Platform.TELEGRAM, telegram.tg_id)
    stored_vk = await users.get(Platform.VK, vk.vk_id)
    assert stored_telegram is not None and stored_telegram.dialog_state == "PROFILE_FULL_NAME_CYRILLIC"
    assert stored_telegram.dialog_context == {"local": "telegram"}
    assert stored_vk is not None and stored_vk.dialog_state == "IDLE"
    assert stored_vk.dialog_context == {}


@pytest.mark.asyncio
async def test_linked_registration_commands_use_existing_row_and_one_confirmation_fifo_group(
    disk_store: MemoryDiskStore,
) -> None:
    users = MemoryUserRepository()
    telegram, vk = await save_linked(users)
    event = game("event-cmd")
    telegram_identity = BotIdentity(platform=Platform.TELEGRAM, platform_user_id=telegram.tg_id)
    existing = registration(event, telegram_identity, status=AttendanceStatus.WAITING)
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository([existing])
    publisher = MemoryCommandPublisher()
    showcase = YandexDiskShowcaseRepository(disk_store)
    service = RegistrationService(events, RegistrationCatalog(events, tables, showcase), publisher, SECRET, users)

    from_vk = await service.enqueue(
        operation=Operation.CONFIRM,
        event_id=event.event_id,
        platform=Platform.VK,
        user_id=vk.vk_id,
        payload=CharacterWishPayload(character_wish="Врач"),
        reply_context=ReplyContext(),
        idempotency_key="vk-confirm",
    )
    from_telegram = await service.enqueue(
        operation=Operation.CONFIRM,
        event_id=event.event_id,
        platform=Platform.TELEGRAM,
        user_id=telegram.tg_id,
        payload=CharacterWishPayload(character_wish="Врач"),
        reply_context=ReplyContext(),
        idempotency_key="tg-confirm",
    )

    assert from_vk.participant_key == existing.participant_key
    assert from_telegram.participant_key == existing.participant_key
    assert from_vk.queue_group_id == from_telegram.queue_group_id


@pytest.mark.asyncio
async def test_linked_duplicate_statuses_change_together_without_deleting_rows(
    disk_store: MemoryDiskStore,
) -> None:
    users = MemoryUserRepository()
    telegram, vk = await save_linked(users)
    day = date(2026, 11, 14)
    target = game("event-new", game_date=day)
    replaced = game("event-old", game_date=day)
    identities = (
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=telegram.tg_id),
        BotIdentity(platform=Platform.VK, platform_user_id=vk.vk_id),
    )
    target_rows = [registration(target, identity, status=AttendanceStatus.WAITING) for identity in identities]
    replaced_rows = [registration(replaced, identity, status=AttendanceStatus.CONFIRMED) for identity in identities]
    replaced_rows[1].created_at += timedelta(microseconds=1)
    events = MemoryEventRepository([target, replaced])
    tables = MemoryRegistrationRepository([*target_rows, *replaced_rows])
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(target.disk_resource_path, target_rows)
    await showcase.create_event_workbook(replaced.disk_resource_path, replaced_rows)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase), users, SECRET)
    confirm = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=target.event_id,
        operation=Operation.CONFIRM,
        platform=Platform.VK,
        platform_user_id=vk.vk_id,
        participant_key=target_rows[0].participant_key,
        payload=CharacterWishPayload(character_wish="Детектив", replaced_event_id=replaced.event_id),
    )

    await mutations.apply(confirm)

    assert len(await tables.list_for_event(target.event_id)) == 2
    assert len(await tables.list_for_event(replaced.event_id)) == 2
    assert {row.attendance_status for row in await tables.list_for_event(target.event_id)} == {
        AttendanceStatus.CONFIRMED
    }
    assert {row.attendance_status for row in await tables.list_for_event(replaced.event_id)} == {
        AttendanceStatus.CANCELLED
    }
    assert {row.character_wish for row in await tables.list_for_event(target.event_id)} == {"Детектив"}

    cancel = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=target.event_id,
        operation=Operation.CANCEL,
        platform=Platform.TELEGRAM,
        platform_user_id=telegram.tg_id,
        participant_key=target_rows[1].participant_key,
        payload=EmptyPayload(),
    )
    await mutations.apply(cancel)
    assert len(await tables.list_for_event(target.event_id)) == 2
    assert {row.attendance_status for row in await tables.list_for_event(target.event_id)} == {
        AttendanceStatus.CANCELLED
    }


@pytest.mark.asyncio
async def test_worker_rejects_duplicate_signup_from_other_linked_interface(
    disk_store: MemoryDiskStore,
) -> None:
    users = MemoryUserRepository()
    telegram, vk = await save_linked(users)
    telegram.full_name = "Иванов Иван"
    telegram.crossplay = False
    telegram.needs_pass = False
    vk.full_name = telegram.full_name
    vk.crossplay = False
    vk.needs_pass = False
    await users.save(telegram)
    await users.save(vk)
    event = game("event-dupe")
    telegram_identity = BotIdentity(platform=Platform.TELEGRAM, platform_user_id=telegram.tg_id)
    existing = registration(event, telegram_identity, status=AttendanceStatus.WAITING)
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository([existing])
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, [existing])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase), users, SECRET)
    duplicate = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=Operation.ENLIST,
        platform=Platform.VK,
        platform_user_id=vk.vk_id,
        participant_key=participant_key(SECRET, Platform.VK, vk.vk_id, event.event_id),
        payload={
            "display_name": vk.full_name,
            "wish_play": "Без пожеланий",
            "larp_experience": False,
            "crossplay": False,
            "vk_profile": f"https://vk.com/id{vk.vk_id}",
        },
    )

    with pytest.raises(OperationNotAllowed, match="уже записаны"):
        await mutations.apply(duplicate)
    assert await tables.list_for_event(event.event_id) == [existing]
