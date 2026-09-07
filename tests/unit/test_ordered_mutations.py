from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from larp_bot.adapters.memory import (
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
    StaticAdminProvider,
)
from larp_bot.adapters.yandex_disk.repository import PUBLIC_HEADERS, YandexDiskShowcaseRepository
from larp_bot.application.services import (
    OperationNotAllowed,
    OrderedMutationService,
    RegistrationCatalog,
)
from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    PlayerAmountPayload,
    Registration,
    TelegramUser,
)
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore


def command(
    event: Event,
    operation: Operation,
    payload: EnlistPayload
    | CharacterWishPayload
    | ConfirmationDeadlinePayload
    | NotificationPayload
    | PlayerAmountPayload
    | EmptyPayload,
    *,
    key: str = "a" * 43,
) -> OrderedRegistrationCommand:
    return OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=operation,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key=(
            None
            if operation
            in {
                Operation.OPEN_REGISTRATION,
                Operation.OPEN_CONFIRMATION,
                Operation.SEND_CONFIRMATION_REMINDER,
                Operation.SEND_CONFIRMED_NOTIFICATION,
                Operation.UPDATE_PLAYER_AMOUNT,
                Operation.REMOVE_WAITING_PARTICIPANTS,
                Operation.CLOSE_EVENT,
                Operation.ARCHIVE_EVENT,
                Operation.DELETE_EVENT,
            }
            else key
        ),
        payload=payload,
    )


async def add_command_author_as_leader(events: MemoryEventRepository, event: Event) -> None:
    await events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=1),
    )


@pytest.mark.asyncio
async def test_confirmed_notification_retry_appends_to_event_once(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    notification = command(
        event,
        Operation.SEND_CONFIRMED_NOTIFICATION,
        NotificationPayload(text="https://t.me/+GameChat_123"),
    )

    await mutations.apply(notification)
    await mutations.apply(notification)

    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.confirmed_notifications == ["https://t.me/+GameChat_123"]


@pytest.mark.asyncio
async def test_worker_rejects_privileged_mutation_from_nonleader(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    mutations = OrderedMutationService(
        events,
        RegistrationCatalog(events, tables, YandexDiskShowcaseRepository(disk_store)),
    )

    with pytest.raises(OperationNotAllowed, match="Только мастера"):
        await mutations.apply(
            command(
                event,
                Operation.SEND_CONFIRMED_NOTIFICATION,
                NotificationPayload(text="Несанкционированное уведомление"),
            )
        )

    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.confirmed_notifications == []


@pytest.mark.asyncio
async def test_worker_allows_admin_privileged_mutation_without_event_master_membership(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    mutations = OrderedMutationService(
        events,
        RegistrationCatalog(events, tables, YandexDiskShowcaseRepository(disk_store)),
        admins=StaticAdminProvider(tg_ids={1}),
    )

    await mutations.apply(
        command(
            event,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            NotificationPayload(text="Администраторское уведомление"),
        )
    )

    assert await events.list_leaders(event.event_id) == []
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.confirmed_notifications == ["Администраторское уведомление"]


@pytest.mark.asyncio
async def test_player_amount_change_is_retryable_and_moves_reserve_row_in_both_tables(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    public_path = f"disk:/larp-bot/events/{event.event_id}/public_table_{event.name}.xlsx"
    event = event.model_copy(
        update={
            "player_amount": 4,
            "public_table_resource_path": public_path,
            "public_table_public_url": "https://disk.example/public-table",
        }
    )
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, player_amount=event.player_amount)
    await showcase.create_public_event_workbook(public_path, player_amount=event.player_amount)
    for index in range(3):
        await tables.enlist(
            event.event_id,
            operation_id=f"enlist-{index}",
            participant_key=str(index) * 43,
            display_name=f"Игрок {index + 1}",
            wish_play="Без пожеланий",
        )
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    update = command(event, Operation.UPDATE_PLAYER_AMOUNT, PlayerAmountPayload(player_amount=2))

    result = await mutations.apply(update)
    await mutations.apply(update)

    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.player_amount == 2
    assert result.text == "Количество игроков изменено: 2"
    for path in (event.disk_resource_path, public_path):
        workbook = load_workbook(BytesIO(disk_store.files[path]))
        try:
            assert workbook.active.cell(4, 1).value == "ЗАПАС"
            assert workbook.active.cell(5, 1).value == 3
            assert workbook.active.cell(5, 2).value == "Игрок 3"
        finally:
            workbook.close()
        assert disk_store.replace_count[path] == 2


@pytest.mark.asyncio
async def test_legacy_enlist_command_backfills_profile_columns(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Player",
            vk_url="https://vk.com/player",
            crossplay=False,
            larp_experience=True,
            needs_pass=False,
        )
    )
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase), users)

    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A")))

    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        assert workbook.active["C2"].value == "https://vk.com/player"
        assert workbook.active["D2"].value is None
        assert workbook.active["E2"].value == "Да"
        assert workbook.active["F2"].value == "Нет"
    finally:
        workbook.close()
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.public_table_resource_path is not None
    public_workbook = load_workbook(BytesIO(disk_store.files[stored_event.public_table_resource_path]))
    try:
        assert tuple(cell.value for cell in public_workbook.active[1]) == PUBLIC_HEADERS
        assert public_workbook.active.max_column == 6
        assert "https://vk.com/player" not in {cell.value for row in public_workbook.active.iter_rows() for cell in row}
    finally:
        public_workbook.close()


