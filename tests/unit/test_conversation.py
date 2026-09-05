from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from larp_bot.adapters.memory import (
    MemoryCommandPublisher,
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
    MemoryVkUserIdResolver,
    StaticAdminProvider,
)
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.application.conversation import (
    ADMIN,
    ADMIN_MENU,
    ARCHIVE_GAME,
    CANCEL,
    CHANGE_STATUS,
    CHARACTER,
    CONFIRM,
    CREATE_PASS_TABLE,
    ENLIST,
    FREE_TEXT_DIALOG_STATES,
    GAMEMASTER_NOTIFICATION,
    GRANT_GAMEMASTER,
    KEEP,
    LEGACY_DELETE_GAME,
    LIST_PASS_TABLES,
    MAIN_MENU,
    MASTER_TABLE_DISCLAIMER,
    PROFILE,
    SEND_CONFIRMATION_REMINDER,
    SEND_CONFIRMED_NOTIFICATION,
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
    Button,
    ConfirmationDeadlinePayload,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    NotificationPayload,
    Operation,
    Platform,
    TelegramUser,
    VkUser,
)
from tests.conftest import MemoryDiskStore


def inbound(
    update: int,
    value: str,
    *,
    callback: bool = False,
    user_id: int = 1,
    telegram_username: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        identity=BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id),
        update_id=str(update),
        text="" if callback else value,
        callback=value if callback else None,
        chat_id=user_id,
        telegram_username=telegram_username,
    )


class FailOnceTransport(MemoryDeferredTransport):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("temporary transport failure")
        await super().send(
            platform=platform,
            user_id=user_id,
            request_id=request_id,
            text=text,
            buttons=buttons,
        )


async def engine_setup(
    store: MemoryDiskStore,
    event: Event,
    *additional_events: Event,
    admin: bool = False,
    gamemaster: bool = False,
    vk_profile_ids: dict[str, int] | None = None,
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
    events = MemoryEventRepository([event, *additional_events])
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(store)
    await showcase.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, showcase)
    publisher = MemoryCommandPublisher()
    registrations = RegistrationService(events, catalog, publisher, "participant-secret")
    transport = MemoryDeferredTransport()
    conversation = ConversationEngine(
        users,
        events,
        registrations,
        EventAdministrationService(events, showcase, catalog, users, "participant-secret"),
        StaticAdminProvider(
            tg_ids={1} if admin else set(),
            tg_gamemaster_ids={1} if gamemaster else set(),
        ),
        transport=transport,
        vk_user_ids=MemoryVkUserIdResolver(vk_profile_ids),
    )
    return conversation, users, publisher, tables


@pytest.mark.asyncio
async def test_enlist_finishes_without_confirmation_or_character_wishes(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event)
    responses = [await engine.handle(inbound(1, ENLIST))]
    responses.append(await engine.handle(inbound(2, f"select:enlist:{event.event_id}", callback=True)))
    responses.append(await engine.handle(inbound(3, "Алиса", telegram_username="ivan_player")))
    prompts = " ".join(response.text.casefold() for response in responses)
    assert "пожелания по персонажу" not in prompts
    assert "подтвердить запись" not in prompts
    assert not any(button.value == "enlist:confirm" for response in responses for button in response.buttons)
    assert len(publisher.commands) == 1
    queued = publisher.commands[0]
    assert queued.operation is Operation.ENLIST
    assert isinstance(queued.payload, EnlistPayload)
    assert not hasattr(queued.payload, "character_wish")
    assert queued.payload.larp_experience is True
    assert queued.payload.crossplay is True
    assert queued.payload.vk_profile == "https://vk.com/id1"
    assert queued.payload.telegram_profile == "https://t.me/ivan_player"
    assert [(button.label, button.value) for button in queued.reply_context.buttons] == [(MAIN_MENU, MAIN_MENU)]
    assert [(button.label, button.value) for button in responses[-1].buttons] == [(MAIN_MENU, MAIN_MENU)]
    assert responses[-1].deferred


