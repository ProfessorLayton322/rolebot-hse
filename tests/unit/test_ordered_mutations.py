from __future__ import annotations

from uuid import uuid4

import pytest

from larp_bot.adapters.memory import MemoryEventRepository
from larp_bot.adapters.yandex_disk.repository import YandexDiskRegistrationRepository
from larp_bot.application.services import EventNotFound, OperationNotAllowed, OrderedMutationService
from larp_bot.domain.models import (
    AttendanceStatus,
    CharacterWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    Operation,
    OrderedRegistrationCommand,
    Platform,
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
        participant_key=(None if operation in {Operation.CLOSE_EVENT, Operation.DELETE_EVENT} else key),
        payload=payload,
    )


@pytest.mark.asyncio
async def test_fifo_sequence_has_required_final_state(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = YandexDiskRegistrationRepository(disk_store)
    await tables.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, tables)
    sequence = [
        command(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A", dont_wish_play="B")),
        command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A")),
        command(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B")),
        command(event, Operation.CANCEL, EmptyPayload()),
    ]
    for item in sequence:
        await mutations.apply(item)
    registration = await tables.find_registration(event, "a" * 43)
    assert registration is not None
    assert registration.character_wish == "B"
    assert registration.attendance_status is AttendanceStatus.CANCELLED


@pytest.mark.asyncio
async def test_fifo_character_update_preserves_confirmed_state(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = YandexDiskRegistrationRepository(disk_store)
    await tables.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, tables)
    for item in (
        command(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A", dont_wish_play="B")),
        command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A")),
        command(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B")),
    ):
        await mutations.apply(item)
    registration = await tables.find_registration(event, "a" * 43)
    assert registration is not None
    assert registration.character_wish == "B"
    assert registration.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_close_orders_enlist_rejection_but_allows_existing_confirmation(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    tables = YandexDiskRegistrationRepository(disk_store)
    await tables.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, tables)
    await mutations.apply(
        command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X", dont_wish_play="Y"))
    )
    await mutations.apply(command(event, Operation.CLOSE_EVENT, EmptyPayload()))
    with pytest.raises(OperationNotAllowed):
        await mutations.apply(
            command(
                event,
                Operation.ENLIST,
                EnlistPayload(display_name="User B", wish_play="X", dont_wish_play="Y"),
                key="b" * 43,
            )
        )
    await mutations.apply(command(event, Operation.CONFIRM, CharacterWishPayload(character_wish="Doctor")))
    registration = await tables.find_registration(event, "a" * 43)
    assert registration is not None
    assert registration.attendance_status is AttendanceStatus.CONFIRMED
    await mutations.apply(command(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="Medic")))
    updated = await tables.find_registration(event, "a" * 43)
    assert updated is not None
    assert updated.character_wish == "Medic"
    assert updated.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_delete_prevents_later_character_update(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = YandexDiskRegistrationRepository(disk_store)
    await tables.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    mutations = OrderedMutationService(events, tables)
    await mutations.apply(
        command(event, Operation.ENLIST, EnlistPayload(display_name="User A", wish_play="X", dont_wish_play="Y"))
    )
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