@pytest.mark.asyncio
async def test_profile_update_refreshes_registration_and_every_existing_game_table(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    public_path = f"disk:/larp-bot/events/{event.event_id}/public_table_{event.name}.xlsx"
    pass_path = f"disk:/larp-bot/passes/{event.event_id}.xlsx"
    event = event.model_copy(
        update={
            "public_table_resource_path": public_path,
            "public_table_public_url": "https://disk.example/public-table",
            "pass_table_resource_path": pass_path,
            "pass_table_public_url": "https://disk.example/pass-table",
        }
    )
    secret = "participant-secret"
    key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    old_pass = PassDetails(
        surname_cyrillic="Старов",
        name_cyrillic="Стар",
        patronym_cyrillic="Старович",
        foreigner=False,
        mobile_phone="+7 900 000-00-00",
        email="old@example.com",
    )
    new_pass = PassDetails(
        surname_cyrillic="Новиков",
        name_cyrillic="Новый",
        patronym_cyrillic="Нович",
        foreigner=False,
        mobile_phone="+7 999 111-22-33",
        email="new@example.com",
    )
    registration = Registration(
        event_id=event.event_id,
        participant_key=key,
        display_name="Старов Стар Старович",
        vk_profile="https://vk.com/old-profile",
        telegram_profile="https://t.me/old_profile",
        wish_play="С другом",
        larp_experience=True,
        crossplay=False,
        character_wish="Врач",
        attendance_status=AttendanceStatus.CONFIRMED,
        last_operation_id="confirm-old",
        created_at=created_at,
        updated_at=created_at,
    )
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository([registration])
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Новиков Новый Нович",
            vk_url="https://vk.com/new-profile",
            telegram_handle="@new_profile",
            crossplay=True,
            larp_experience=False,
            needs_pass=True,
            pass_details=new_pass,
        )
    )
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path, [registration])
    await showcase.create_public_event_workbook(public_path, [registration])
    await showcase.create_pass_table(pass_path, [old_pass])
    mutations = OrderedMutationService(
        events,
        RegistrationCatalog(events, tables, showcase),
        users,
        secret,
    )
    update = command(event, Operation.UPDATE_PROFILE, EmptyPayload(), key=key)

    result = await mutations.apply(update)

    stored = await tables.get(event.event_id, key)
    assert result.notifications_enabled is False
    assert stored is not None
    assert stored.display_name == "Новиков Новый Нович"
    assert stored.vk_profile == "https://vk.com/new-profile"
    assert stored.telegram_profile == "https://t.me/new_profile"
    assert stored.larp_experience is False
    assert stored.crossplay is True
    assert stored.wish_play == "С другом"
    assert stored.character_wish == "Врач"
    assert stored.attendance_status is AttendanceStatus.CONFIRMED
    assert stored.created_at == created_at

    master = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    public = load_workbook(BytesIO(disk_store.files[public_path]))
    passes = load_workbook(BytesIO(disk_store.files[pass_path]))
    try:
        assert master.active["B2"].value == "Новиков Новый Нович"
        assert master.active["C2"].value == "https://vk.com/new-profile"
        assert master.active["D2"].value == "https://t.me/new_profile"
        assert master.active["E2"].value == "Нет"
        assert master.active["F2"].value == "Да"
        assert public.active["B2"].value == "Новиков Новый Нович"
        assert public.active["C2"].value == "Да"
        assert passes.active["A2"].value == "Новиков"
        assert passes.active["H2"].value == "+7 999 111-22-33"
        assert passes.active["I2"].value == "new@example.com"
    finally:
        master.close()
        public.close()
        passes.close()


