from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from larp_bot.adapters.memory import MemoryEventRepository, MemoryRegistrationRepository, MemoryUserRepository
from larp_bot.adapters.yandex_disk.repository import PASS_TABLE_HEADERS, YandexDiskShowcaseRepository
from larp_bot.application.services import (
    EventAdministrationService,
    OrderedMutationService,
    RegistrationCatalog,
    daily_pass_table_path,
)
from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    CharacterWishPayload,
    EmptyPayload,
    EnlistPayload,
    EventStatus,
    Operation,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    Registration,
    TelegramUser,
    VkUser,
)
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore


def russian_pass() -> PassDetails:
    return PassDetails(
        surname_cyrillic="Иванов",
        name_cyrillic="Иван",
        patronym_cyrillic="-",
        foreigner=False,
        mobile_phone="+7 999 123-45-67",
        email="ivan@example.com",
    )


def foreign_pass() -> PassDetails:
    return PassDetails(
        surname_cyrillic="Ли",
        name_cyrillic="Анна",
        patronym_cyrillic="-",
        foreigner=True,
        surname_latin="Li",
        name_latin="Anna",
        patronym_latin="-",
        mobile_phone="+44 7700 900123",
        email="anna@example.com",
    )


@pytest.mark.asyncio
async def test_pass_table_uses_all_active_pass_profiles_from_ydb(disk_store: MemoryDiskStore, event) -> None:
    event = event.model_copy(update={"player_amount": 1})
    secret = "participant-secret"
    users = MemoryUserRepository()
    player_profiles = [
        TelegramUser(
            tg_id=11,
            full_name="Иван Иванов",
            vk_url="https://vk.com/ivan",
            crossplay=False,
            larp_experience=True,
            needs_pass=True,
            pass_details=russian_pass(),
        ),
        VkUser(
            vk_id=22,
            full_name="Анна Ли",
            crossplay=True,
            larp_experience=False,
            needs_pass=True,
            pass_details=foreign_pass(),
        ),
        TelegramUser(
            tg_id=33,
            full_name="Ждущий Игрок",
            vk_url="https://vk.com/waiting",
            crossplay=False,
            larp_experience=False,
            needs_pass=True,
            pass_details=russian_pass().model_copy(
                update={
                    "surname_cyrillic": "Ждущий",
                    "name_cyrillic": "Игрок",
                    "email": "waiting@example.com",
                }
            ),
        ),
        TelegramUser(
            tg_id=44,
            full_name="Без Пропуска",
            vk_url="https://vk.com/no-pass",
            crossplay=False,
            larp_experience=False,
            needs_pass=False,
        ),
    ]
    for user in player_profiles:
        await users.save(user)

    registrations = []
    for user, status in zip(
        player_profiles,
        (
            AttendanceStatus.CONFIRMED,
            AttendanceStatus.CONFIRMED,
            AttendanceStatus.WAITING,
            AttendanceStatus.CONFIRMED,
        ),
        strict=True,
    ):
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        uid = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
        registrations.append(
            Registration(
                event_id=event.event_id,
                participant_key=participant_key(secret, platform, uid, event.event_id),
                display_name=user.full_name or "",
                wish_play="Без пожеланий",
                attendance_status=status,
            )
        )

    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository(registrations)
    workbooks = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(events, tables, workbooks)
    service = EventAdministrationService(events, workbooks, catalog, users, secret)

    result = await service.create_pass_table(event.event_id)

    assert result.created is True
    assert result.row_count == 3
    assert result.public_url == "https://disk.example/public/1"
    stored_event = await events.get(event.event_id)
    assert stored_event is not None
    assert stored_event.pass_table_resource_path == f"disk:/larp-bot/passes/{event.event_id}.xlsx"
    assert stored_event.pass_table_public_url == result.public_url

    content = disk_store.files[stored_event.pass_table_resource_path]
    workbook = load_workbook(BytesIO(content))
    try:
        sheet = workbook.active
        assert sheet.title == "Лист1"
        assert tuple(cell.value for cell in sheet[1]) == PASS_TABLE_HEADERS
        assert all(cell.fill.fill_type == "solid" for cell in sheet[1])
        assert all(cell.fill.fgColor.type == "theme" and cell.fill.fgColor.theme == 5 for cell in sheet[1])
        assert sheet.column_dimensions["G"].width == sheet.column_dimensions["C"].width
        table_cells = (
            cell
            for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, min_col=1, max_col=len(PASS_TABLE_HEADERS))
            for cell in row
        )
        assert all(cell.font.name == "Calibri Light" and cell.font.size == 14 for cell in table_cells)
        rows = {
            sheet.cell(row, 1).value: tuple(sheet.cell(row, column).value for column in range(1, 10))
            for row in range(2, 5)
        }
        assert all(cell.fill.fill_type is None for row in sheet.iter_rows(min_row=2) for cell in row)
        assert rows["Иванов"] == (
            "Иванов",
            "Иван",
            "-",
            "Нет",
            None,
            None,
            None,
            "+7 999 123-45-67",
            "ivan@example.com",
        )
        assert rows["Ли"] == (
            "Ли",
            "Анна",
            "-",
            "Да",
            "Li",
            "Anna",
            "-",
            "+44 7700 900123",
            "anna@example.com",
        )
        assert rows["Ждущий"] == (
            "Ждущий",
            "Игрок",
            "-",
            "Нет",
            None,
            None,
            None,
            "+7 999 123-45-67",
            "waiting@example.com",
        )
    finally:
        workbook.close()

    repeated = await service.create_pass_table(event.event_id)
    assert repeated.public_url == result.public_url
    assert repeated.created is False
    assert repeated.row_count is None
    assert len(disk_store.files) == 1