@pytest.mark.asyncio
async def test_cancel_confirmation_warns_about_losing_queue_position(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, _, tables = await engine_setup(disk_store, event)
    key = engine.registrations.key(Platform.TELEGRAM, 1, event.event_id)
    await tables.enlist(
        event.event_id,
        operation_id="existing-enlist",
        participant_key=key,
        display_name="Иван Иванов",
        wish_play="Алиса",
    )

    games = await engine.handle(inbound(1, CANCEL))
    assert any(button.value == f"select:cancel:{event.event_id}" for button in games.buttons)
    response = await engine.handle(inbound(2, f"select:cancel:{event.event_id}", callback=True))

    assert response.text == (
        f"Вы уверены, что хотите отменить участие в игре «{event.name}»? "
        "Если вы отмените участие, то сможете записаться снова, но попадёте "
        "в конец очереди подтверждения"
    )


@pytest.mark.asyncio
async def test_only_idle_buttonless_user_responses_get_main_menu_navigation(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event)

    prompt = await engine.handle(inbound(1, PROFILE, user_id=2))
    terminal = await engine.handle(inbound(2, "select:character:missing-game", callback=True))

    assert all(button.value != MAIN_MENU for button in prompt.buttons)
    assert [(button.label, button.value) for button in terminal.buttons] == [(MAIN_MENU, MAIN_MENU)]


@pytest.mark.asyncio
async def test_terminal_navigation_buttons_leave_active_text_dialogs(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event, admin=True)
    await engine.handle(inbound(1, PROFILE, user_id=2))

    main = await engine.handle(inbound(2, MAIN_MENU, callback=True, user_id=2))
    user = await users.get(Platform.TELEGRAM, 2)

    assert PROFILE in [button.value for button in main.buttons]
    assert user is not None and user.dialog_state == "IDLE"

    await engine.handle(inbound(3, "➕ Создать игру", callback=True))

    administration = await engine.handle(inbound(4, ADMIN_MENU, callback=True))
    admin = await users.get(Platform.TELEGRAM, 1)

    assert administration.text == "🛠 Администрирование"
    assert admin is not None and admin.dialog_state == "IDLE"


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

    response = await engine.handle(inbound(4, "enlist:wish-play:skip", callback=True))
    assert response.deferred
    assert isinstance(publisher.commands[-1].payload, EnlistPayload)
    assert publisher.commands[-1].payload.wish_play == "Без пожеланий"


@pytest.mark.asyncio
async def test_duplicate_update_does_not_enqueue_twice(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event)
    await engine.handle(inbound(1, ENLIST))
    await engine.handle(inbound(2, f"select:enlist:{event.event_id}", callback=True))
    final = inbound(3, "Алиса")
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
    stale = await engine.handle(inbound(3, f"select:confirm:{event.event_id}", callback=True))
    assert "кнопка устарела" in stale.text.casefold()
    assert stale.buttons == selection.buttons
    assert not publisher.commands
    await engine.handle(inbound(4, "Doctor"))
    assert publisher.commands[-1].operation is Operation.CONFIRM


@pytest.mark.asyncio
async def test_every_free_text_state_rejects_a_button_absent_from_the_last_bot_message(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, publisher, _ = await engine_setup(disk_store, event, admin=True)
    current_button = Button(label="Текущая кнопка", value="current:button")

    for update, state in enumerate(sorted(FREE_TEXT_DIALOG_STATES), start=1):
        user = await users.get(Platform.TELEGRAM, 1)
        assert user is not None
        user.dialog_state = state
        user.dialog_context = {"sentinel": state}
        user.last_bot_buttons = [current_button]
        await users.save(user)

        response = await engine.handle(inbound(update, "stale:button", callback=True))
        saved = await users.get(Platform.TELEGRAM, 1)

        assert "кнопка устарела" in response.text.casefold()
        assert response.buttons == [current_button]
        assert saved is not None and saved.dialog_state == state
        assert saved.dialog_context == {"sentinel": state}

    assert not publisher.commands


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
        EventAdministrationService(events, showcase, catalog, users, "secret"),
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

    for update, value in enumerate((PROFILE, "Иванов", "Иван", "Пропустить", "Да", "Нет", "Нет"), start=1):
        await engine.handle(vk(update, value))
    profile = await users.get(Platform.VK, 7)
    assert isinstance(profile, VkUser)
    assert profile.profile_complete and profile.telegram_handle is None
    assert profile.full_name == "Иванов Иван"

    await engine.handle(vk(10, ENLIST))
    await engine.handle(vk(11, f"select:enlist:{event.event_id}", callback=True))
    await engine.handle(vk(12, "Алиса"))
    enlist = publisher.commands[-1]
    assert enlist.operation is Operation.ENLIST
    assert isinstance(enlist.payload, EnlistPayload)
    assert enlist.participant_key is not None
    assert enlist.payload.vk_profile == "https://vk.com/id7"
    assert enlist.payload.telegram_profile is None
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
    stale = await engine.handle(vk(16, f"select:confirm:{event.event_id}", callback=True))
    assert "кнопка устарела" in stale.text.casefold()
    assert stale.buttons == prompt.buttons
    assert not publisher.commands[1:]
    await engine.handle(vk(17, "Doctor"))
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

    await engine.handle(vk(18, CHARACTER))
    edit_prompt = await engine.handle(vk(19, f"select:character:{event.event_id}", callback=True))
    assert "Отправьте новый вариант" in edit_prompt.text
    await engine.handle(vk(20, "Medic"))
    assert publisher.commands[-1].operation is Operation.UPDATE_CHARACTER_WISH

    menu = await engine.handle(vk(21, ADMIN))
    assert "Администрирование" in menu.text


@pytest.mark.asyncio
async def test_telegram_identity_handle_is_refreshed_from_each_update(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event)

    await engine.handle(inbound(1, "/start", user_id=2, telegram_username="Current_Name"))

    user = await users.get(Platform.TELEGRAM, 2)
    assert isinstance(user, TelegramUser)
    assert user.telegram_handle == "@current_name"


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
        EventAdministrationService(events, showcase, catalog, users, "secret"),
        StaticAdminProvider(),
    )
    response = await engine.handle(inbound(1, ENLIST))
    assert "Сначала зарегистрируйте профиль" in response.text
    assert response.buttons[0].value == PROFILE

    forged = await engine.handle(inbound(2, f"select:enlist:{event.event_id}", callback=True))
    assert "полностью заполните профиль" in forged.text
    assert not publisher.commands


@pytest.mark.asyncio
async def test_profile_validates_email_before_advancing_to_next_question(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event)
    user_id = 2
    answers = (
        PROFILE,
        "Иванов",
        "Иван",
        "https://vk.com/id2",
        "Да",
        "Нет",
        "Да",
        "Иванович",
        "Нет",
        "+7 999 123-45-67",
    )
    for update, answer in enumerate(answers, start=1):
        await engine.handle(inbound(update, answer, user_id=user_id))

    invalid = await engine.handle(inbound(11, "not-an-email", user_id=user_id))

    assert "Некорректный email" in invalid.text
    pending = await users.get(Platform.TELEGRAM, user_id)
    assert pending is not None
    assert pending.dialog_state == "PROFILE_PASS_EMAIL_ADDRESS"
    assert "email" not in pending.dialog_context

    saved = await engine.handle(inbound(12, "player@example.com", user_id=user_id))
    profile = await users.get(Platform.TELEGRAM, user_id)
    assert "Профиль сохранён" in saved.text
    assert profile is not None and profile.pass_details is not None
    assert profile.pass_details.email == "player@example.com"
    assert profile.pass_details.mobile_phone == "+7 999 123-45-67"
    assert profile.pass_details.foreigner is False
    assert profile.pass_details.surname_latin is None
    assert profile.full_name == "Иванов Иван"


@pytest.mark.asyncio
async def test_every_profile_collects_cyrillic_surname_and_name_separately(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event)

    surname_prompt = await engine.handle(inbound(1, PROFILE, user_id=2))
    invalid_surname = await engine.handle(inbound(2, "Smith", user_id=2))
    name_prompt = await engine.handle(inbound(3, "Смирнов", user_id=2))
    invalid_name = await engine.handle(inbound(4, "John", user_id=2))
    contact_prompt = await engine.handle(inbound(5, "Иван", user_id=2))
    for update, answer in enumerate(("https://vk.com/id2", "Нет", "Нет", "Нет"), start=6):
        saved = await engine.handle(inbound(update, answer, user_id=2))

    profile = await users.get(Platform.TELEGRAM, 2)
    assert "фамилию кириллицей" in surname_prompt.text
    assert "только кириллицу" in invalid_surname.text
    assert "имя кириллицей" in name_prompt.text
    assert "только кириллицу" in invalid_name.text
    assert "страницу VK" in contact_prompt.text
    assert "Профиль сохранён" in saved.text
    assert profile is not None and profile.full_name == "Смирнов Иван"
    assert profile.needs_pass is False and profile.pass_details is None


@pytest.mark.asyncio
async def test_foreign_profile_collects_separate_pass_fields(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event)
    answers = (
        PROFILE,
        "Ли",
        "Анна",
        "https://vk.com/anna-li",
        "Нет",
        "Да",
        "Да",
        "-",
        "Да",
        "Li",
        "Anna",
        "-",
        "+44 7700 900123",
        "anna@example.com",
    )
    response = None
    for update, answer in enumerate(answers, start=1):
        response = await engine.handle(inbound(update, answer, user_id=2))

    profile = await users.get(Platform.TELEGRAM, 2)
    assert response is not None and "Профиль сохранён" in response.text
    assert profile is not None and profile.profile_complete and profile.pass_details is not None
    assert profile.pass_details.model_dump() == {
        "surname_cyrillic": "Ли",
        "name_cyrillic": "Анна",
        "patronym_cyrillic": "-",
        "foreigner": True,
        "surname_latin": "Li",
        "name_latin": "Anna",
        "patronym_latin": "-",
        "mobile_phone": "+44 7700 900123",
        "email": "anna@example.com",
    }


@pytest.mark.asyncio
async def test_admin_archive_requires_exact_case_but_trims_whitespace(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)
    menu = await engine.handle(inbound(1, ADMIN))
    assert ARCHIVE_GAME in [button.value for button in menu.buttons]
    await engine.handle(inbound(2, ARCHIVE_GAME, callback=True))
    await engine.handle(inbound(3, f"select:admin-archive:{event.event_id}", callback=True))
    wrong = await engine.handle(inbound(4, event.name.casefold()))
    assert "не совпало" in wrong.text
    accepted = await engine.handle(inbound(5, f"  {event.name}  "))
    assert accepted.deferred
    assert [(button.label, button.value) for button in accepted.buttons] == [(ADMIN_MENU, ADMIN_MENU)]
    assert [(button.label, button.value) for button in publisher.commands[-1].reply_context.buttons] == [
        (ADMIN_MENU, ADMIN_MENU)
    ]
    assert publisher.commands[-1].operation is Operation.CLOSE_EVENT


@pytest.mark.asyncio
async def test_gamemaster_has_admin_interface_except_archive(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event, gamemaster=True)

    main = await engine.handle(inbound(1, "/start"))
    assert ADMIN in [button.value for button in main.buttons]

    menu = await engine.handle(inbound(2, ADMIN))
    assert [button.value for button in menu.buttons] == [
        "➕ Создать игру",
        CHANGE_STATUS,
        SEND_CONFIRMATION_REMINDER,
        SEND_CONFIRMED_NOTIFICATION,
        CREATE_PASS_TABLE,
        LIST_PASS_TABLES,
        "📋 Список игр",
        "⬅️ Назад",
    ]

    status_games = await engine.handle(inbound(3, CHANGE_STATUS))
    assert any(button.value == f"select:admin-status:{event.event_id}" for button in status_games.buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["@Target_Player", "https://t.me/Target_Player"])
async def test_admin_grants_telegram_gamemaster_by_profile_and_notifies_them(
    profile: str, disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event, admin=True)
    await users.save(
        TelegramUser(
            tg_id=2,
            telegram_handle="@target_player",
            full_name="Петрова Анна",
            vk_url="https://vk.com/anna",
            crossplay=True,
            larp_experience=False,
            needs_pass=False,
        )
    )

    menu = await engine.handle(inbound(1, ADMIN))
    assert GRANT_GAMEMASTER in [button.value for button in menu.buttons]
    choose_platform = await engine.handle(inbound(2, GRANT_GAMEMASTER, callback=True))
    assert [button.label for button in choose_platform.buttons[:2]] == ["Telegram", "VK"]
    prompt = await engine.handle(inbound(3, "admin:gamemaster:telegram", callback=True))
    assert "ссылку на профиль Telegram или @username" in prompt.text
    granted = await engine.handle(inbound(4, profile))

    target = await users.get(Platform.TELEGRAM, 2)
    assert target is not None and target.is_gamemaster
    assert target.gamemaster_grant_operation_id == "gamemaster-grant:telegram:2:4"
    assert isinstance(engine.transport, MemoryDeferredTransport)
    assert engine.transport.sent == [
        (Platform.TELEGRAM, 2, "gamemaster-grant:telegram:2:4", GAMEMASTER_NOTIFICATION, ()),
    ]
    assert granted.text == "✅ Пользователь назначен гейммастером в Telegram."

    target_main = await engine.handle(inbound(5, "/start", user_id=2, telegram_username="target_player"))
    assert ADMIN in [button.value for button in target_main.buttons]


@pytest.mark.asyncio
async def test_admin_grants_vk_gamemaster_from_vanity_link(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, users, _, _ = await engine_setup(
        disk_store,
        event,
        admin=True,
        vk_profile_ids={"https://vk.com/game_master": 22},
    )
    await users.save(
        VkUser(
            vk_id=22,
            full_name="Петров Пётр",
            crossplay=False,
            larp_experience=True,
            needs_pass=False,
        )
    )

    await engine.handle(inbound(1, GRANT_GAMEMASTER))
    await engine.handle(inbound(2, "admin:gamemaster:vk", callback=True))
    granted = await engine.handle(inbound(3, "vk.ru/game_master"))

    target = await users.get(Platform.VK, 22)
    assert target is not None and target.is_gamemaster
    assert isinstance(engine.transport, MemoryDeferredTransport)
    assert engine.transport.sent == [
        (Platform.VK, 22, "gamemaster-grant:vk:22:3", GAMEMASTER_NOTIFICATION, ()),
    ]
    assert granted.text == "✅ Пользователь назначен гейммастером в VK."


@pytest.mark.asyncio
async def test_admin_rejects_incomplete_gamemaster_profile(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event, admin=True)
    await users.save(TelegramUser(tg_id=2, telegram_handle="@target_player"))

    await engine.handle(inbound(1, GRANT_GAMEMASTER))
    await engine.handle(inbound(2, "admin:gamemaster:telegram", callback=True))
    rejected = await engine.handle(inbound(3, "@target_player"))

    target = await users.get(Platform.TELEGRAM, 2)
    assert target is not None and not target.is_gamemaster
    assert "заполнен не полностью" in rejected.text
    assert isinstance(engine.transport, MemoryDeferredTransport)
    assert engine.transport.sent == []


@pytest.mark.asyncio
async def test_gamemaster_notification_retries_after_role_was_persisted(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, _, _ = await engine_setup(disk_store, event, admin=True)
    engine.transport = FailOnceTransport()
    await users.save(
        TelegramUser(
            tg_id=2,
            telegram_handle="@target_player",
            full_name="Петрова Анна",
            vk_url="https://vk.com/anna",
            crossplay=True,
            larp_experience=False,
            needs_pass=False,
        )
    )
    await engine.handle(inbound(1, GRANT_GAMEMASTER))
    await engine.handle(inbound(2, "admin:gamemaster:telegram", callback=True))
    grant_message = inbound(3, "@target_player")

    with pytest.raises(RuntimeError, match="temporary transport failure"):
        await engine.handle(grant_message)
    persisted = await users.get(Platform.TELEGRAM, 2)
    assert persisted is not None and persisted.is_gamemaster
    assert persisted.last_delivery_operation_id is None

    response = await engine.handle(grant_message)

    assert response.text == "✅ Пользователь назначен гейммастером в Telegram."
    assert isinstance(engine.transport, FailOnceTransport)
    assert len(engine.transport.sent) == 1
    assert engine.transport.sent[0][3] == GAMEMASTER_NOTIFICATION


@pytest.mark.asyncio
async def test_gamemaster_cannot_grant_gamemaster_role(disk_store: MemoryDiskStore, event: Event) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event, gamemaster=True)

    response = await engine.handle(inbound(1, GRANT_GAMEMASTER))

    assert response.text == "Недостаточно прав."


@pytest.mark.asyncio
async def test_created_game_returns_separate_master_and_public_links_with_warning(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event, admin=True)

    prompt = await engine.handle(inbound(1, "➕ Создать игру"))
    assert prompt.text == "Введите название игры:"
    response = await engine.handle(inbound(2, "Бал в зимнем дворце"))

    created_events = await engine.events.list_page(limit=10)
    created = next(candidate for candidate in created_events if candidate.name == "Бал в зимнем дворце")
    assert created.master_table_resource_path.endswith("/master_table_Бал в зимнем дворце.xlsx")
    assert created.public_table_resource_path is not None
    assert created.public_table_resource_path.endswith("/public_table_Бал в зимнем дворце.xlsx")
    assert response.text.count("https://disk.example/public/") == 2
    assert MASTER_TABLE_DISCLAIMER in response.text
    assert "Публичная таблица (без контактов Telegram и VK)" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_state", ["ADMIN_ARCHIVE_NAME", "ADMIN_DELETE_NAME"])
async def test_gamemaster_cannot_archive_through_hidden_or_stale_actions(
    archive_state: str, disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, users, publisher, _ = await engine_setup(disk_store, event, gamemaster=True)

    for update, value in enumerate(
        (
            ARCHIVE_GAME,
            LEGACY_DELETE_GAME,
            f"select:admin-archive:{event.event_id}",
            f"select:admin-delete:{event.event_id}",
            f"page:admin-archive:{int(event.created_at.timestamp() * 1_000_000)}:{event.event_id}",
        ),
        start=1,
    ):
        response = await engine.handle(inbound(update, value, callback=value.startswith(("select:", "page:"))))
        assert response.text == "Недостаточно прав."

    user = await users.get(Platform.TELEGRAM, 1)
    assert user is not None
    user.dialog_state = archive_state
    user.dialog_context = {"event_id": event.event_id, "event_name": event.name}
    await users.save(user)
    response = await engine.handle(inbound(10, event.name))

    assert response.text == "Недостаточно прав."
    saved_user = await users.get(Platform.TELEGRAM, 1)
    assert saved_user is not None and saved_user.dialog_state == "IDLE"
    assert publisher.commands == []


@pytest.mark.asyncio
async def test_admin_archive_game_buttons_are_paginated(disk_store: MemoryDiskStore) -> None:
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
        for index in range(11)
    ]
    engine, _, _, _ = await engine_setup(disk_store, *events, admin=True)

    first = await engine.handle(inbound(1, ARCHIVE_GAME))
    game_buttons = [button for button in first.buttons if button.value.startswith("select:admin-archive:")]
    next_button = next(button for button in first.buttons if button.label == "➡️ Далее")
    assert len(game_buttons) == 10

    second = await engine.handle(inbound(2, next_button.value, callback=True))
    assert [button.label for button in second.buttons if button.value.startswith("select:admin-archive:")] == [
        "Game 10"
    ]


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
        if operation is Operation.OPEN_CONFIRMATION:
            assert "DD.MM.YY HH:MM" in queued.text
            queued = await engine.handle(inbound(update, "10.09.26 19:00"))
            update += 1
        assert queued.deferred
        assert publisher.commands[-1].operation is operation
        if operation is Operation.OPEN_CONFIRMATION:
            payload = publisher.commands[-1].payload
            assert isinstance(payload, ConfirmationDeadlinePayload)
            assert payload.deadline.strftime("%d.%m.%y %H:%M") == "10.09.26 19:00"


@pytest.mark.asyncio
async def test_admin_confirmation_deadline_retries_invalid_input_with_same_options(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    event.status = EventStatus.CREATED
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)

    await engine.handle(inbound(1, CHANGE_STATUS))
    await engine.handle(inbound(2, f"select:admin-status:{event.event_id}", callback=True))
    prompt = await engine.handle(inbound(3, "admin:status:confirmation", callback=True))
    invalid = await engine.handle(inbound(4, "9.09.26 19:00"))

    assert "DD.MM.YY HH:MM" in prompt.text
    assert prompt.buttons[0].label == "Ближайший четверг 19:00"
    assert "Некорректная" in invalid.text
    assert invalid.buttons == prompt.buttons
    assert not publisher.commands


@pytest.mark.asyncio
async def test_admin_can_choose_nearest_thursday_deadline(
    monkeypatch: pytest.MonkeyPatch,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    deadline = datetime(2026, 9, 3, 16, tzinfo=UTC)
    monkeypatch.setattr("larp_bot.application.conversation.closest_thursday_19", lambda: deadline)
    event.status = EventStatus.CREATED
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)

    await engine.handle(inbound(1, CHANGE_STATUS))
    await engine.handle(inbound(2, f"select:admin-status:{event.event_id}", callback=True))
    await engine.handle(inbound(3, "admin:status:confirmation", callback=True))
    queued = await engine.handle(inbound(4, "admin:deadline:nearest-thursday", callback=True))

    assert "03.09.26 19:00" in queued.text
    payload = publisher.commands[-1].payload
    assert isinstance(payload, ConfirmationDeadlinePayload)
    assert payload.deadline == deadline


@pytest.mark.asyncio
async def test_admin_has_separate_confirmation_reminder_action(disk_store: MemoryDiskStore, event: Event) -> None:
    event.confirmation_deadline = datetime(2026, 9, 10, 16, tzinfo=UTC)
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)

    menu = await engine.handle(inbound(1, ADMIN))
    assert SEND_CONFIRMATION_REMINDER in [button.value for button in menu.buttons]
    games = await engine.handle(inbound(2, SEND_CONFIRMATION_REMINDER, callback=True))
    assert any(button.value == f"select:admin-reminder:{event.event_id}" for button in games.buttons)
    queued = await engine.handle(inbound(3, f"select:admin-reminder:{event.event_id}", callback=True))

    assert queued.deferred
    assert publisher.commands[-1].operation is Operation.SEND_CONFIRMATION_REMINDER


@pytest.mark.asyncio
async def test_admin_can_queue_message_for_confirmed_players(disk_store: MemoryDiskStore, event: Event) -> None:
    event.status = EventStatus.CLOSED
    engine, _, publisher, _ = await engine_setup(disk_store, event, admin=True)

    menu = await engine.handle(inbound(1, ADMIN))
    assert SEND_CONFIRMED_NOTIFICATION in [button.value for button in menu.buttons]
    games = await engine.handle(inbound(2, SEND_CONFIRMED_NOTIFICATION, callback=True))
    assert any(button.value == f"select:admin-notification:{event.event_id}" for button in games.buttons)
    prompt = await engine.handle(inbound(3, f"select:admin-notification:{event.event_id}", callback=True))
    assert "просто вставить ссылку-приглашение" in prompt.text
    assert "бот сам добавит её и название игры" in prompt.text

    stale_button = await engine.handle(inbound(4, CHANGE_STATUS, callback=True))
    assert "кнопка устарела" in stale_button.text.casefold()
    assert stale_button.buttons == prompt.buttons
    assert not publisher.commands

    queued = await engine.handle(inbound(5, "https://t.me/+GameChat_123"))
    assert queued.deferred
    command = publisher.commands[-1]
    assert command.operation is Operation.SEND_CONFIRMED_NOTIFICATION
    assert isinstance(command.payload, NotificationPayload)
    assert command.payload.text == "https://t.me/+GameChat_123"


@pytest.mark.asyncio
async def test_admin_can_create_one_pass_table_and_list_its_permanent_link(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event, admin=True)

    menu = await engine.handle(inbound(1, ADMIN))
    assert CREATE_PASS_TABLE in [button.value for button in menu.buttons]
    assert LIST_PASS_TABLES in [button.value for button in menu.buttons]

    games = await engine.handle(inbound(2, CREATE_PASS_TABLE, callback=True))
    assert any(button.value == f"select:admin-pass-create:{event.event_id}" for button in games.buttons)
    created = await engine.handle(inbound(3, f"select:admin-pass-create:{event.event_id}", callback=True))
    assert "Участников: 0" in created.text
    assert [(button.label, button.value) for button in created.buttons] == [(ADMIN_MENU, ADMIN_MENU)]
    link = "https://disk.example/public/2"
    assert link in created.text

    await engine.handle(inbound(4, CREATE_PASS_TABLE, callback=True))
    repeated = await engine.handle(inbound(5, f"select:admin-pass-create:{event.event_id}", callback=True))
    assert "уже создана" in repeated.text
    assert [(button.label, button.value) for button in repeated.buttons] == [(ADMIN_MENU, ADMIN_MENU)]
    assert link in repeated.text

    listed = await engine.handle(inbound(6, LIST_PASS_TABLES, callback=True))
    assert event.name in listed.text
    assert link in listed.text


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
        EventAdministrationService(event_repository, showcase, catalog, users, "secret"),
        StaticAdminProvider(tg_ids={1}),
    )
    page = await engine.handle(inbound(1, "📋 Список игр"))
    assert page.text.count("https://disk.example/") == min(count, 10) * 2
    assert page.text.count(MASTER_TABLE_DISCLAIMER) == min(count, 10)
    assert page.text.count("Публичная таблица (без контактов Telegram и VK)") == min(count, 10)
    assert page.text.count("Статус: Регистрация") == min(count, 10)
    has_next = any(button.label == "➡️ Далее" for button in page.buttons)
    assert has_next is (count > 10)