@pytest.mark.asyncio
async def test_profile_update_refreshes_pass_table_for_master_without_registration(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    pass_path = f"disk:/larp-bot/passes/{event.event_id}.xlsx"
    event = event.model_copy(
        update={
            "pass_table_resource_path": pass_path,
            "pass_table_public_url": "https://disk.example/pass-table",
        }
    )
    secret = "participant-secret"
    key = participant_key(secret, Platform.TELEGRAM, 1, event.event_id)
    profile = PassDetails(
        surname_cyrillic="Мастеров",
        name_cyrillic="Мастер",
        patronym_cyrillic="Мастерович",
        foreigner=False,
        mobile_phone="+7 999 444-55-66",
        email="master@example.com",
    )
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Мастеров Мастер Мастерович",
            vk_url="https://vk.com/master",
            crossplay=True,
            larp_experience=True,
            needs_pass=True,
            pass_details=profile,
        )
    )
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_pass_table(pass_path, [])
    mutations = OrderedMutationService(
        events,
        RegistrationCatalog(events, tables, showcase),
        users,
        secret,
    )

    await mutations.apply(command(event, Operation.UPDATE_PROFILE, EmptyPayload(), key=key))

    workbook = load_workbook(BytesIO(disk_store.files[pass_path]))
    try:
        assert workbook.active["A2"].value == "Мастеров"
        assert workbook.active["I2"].value == "master@example.com"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_worker_rejects_enlist_when_ydb_profile_is_missing(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase), users)

    with pytest.raises(OperationNotAllowed, match="полностью заполните профиль"):
        await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A")))

    assert await tables.get(event.event_id, "a" * 43) is None