@pytest.mark.asyncio
async def test_pass_table_includes_pass_requiring_master_without_player_registration(
    disk_store: MemoryDiskStore, event
) -> None:
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=51,
            full_name="Иванов Иван",
            vk_url="https://vk.com/master",
            crossplay=False,
            larp_experience=True,
            needs_pass=True,
            pass_details=russian_pass().model_copy(update={"email": "master@example.com"}),
        )
    )
    await users.save(
        TelegramUser(
            tg_id=52,
            full_name="Петров Пётр",
            vk_url="https://vk.com/admin",
            crossplay=False,
            larp_experience=True,
            needs_pass=True,
            pass_details=russian_pass().model_copy(
                update={"surname_cyrillic": "Петров", "name_cyrillic": "Пётр", "email": "admin@example.com"}
            ),
        )
    )
    events = MemoryEventRepository([event])
    await events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=51),
    )
    registrations = MemoryRegistrationRepository()
    workbooks = YandexDiskShowcaseRepository(disk_store)
    service = EventAdministrationService(
        events,
        workbooks,
        RegistrationCatalog(events, registrations, workbooks),
        users,
        "participant-secret",
    )

    result = await service.create_pass_table(event.event_id)

    assert result.row_count == 1
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.pass_table_resource_path is not None
    workbook = load_workbook(BytesIO(disk_store.files[stored_event.pass_table_resource_path]))
    try:
        assert workbook.active.max_row == 2
        assert workbook.active["I2"].value == "master@example.com"
        assert "admin@example.com" not in {cell.value for row in workbook.active.iter_rows() for cell in row}
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_pass_table_tracks_signup_confirmation_and_cancellation_without_changing_url(
    disk_store: MemoryDiskStore, event
) -> None:
    secret = "participant-secret"
    users = MemoryUserRepository()
    await users.save(
        TelegramUser(
            tg_id=11,
            full_name="Иван Иванов",
            vk_url="https://vk.com/ivan",
            crossplay=False,
            larp_experience=True,
            needs_pass=True,
            pass_details=russian_pass(),
        )
    )
    events = MemoryEventRepository([event])
    registrations = MemoryRegistrationRepository()
    workbooks = YandexDiskShowcaseRepository(disk_store)
    await workbooks.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, registrations, workbooks)
    administration = EventAdministrationService(events, workbooks, catalog, users, secret)
    export = await administration.create_pass_table(event.event_id)
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.pass_table_resource_path is not None
    pass_path = stored_event.pass_table_resource_path

    mutations = OrderedMutationService(events, catalog, users, secret)
    key = participant_key(secret, Platform.TELEGRAM, 11, event.event_id)

    def mutation(operation: Operation, payload: EnlistPayload | CharacterWishPayload | EmptyPayload):
        return OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=event.event_id,
            operation=operation,
            platform=Platform.TELEGRAM,
            platform_user_id=11,
            participant_key=key,
            payload=payload,
        )

    await mutations.apply(
        mutation(Operation.ENLIST, EnlistPayload(display_name="Иван Иванов", wish_play="Без пожеланий"))
    )

    waiting = load_workbook(BytesIO(disk_store.files[pass_path]))
    try:
        assert waiting.active.max_row == 2
        assert waiting.active["A2"].value == "Иванов"
    finally:
        waiting.close()
    assert disk_store.public_urls[pass_path] == export.public_url
    assert disk_store.replace_count[pass_path] == 1

    await mutations.apply(mutation(Operation.CONFIRM, CharacterWishPayload(character_wish="Без пожеланий")))

    confirmed = load_workbook(BytesIO(disk_store.files[pass_path]))
    try:
        assert confirmed.active.max_row == 2
        assert confirmed.active["A2"].value == "Иванов"
    finally:
        confirmed.close()
    assert disk_store.public_urls[pass_path] == export.public_url
    assert disk_store.replace_count[pass_path] == 2

    await mutations.apply(mutation(Operation.CANCEL, EmptyPayload()))

    cancelled = load_workbook(BytesIO(disk_store.files[pass_path]))
    try:
        assert cancelled.active.max_row == 1
    finally:
        cancelled.close()
    refreshed_event = await events.get(event.event_id)
    assert refreshed_event is not None
    assert refreshed_event.pass_table_public_url == export.public_url
    assert disk_store.public_urls[pass_path] == export.public_url
    assert disk_store.replace_count[pass_path] == 3


