from __future__ import annotations

from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from larp_bot.adapters.memory import MemoryEventRepository, MemoryRegistrationRepository, MemoryUserRepository
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.application.services import (
    EventNotFound,
    OperationNotAllowed,
    OrderedMutationService,
    RegistrationCatalog,
)
from larp_bot.domain.models import (
    AttendanceStatus,
    CharacterWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    TelegramUser,
)
from tests.conftest import MemoryDiskStore


def command(
    event: Event,
    operation: Operation,
    payload: EnlistPayload | CharacterWishPayload | EmptyPayload,
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
            in {Operation.OPEN_REGISTRATION, Operation.OPEN_CONFIRMATION, Operation.CLOSE_EVENT, Operation.DELETE_EVENT}
            else key
        ),
        payload=payload,
    )


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
        assert workbook.active["C2"].value == "Да"
        assert workbook.active["D2"].value == "Нет"
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

    await mutations.apply(command(event, Operation.OPEN_CONFIRMATION, EmptyPayload()))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
    assert registration.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_close_orders_both_enlist_and_confirmation_rejection(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
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
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))

    await mutations.apply(command(event, Operation.CLOSE_EVENT, EmptyPayload()))
    closed = await events.get(event.event_id)
    assert closed is not None and closed.status is EventStatus.CLOSED

    await mutations.apply(command(event, Operation.OPEN_CONFIRMATION, EmptyPayload()))
    opened = await events.get(event.event_id)
    assert opened is not None and opened.status is EventStatus.CONFIRMATION_OPEN
    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))

    await mutations.apply(command(event, Operation.OPEN_REGISTRATION, EmptyPayload()))
    registration = await events.get(event.event_id)
    assert registration is not None and registration.status is EventStatus.CREATED
    with pytest.raises(OperationNotAllowed, match="ещё не открыто"):
        await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Changed")))


@pytest.mark.asyncio
async def test_delete_prevents_later_character_update(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, RegistrationCatalog(events, tables, showcase))
    await mutations.apply(command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X")))
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    await mutations.apply(command(event, Operation.DELETE_EVENT, EmptyPayload()))
    with pytest.raises(EventNotFound):
        await mutations.apply(
            command(
                event,
                Operation.UPDATE_CHARACTER_WISH,
                CharacterWishPayload(character_wish="New"),
            )
        )
    assert event.disk_resource_path not in disk_store.files