@pytest.mark.asyncio
async def test_enlist_persists_both_profiles_in_showcase(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    payload = EnlistPayload(
        display_name="Player",
        wish_play="A",
        vk_profile="https://vk.com/player",
        telegram_profile="https://t.me/player",
    )

    await mutations.apply(command(event, Operation.ENLIST, payload))

    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
    assert registration.vk_profile == "https://vk.com/player"
    assert registration.telegram_profile == "https://t.me/player"
    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        assert workbook.active["C2"].value == "https://vk.com/player"
        assert workbook.active["D2"].value == "https://t.me/player"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_fifo_sequence_has_required_final_state(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    sequence = [
        command(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A")),
        command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A")),
        command(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B")),
        command(event, Operation.CANCEL, EmptyPayload()),
    ]
    for item in sequence:
        await mutations.apply(item)
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
    assert registration.character_wish == "B"
    assert registration.attendance_status is AttendanceStatus.CANCELLED
    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        assert workbook.active.max_row == event.player_amount + 2
        assert workbook.active.cell(event.player_amount + 2, 1).value == "ЗАПАС"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_fifo_character_update_preserves_confirmed_state(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    for item in (
        command(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A")),
        command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A")),
        command(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B")),
    ):
        await mutations.apply(item)
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
    assert registration.character_wish == "B"
    assert registration.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_created_game_allows_enlist_but_rejects_confirmation_until_admin_opens_it(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    event.status = EventStatus.CREATED
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    with pytest.raises(OperationNotAllowed, match="ещё не открыто"):
        await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))

    deadline = datetime(2026, 9, 10, 16, tzinfo=UTC)
    await add_command_author_as_leader(events, event)
    await mutations.apply(command(event, Operation.OPEN_CONFIRMATION, ConfirmationDeadlinePayload(deadline=deadline)))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
    assert registration.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_close_orders_enlist_confirmation_and_cancellation_rejection(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    await mutations.apply(command(event, Operation.CLOSE_EVENT, EmptyPayload()))
    with pytest.raises(OperationNotAllowed):
        await mutations.apply(
            command(
                event,
                Operation.ENLIST,
                EnlistPayload(display_name="User B", wish_play="X"),
                key="b" * 43,
            )
        )
    with pytest.raises(OperationNotAllowed, match="закрыто"):
        await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Changed")))
    with pytest.raises(OperationNotAllowed, match="Состав игроков зафиксирован"):
        await mutations.apply(command(event, Operation.CANCEL, EmptyPayload()))
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
    assert registration.attendance_status is AttendanceStatus.CONFIRMED
    await mutations.apply(command(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="Medic")))
    updated = await tables.get(event.event_id, "a" * 43)
    assert updated is not None
    assert updated.character_wish == "Medic"
    assert updated.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_admin_status_commands_allow_any_transition(disk_store: MemoryDiskStore, event: Event) -> None:
    event.status = EventStatus.CREATED
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))

    await mutations.apply(command(event, Operation.CLOSE_EVENT, EmptyPayload()))
    closed = await events.get(event.event_id)
    assert closed is not None and closed.status is EventStatus.CLOSED

    deadline = datetime(2026, 9, 10, 16, tzinfo=UTC)
    await mutations.apply(command(event, Operation.OPEN_CONFIRMATION, ConfirmationDeadlinePayload(deadline=deadline)))
    opened = await events.get(event.event_id)
    assert opened is not None and opened.status is EventStatus.CONFIRMATION_OPEN
    assert opened.confirmation_deadline == deadline
    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))

    await mutations.apply(command(event, Operation.OPEN_REGISTRATION, EmptyPayload()))
    registration = await events.get(event.event_id)
    assert registration is not None and registration.status is EventStatus.CREATED
    with pytest.raises(OperationNotAllowed, match="ещё не открыто"):
        await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Changed")))


@pytest.mark.asyncio
async def test_legacy_delete_command_archives_without_deleting_game_data(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    result = await mutations.apply(command(event, Operation.DELETE_EVENT, EmptyPayload()))

    stored_event = await events.get(event.event_id)
    stored_registration = await tables.get(event.event_id, "a" * 43)
    assert result.text == "Игра архивирована; таблицы и записи сохранены"
    assert stored_event is not None and stored_event.status is EventStatus.CLOSED
    assert stored_event.archived_at is not None
    assert stored_registration is not None and stored_registration.character_wish == "Doctor"
    assert event.disk_resource_path in disk_store.files


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")),
        (Operation.CANCEL, EmptyPayload()),
        (Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="Medic")),
    ],
)
async def test_archived_game_rejects_participant_mutations(
    operation: Operation,
    payload: EnlistPayload | EmptyPayload | CharacterWishPayload,
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    if operation is not Operation.ENLIST:
        await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    if operation is Operation.UPDATE_CHARACTER_WISH:
        await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    await events.archive(event.event_id, datetime.now(UTC))

    with pytest.raises(OperationNotAllowed, match="архивирована"):
        await mutations.apply(command(event, operation, payload))


@pytest.mark.asyncio
async def test_leader_removal_deletes_registration_and_refreshes_every_table(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    pass_path = f"disk:/larp-bot/passes/{event.event_id}.xlsx"
    event = Event.model_validate(
        event.model_dump()
        | {
            "pass_table_resource_path": pass_path,
            "pass_table_public_url": "https://disk.example/pass-table",
        }
    )
    profile = PassDetails(
        surname_cyrillic="Иванов",
        name_cyrillic="Иван",
        patronym_cyrillic="Иванович",
        foreigner=False,
        mobile_phone="+79991234567",
        email="ivan@example.com",
    )
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=1,
            full_name="Иванов Иван",
            vk_url="https://vk.com/player",
            crossplay=True,
            larp_experience=True,
            needs_pass=True,
            pass_details=profile,
        )
    )
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    await showcase.create_pass_table(pass_path, [profile])
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    mutations = OrderedMutationService(
        events,
        RegistrationCatalog(events, tables, showcase),
        users,
        "participant-secret",
    )
    key = participant_key("participant-secret", Platform.TELEGRAM, 1, event.event_id)
    await mutations.apply(
        command(
            event,
            Operation.ENLIST,
            EnlistPayload(display_name="Иванов Иван", wish_play="Без пожеланий"),
            key=key,
        )
    )
    await mutations.apply(
        command(
            event,
            Operation.CONFIRM,
            CharacterWishPayload(character_wish="Без пожеланий"),
            key=key,
        )
    )
    removal = command(event, Operation.REMOVE_PARTICIPANT, EmptyPayload(), key=key)

    assert (await mutations.apply(removal)).text == "Игрок удалён"
    assert (await mutations.apply(removal)).text == "Игрок уже удалён"
    assert await tables.get(event.event_id, key) is None

    refreshed_event = await events.get(event.event_id)
    assert refreshed_event is not None and refreshed_event.public_table_resource_path is not None
    for path in (event.disk_resource_path, refreshed_event.public_table_resource_path):
        workbook = load_workbook(BytesIO(disk_store.files[path]))
        try:
            assert workbook.active.max_row == event.player_amount + 2
            assert workbook.active.cell(event.player_amount + 2, 1).value == "ЗАПАС"
        finally:
            workbook.close()
    pass_workbook = load_workbook(BytesIO(disk_store.files[pass_path]))
    try:
        assert pass_workbook.active.max_row == 2
        assert pass_workbook.active["I2"].value == "ivan@example.com"
    finally:
        pass_workbook.close()