@pytest.mark.asyncio
async def test_archived_game_keeps_its_registration_and_pass_tables(disk_store: MemoryDiskStore, event) -> None:
    events = MemoryEventRepository([event])
    tables = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    workbooks = YandexDiskShowcaseRepository(disk_store)
    await workbooks.create_event_workbook(event.disk_resource_path)
    catalog = RegistrationCatalog(events, tables, workbooks)
    service = EventAdministrationService(events, workbooks, catalog, users, "secret")
    await service.create_pass_table(event.event_id)
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.pass_table_resource_path is not None

    await catalog.archive(stored_event)

    archived_event = await events.get(event.event_id)
    assert event.disk_resource_path in disk_store.files
    assert stored_event.pass_table_resource_path in disk_store.files
    assert archived_event is not None and archived_event.status is EventStatus.CLOSED
    assert archived_event.archived_at is not None


@pytest.mark.asyncio
async def test_daily_pass_table_deduplicates_same_day_players_and_keeps_its_url_current(
    disk_store: MemoryDiskStore,
    event,
) -> None:
    secret = "participant-secret"
    game_date = date(2026, 11, 14)
    first = event.model_copy(update={"game_date": game_date})
    second = event.model_copy(
        update={
            "event_id": "event-b2",
            "name": "Вторая игра",
            "game_date": game_date,
            "disk_resource_path": "disk:/larp-bot/events/event-b2-master.xlsx",
            "public_registration_url": "https://disk.example/public/b2",
        }
    )
    other_day = event.model_copy(
        update={
            "event_id": "event-c3",
            "name": "Игра завтра",
            "game_date": date(2026, 11, 15),
            "disk_resource_path": "disk:/larp-bot/events/event-c3-master.xlsx",
            "public_registration_url": "https://disk.example/public/c3",
        }
    )
    users = MemoryUserRepository()
    player = TelegramUser(
        tg_id=11,
        full_name="Иван Иванов",
        vk_url="https://vk.com/ivan",
        linked_vk_id=22,
        telegram_handle="@ivan11",
        crossplay=False,
        larp_experience=True,
        needs_pass=True,
        pass_details=russian_pass(),
    )
    linked_player = VkUser(
        vk_id=22,
        telegram_handle="@ivan11",
        full_name="Иван Иванов",
        crossplay=False,
        larp_experience=True,
        needs_pass=False,
    )
    player.updated_at = linked_player.updated_at + timedelta(seconds=1)
    tomorrow_player = TelegramUser(
        tg_id=33,
        full_name="Анна Ли",
        vk_url="https://vk.com/anna",
        crossplay=False,
        larp_experience=True,
        needs_pass=True,
        pass_details=foreign_pass(),
    )
    await users.save(player)
    await users.save(linked_player)
    await users.save(tomorrow_player)
    events = MemoryEventRepository([first, second, other_day])
    registrations = MemoryRegistrationRepository(
        [
            Registration(
                event_id=first.event_id,
                participant_key=participant_key(secret, Platform.TELEGRAM, 11, first.event_id),
                display_name="Иван Иванов",
                wish_play="Без пожеланий",
            ),
            Registration(
                event_id=second.event_id,
                participant_key=participant_key(secret, Platform.VK, 22, second.event_id),
                display_name="Иван Иванов",
                wish_play="Без пожеланий",
            ),
            Registration(
                event_id=other_day.event_id,
                participant_key=participant_key(secret, Platform.TELEGRAM, 33, other_day.event_id),
                display_name="Анна Ли",
                wish_play="Без пожеланий",
            ),
        ]
    )
    workbooks = YandexDiskShowcaseRepository(disk_store)
    for current in (first, second, other_day):
        await workbooks.create_event_workbook(current.disk_resource_path)
    catalog = RegistrationCatalog(events, registrations, workbooks)
    administration = EventAdministrationService(events, workbooks, catalog, users, secret)

    export = await administration.create_daily_pass_table(game_date)
    path = daily_pass_table_path(game_date)

    assert export.row_count == 1
    assert export.public_url == disk_store.public_urls[path]
    initial = load_workbook(BytesIO(disk_store.files[path]))
    try:
        assert initial.active.max_row == 2
        assert initial.active["I2"].value == "ivan@example.com"
    finally:
        initial.close()

    player.pass_details = russian_pass().model_copy(update={"email": "new@example.com"})
    await users.save(player)
    mutations = OrderedMutationService(events, catalog, users, secret)

    def mutation(current, operation: Operation) -> OrderedRegistrationCommand:
        return OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=current.event_id,
            operation=operation,
            platform=Platform.TELEGRAM,
            platform_user_id=11,
            participant_key=participant_key(secret, Platform.TELEGRAM, 11, current.event_id),
            payload=EmptyPayload(),
        )

    await mutations.apply(mutation(first, Operation.UPDATE_PROFILE))
    updated = load_workbook(BytesIO(disk_store.files[path]))
    try:
        assert updated.active.max_row == 2
        assert updated.active["I2"].value == "new@example.com"
    finally:
        updated.close()

    await mutations.apply(mutation(first, Operation.CANCEL))
    still_registered = load_workbook(BytesIO(disk_store.files[path]))
    try:
        assert still_registered.active.max_row == 2
        assert still_registered.active["I2"].value == "new@example.com"
    finally:
        still_registered.close()

    player.pass_details = None
    player.needs_pass = False
    player.updated_at += timedelta(seconds=1)
    await users.save(player)
    await administration.refresh_daily_pass_tables_for_user(Platform.TELEGRAM, player.tg_id)
    no_longer_needs_pass = load_workbook(BytesIO(disk_store.files[path]))
    try:
        assert no_longer_needs_pass.active.max_row == 1
    finally:
        no_longer_needs_pass.close()

    player.needs_pass = True
    player.pass_details = russian_pass().model_copy(update={"email": "restored@example.com"})
    player.updated_at += timedelta(seconds=1)
    await users.save(player)
    await administration.refresh_daily_pass_tables_for_user(Platform.TELEGRAM, player.tg_id)

    await mutations.apply(mutation(second, Operation.CANCEL))
    cancelled_everywhere = load_workbook(BytesIO(disk_store.files[path]))
    try:
        assert cancelled_everywhere.active.max_row == 1
    finally:
        cancelled_everywhere.close()

    repeated = await administration.create_daily_pass_table(game_date)
    assert repeated.public_url == export.public_url
    assert disk_store.public_urls[path] == export.public_url
    assert repeated.row_count == 0
