from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from larp_bot.adapters.memory import (
    MemoryCommandPublisher,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
    StaticAdminProvider,
)
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.application.conversation import (
    ADMIN,
    CHANGE_STATUS,
    CHARACTER,
    CONFIRM,
    ENLIST,
    KEEP,
    PROFILE,
    STATUS_CLOSED,
    STATUS_CONFIRMATION,
    STATUS_REGISTRATION,
    ConversationEngine,
)
from larp_bot.application.services import (
    EventAdministrationService,
    RegistrationCatalog,
    RegistrationService,
)
from larp_bot.domain.models import (
    BotIdentity,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    Platform,
    TelegramUser,
    VkUser,
)
from tests.conftest import MemoryDiskStore


def inbound(update: int, value: str, *, callback: bool = False, user_id: int = 1) -> InboundMessage:
    return InboundMessage(
        identity=BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id),
        update_id=str(update),
        text="" if callback else value,
        callback=value if callback else None,
        chat_id=user_id,
    )


async def engine_setup(
    store: MemoryDiskStore, event: Event, *, admin: bool = False
) -> tuple[
    ConversationEngine,
    MemoryUserRepository,
    MemoryCommandPublisher,
    MemoryRegistrationRepository,
]:
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
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(store)
    await showcase.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, showcase)
    publisher = MemoryCommandPublisher()
    registrations = RegistrationService(events, catalog, publisher, "participant-secret")
    conversation = ConversationEngine(
        users,
        events,
        registrations,
        EventAdministrationService(events, showcase),
        StaticAdminProvider(tg_ids={1} if admin else set()),
    )
    return conversation, users, publisher, tables


@pytest.mark.asyncio
async def test_enlist_never_asks_character_wishes(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event)
    responses = [await engine.handle(inbound(1, ENLIST))]
    responses.append(await engine.handle(inbound(2, f"select:enlist:{event.event_id}", callback=True)))
    responses.append(await engine.handle(inbound(3, "Алиса")))
    responses.append(await engine.handle(inbound(4, "enlist:confirm", callback=True)))
    prompts_before_enqueue = " ".join(response.text.casefold() for response in responses[:-1])
    assert "пожелания по персонажу" not in prompts_before_enqueue
    assert "не хотел" not in prompts_before_enqueue
    assert len(publisher.commands) == 1
    queued = publisher.commands[0]
    assert queued.operation is Operation.ENLIST
    assert isinstance(queued.payload, EnlistPayload)
    assert not hasattr(queued.payload, "character_wish")
    assert queued.payload.larp_experience is True
    assert queued.payload.crossplay is True
    assert responses[-1].deferred


@pytest.mark.asyncio
async def test_enlist_rejects_stale_game_button_and_offers_explicit_skip(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event)
    await engine.handle(inbound(1, ENLIST))
    prompt = await engine.handle(inbound(2, f"select:enlist:{event.event_id}", callback=True))
    assert "нажмите «пропустить»" in prompt.text.casefold()
    assert any(button.value == "enlist:wish-play:skip" for button in prompt.buttons)

    stale = await engine.handle(inbound(3, f"select:enlist:{event.event_id}", callback=True))
    assert "текущую клавиатуру" in stale.text
    assert not publisher.commands

    await engine.handle(inbound(4, "enlist:wish-play:skip", callback=True))
    await engine.handle(inbound(5, "enlist:confirm", callback=True))
    assert isinstance(publisher.commands[-1].payload, EnlistPayload)
    assert publisher.commands[-1].payload.wish_play == "Без пожеланий"


@pytest.mark.asyncio
async def test_duplicate_update_does_not_enqueue_twice(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event)
    await engine.handle(inbound(1, ENLIST))
    await engine.handle(inbound(2, f"select:enlist:{event.event_id}", callback=True))
    await engine.handle(inbound(3, "Алиса"))
    final = inbound(4, "enlist:confirm", callback=True)
    await engine.handle(final)
    duplicate = await engine.handle(final)
    assert duplicate.silent
    assert len(publisher.commands) == 1