@pytest.mark.asyncio
async def test_nonleader_cannot_remove_event_participant(disk_store: MemoryDiskStore, event: Event) -> None:
    registration_key = "a" * 43
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=registration_key,
                display_name="Player",
                wish_play="Anyone",
            )
        ]
    )
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(
        events,
        RegistrationCatalog(events, tables, YandexDiskShowcaseRepository(disk_store)),
    )

    with pytest.raises(OperationNotAllowed, match="Только мастера"):
        await mutations.apply(command(event, Operation.REMOVE_PARTICIPANT, EmptyPayload(), key=registration_key))

    assert await tables.get(event.event_id, registration_key) is not None


@pytest.mark.asyncio
async def test_bulk_removal_deletes_only_waiting_players_and_keeps_retry_snapshot(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    waiting_keys = ["a" * 43, "b" * 43]
    confirmed_key = "c" * 43
    tables = MemoryRegistrationRepository(
        [
            Registration(
                event_id=event.event_id,
                participant_key=key,
                display_name=f"Waiting {index}",
                wish_play="Anyone",
            )
            for index, key in enumerate(waiting_keys)
        ]
        + [
            Registration(
                event_id=event.event_id,
                participant_key=confirmed_key,
                display_name="Confirmed",
                wish_play="Anyone",
                attendance_status=AttendanceStatus.CONFIRMED,
            )
        ]
    )
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    removal = command(event, Operation.REMOVE_WAITING_PARTICIPANTS, EmptyPayload())

    assert (await mutations.apply(removal)).text == "Удалено не подтвердивших участие: 2"
    assert (await mutations.apply(removal)).text == "Удалено не подтвердивших участие: 2"
    assert [await tables.get(event.event_id, key) for key in waiting_keys] == [None, None]
    assert await tables.get(event.event_id, confirmed_key) is not None
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.last_participant_removal_operation_id == removal.operation_id
    assert stored_event.participant_removal_keys == waiting_keys


@pytest.mark.asyncio
async def test_archive_operation_is_distinct_from_closing_registration(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    events = MemoryEventRepository([event])
    await add_command_author_as_leader(events, event)
    showcase = YandexDiskShowcaseRepository(disk_store)
    mutations = OrderedMutationService(events, RegistrationCatalog(events, MemoryRegistrationRepository(), showcase))

    await mutations.apply(command(event, Operation.CLOSE_EVENT, EmptyPayload()))
    closed = await events.get(event.event_id)
    assert closed is not None and closed.archived_at is None

    result = await mutations.apply(command(event, Operation.ARCHIVE_EVENT, EmptyPayload()))
    archived = await events.get(event.event_id)
    assert result.text == "Игра архивирована; таблицы и записи сохранены"
    assert archived is not None and archived.archived_at is not None
    with pytest.raises(OperationNotAllowed, match="архивирована"):
        await mutations.apply(command(event, Operation.OPEN_REGISTRATION, EmptyPayload()))