@pytest.mark.asyncio
async def test_confirmation_is_first_character_wish_prompt(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, publisher, tables = await engine_setup(disk_store, event)
    key = engine.registrations.key(Platform.TELEGRAM, 1, event.event_id)
    await tables.enlist(
        event.event_id,
        operation_id="existing-enlist",
        participant_key=key,
        display_name="Иван Иванов",
        wish_play="Алиса",
    )
    listing = await engine.handle(inbound(1, CONFIRM))
    assert any(button.value == f"select:confirm:{event.event_id}" for button in listing.buttons)
    selection = await engine.handle(inbound(2, f"select:confirm:{event.event_id}", callback=True))
    assert "пожелания" in selection.text.casefold()
    await engine.handle(inbound(3, "Doctor"))
    assert publisher.commands[-1].operation is Operation.CONFIRM


@pytest.mark.asyncio
async def test_created_game_accepts_signup_but_does_not_offer_confirmation(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    event.status = EventStatus.CREATED
    engine, _, publisher, tables = await engine_setup(disk_store, event)

    enlist_games = await engine.handle(inbound(1, ENLIST))
    assert any(button.value == f"select:enlist:{event.event_id}" for button in enlist_games.buttons)
    key = engine.registrations.key(Platform.TELEGRAM, 1, event.event_id)
    await tables.enlist(
        event.event_id,
        operation_id="existing-enlist",
        participant_key=key,
        display_name="Иван Иванов",
        wish_play="Алиса",
    )

    confirmation_games = await engine.handle(inbound(2, CONFIRM))
    assert "ещё не открыто" in confirmation_games.text
    assert not any(button.value == f"select:confirm:{event.event_id}" for button in confirmation_games.buttons)
    assert not publisher.commands


@pytest.mark.asyncio
async def test_reconfirm_cancelled_registration_can_keep_old_character_wish(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, publisher, tables = await engine_setup(disk_store, event)
    key = engine.registrations.key(Platform.TELEGRAM, 1, event.event_id)
    await tables.enlist(
        event.event_id,
        operation_id="existing-enlist",
        participant_key=key,
        display_name="Иван",
        wish_play="A",
    )
    await tables.confirm(event.event_id, operation_id="confirm", participant_key=key, character_wish="Doctor")
    await tables.cancel(event.event_id, operation_id="cancel", participant_key=key)
    await engine.handle(inbound(1, CONFIRM))
    selected = await engine.handle(inbound(2, f"select:confirm:{event.event_id}", callback=True))
    assert any(button.value == KEEP for button in selected.buttons)
    await engine.handle(inbound(3, KEEP, callback=True))
    payload = publisher.commands[-1].payload
    assert hasattr(payload, "character_wish") and payload.character_wish == "Doctor"


@pytest.mark.asyncio
async def test_vk_uses_same_profile_and_registration_engine(disk_store: MemoryDiskStore, event: Event) -> None:
    users = MemoryUserRepository()
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, showcase)
    publisher = MemoryCommandPublisher()
    engine = ConversationEngine(
        users,
        events,
        RegistrationService(events, catalog, publisher, "secret"),
        EventAdministrationService(events, showcase),
        StaticAdminProvider(vk_ids={7}),
    )

    def vk(update: int, value: str, *, callback: bool = False) -> InboundMessage:
        return InboundMessage(
            identity=BotIdentity(platform=Platform.VK, platform_user_id=7),
            update_id=str(update),
            text="" if callback else value,
            callback=value if callback else None,
            peer_id=7,
        )

    for update, value in enumerate((PROFILE, "Иван Иванов", "Пропустить", "Да", "Нет", "Нет"), start=1):
        await engine.handle(vk(update, value))
    profile = await users.get(Platform.VK, 7)
    assert isinstance(profile, VkUser)
    assert profile.profile_complete and profile.telegram_handle is None

    await engine.handle(vk(10, ENLIST))
    await engine.handle(vk(11, f"select:enlist:{event.event_id}", callback=True))
    await engine.handle(vk(12, "Алиса"))
    await engine.handle(vk(13, "enlist:confirm", callback=True))
    enlist = publisher.commands[-1]
    assert enlist.operation is Operation.ENLIST
    assert isinstance(enlist.payload, EnlistPayload)
    assert enlist.participant_key is not None
    await tables.enlist(
        event.event_id,
        operation_id=enlist.operation_id,
        participant_key=enlist.participant_key,
        display_name=enlist.payload.display_name,
        wish_play=enlist.payload.wish_play,
        larp_experience=enlist.payload.larp_experience,
        crossplay=enlist.payload.crossplay,
    )
    await engine.handle(vk(14, CONFIRM))
    prompt = await engine.handle(vk(15, f"select:confirm:{event.event_id}", callback=True))
    assert "пожелания" in prompt.text.casefold()
    await engine.handle(vk(16, "Doctor"))
    confirmation = publisher.commands[-1]
    assert confirmation.operation is Operation.CONFIRM
    assert confirmation.participant_key is not None
    assert hasattr(confirmation.payload, "character_wish")
    await tables.confirm(
        event.event_id,
        operation_id=confirmation.operation_id,
        participant_key=confirmation.participant_key,
        character_wish=confirmation.payload.character_wish,
    )

    await engine.handle(vk(17, CHARACTER))
    edit_prompt = await engine.handle(vk(18, f"select:character:{event.event_id}", callback=True))
    assert "Отправьте новый вариант" in edit_prompt.text
    await engine.handle(vk(19, "Medic"))
    assert publisher.commands[-1].operation is Operation.UPDATE_CHARACTER_WISH

    menu = await engine.handle(vk(20, ADMIN))
    assert "Администрирование" in menu.text


@pytest.mark.asyncio
async def test_waiting_blank_character_menu_routes_to_confirmation(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, _, tables = await engine_setup(disk_store, event)
    key = engine.registrations.key(Platform.TELEGRAM, 1, event.event_id)
    await tables.enlist(
        event.event_id,
        operation_id="existing-enlist",
        participant_key=key,
        display_name="Иван",
        wish_play="A",
    )
    await engine.handle(inbound(1, CHARACTER))
    response = await engine.handle(inbound(2, f"select:character:{event.event_id}", callback=True))
    assert CONFIRM in response.text


@pytest.mark.asyncio
async def test_incomplete_profile_cannot_enlist(disk_store: MemoryDiskStore, event: Event) -> None:
    users = MemoryUserRepository()
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(events, tables, showcase)
    publisher = MemoryCommandPublisher()
    engine = ConversationEngine(
        users,
        events,
        RegistrationService(events, catalog, publisher, "secret"),
        EventAdministrationService(events, showcase),
        StaticAdminProvider(),
    )
    response = await engine.handle(inbound(1, ENLIST))
    assert "Сначала зарегистрируйте профиль" in response.text
    assert response.buttons[0].value == PROFILE


@pytest.mark.asyncio
async def test_admin_delete_requires_exact_case_but_trims_whitespace(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)
    menu = await engine.handle(inbound(1, ADMIN))
    assert "🗑 Удалить игру" in [button.value for button in menu.buttons]
    await engine.handle(inbound(2, "🗑 Удалить игру", callback=True))
    await engine.handle(inbound(3, f"select:admin-delete:{event.event_id}", callback=True))
    wrong = await engine.handle(inbound(4, event.name.casefold()))
    assert "не совпало" in wrong.text
    accepted = await engine.handle(inbound(5, f"  {event.name}  "))
    assert accepted.deferred
    assert publisher.commands[-1].operation is Operation.DELETE_EVENT


@pytest.mark.asyncio
async def test_admin_can_choose_any_of_three_explained_statuses(disk_store: MemoryDiskStore, event: Event) -> None:
    event.status = EventStatus.CREATED
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)

    menu = await engine.handle(inbound(1, ADMIN))
    assert CHANGE_STATUS in [button.value for button in menu.buttons]
    games = await engine.handle(inbound(2, CHANGE_STATUS, callback=True))
    assert any(button.value == f"select:admin-status:{event.event_id}" for button in games.buttons)
    status_prompt = await engine.handle(inbound(3, f"select:admin-status:{event.event_id}", callback=True))
    assert "Текущий Статус: Регистрация" in status_prompt.text
    assert "могут записываться, но ещё не могут подтверждать" in status_prompt.text
    assert "могут записываться и подтверждать" in status_prompt.text
    assert "не могут ни записываться, ни подтверждать" in status_prompt.text
    assert [button.label for button in status_prompt.buttons[:3]] == [
        STATUS_REGISTRATION,
        STATUS_CONFIRMATION,
        STATUS_CLOSED,
    ]

    expected_operations = [
        ("admin:status:registration", Operation.OPEN_REGISTRATION),
        ("admin:status:confirmation", Operation.OPEN_CONFIRMATION),
        ("admin:status:closed", Operation.CLOSE_EVENT),
    ]
    update = 4
    for callback, operation in expected_operations:
        if update > 4:
            await engine.handle(inbound(update, CHANGE_STATUS, callback=True))
            update += 1
            await engine.handle(inbound(update, f"select:admin-status:{event.event_id}", callback=True))
            update += 1
        queued = await engine.handle(inbound(update, callback, callback=True))
        update += 1
        assert queued.deferred
        assert publisher.commands[-1].operation is operation


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 10, 11, 20, 21])
async def test_admin_pagination_is_exactly_ten(count: int, disk_store: MemoryDiskStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        Event(
            event_id=f"event-{index:04d}",
            name=f"Game {index}",
            disk_resource_path=f"disk:/larp-bot/events/event-{index:04d}-game.xlsx",
            public_registration_url=f"https://disk.example/{index}",
            created_at=base + timedelta(seconds=index),
            updated_at=base + timedelta(seconds=index),
        )
        for index in range(count)
    ]
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Admin",
            vk_url="vk.com/admin",
            crossplay=True,
            larp_experience=True,
            needs_pass=False,
        )
    )
    event_repository = MemoryEventRepository(events)
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(event_repository, tables, showcase)
    engine = ConversationEngine(
        users,
        event_repository,
        RegistrationService(event_repository, catalog, MemoryCommandPublisher(), "secret"),
        EventAdministrationService(event_repository, showcase),
        StaticAdminProvider(tg_ids={1}),
    )
    page = await engine.handle(inbound(1, "📋 Список игр"))
    assert page.text.count("https://disk.example/") == min(count, 10)
    assert page.text.count("Статус: Регистрация") == min(count, 10)
    has_next = any(button.label == "➡️ Далее" for button in page.buttons)
    assert has_next is (count > 10)
