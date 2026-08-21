from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import load_workbook

from larp_bot.adapters.yandex_disk.repository import ALL_HEADERS, YandexDiskRegistrationRepository
from larp_bot.application.services import OperationNotAllowed
from larp_bot.domain.models import AttendanceStatus, Event
from tests.conftest import MemoryDiskStore


async def initialized(store: MemoryDiskStore, event: Event) -> YandexDiskRegistrationRepository:
    repository = YandexDiskRegistrationRepository(store)
    public_url = await repository.create_event_workbook(event.disk_resource_path)
    assert public_url == "https://disk.example/public/1"
    return repository


@pytest.mark.asyncio
async def test_workbook_schema_has_no_negative_co_player_column(disk_store: MemoryDiskStore, event: Event) -> None:
    await initialized(disk_store, event)
    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        headers = tuple(cell.value for cell in workbook.active[1])
        assert headers == ALL_HEADERS
        assert headers == (
            "Имя",
            "С кем хочу играть",
            "Пожелания по персонажу",
            "Статус",
            "participant_key",
            "last_operation_id",
            "updated_at",
        )
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_enlist_starts_blank_and_waiting(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    await repository.enlist(
        event,
        operation_id="op-enlist",
        participant_key="a" * 43,
        display_name="Иван Иванов",
        wish_play="С Алисой",
    )
    registration = await repository.find_registration(event, "a" * 43)
    assert registration is not None
    assert registration.character_wish == ""
    assert registration.attendance_status is AttendanceStatus.WAITING


@pytest.mark.asyncio
async def test_character_wishes_are_isolated_per_event(disk_store: MemoryDiskStore) -> None:
    repository = YandexDiskRegistrationRepository(disk_store)
    event_a = Event(
        event_id="event-a1",
        name="A",
        disk_resource_path="disk:/larp-bot/events/event-a1-a.xlsx",
        public_registration_url="https://disk.example/a",
    )
    event_b = Event(
        event_id="event-b1",
        name="B",
        disk_resource_path="disk:/larp-bot/events/event-b1-b.xlsx",
        public_registration_url="https://disk.example/b",
    )
    await repository.create_event_workbook(event_a.disk_resource_path)
    await repository.create_event_workbook(event_b.disk_resource_path)
    key_a, key_b = "a" * 43, "b" * 43
    for target, key in ((event_a, key_a), (event_b, key_b)):
        await repository.enlist(
            target,
            operation_id=f"enlist-{key[0]}",
            participant_key=key,
            display_name="Player",
            wish_play="Anyone",
        )
    await repository.confirm(event_a, operation_id="confirm-a", participant_key=key_a, character_wish="Doctor")
    await repository.confirm(event_b, operation_id="confirm-b", participant_key=key_b, character_wish="Soldier")
    await repository.update_character_wish(
        event_a, operation_id="edit-a", participant_key=key_a, character_wish="Medic"
    )
    assert (await repository.find_registration(event_a, key_a)).character_wish == "Medic"  # type: ignore[union-attr]
    assert (await repository.find_registration(event_b, key_b)).character_wish == "Soldier"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_confirm_is_atomic_and_edits_preserve_status(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    key = "a" * 43
    await repository.enlist(
        event,
        operation_id="one",
        participant_key=key,
        display_name="Player",
        wish_play="Anyone",
    )
    await repository.confirm(event, operation_id="two", participant_key=key, character_wish="Doctor")
    confirmed = await repository.find_registration(event, key)
    assert confirmed is not None
    assert (confirmed.character_wish, confirmed.attendance_status) == (
        "Doctor",
        AttendanceStatus.CONFIRMED,
    )
    await repository.update_character_wish(event, operation_id="three", participant_key=key, character_wish="No wishes")
    edited = await repository.find_registration(event, key)
    assert edited is not None
    assert edited.attendance_status is AttendanceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_explicit_no_wishes_is_distinct_from_initial_blank(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    key = "a" * 43
    await repository.enlist(
        event,
        operation_id="one",
        participant_key=key,
        display_name="Player",
        wish_play="A",
    )
    waiting = await repository.find_registration(event, key)
    assert waiting is not None and waiting.character_wish == ""
    before_confirm = disk_store.replace_count[event.disk_resource_path]
    await repository.confirm(
        event,
        operation_id="two",
        participant_key=key,
        character_wish="Без пожеланий",
    )
    confirmed = await repository.find_registration(event, key)
    assert confirmed is not None
    assert confirmed.character_wish == "Без пожеланий"
    assert confirmed.attendance_status is AttendanceStatus.CONFIRMED
    assert disk_store.replace_count[event.disk_resource_path] == before_confirm + 1


@pytest.mark.asyncio
async def test_cancel_and_second_enlist_preserve_character_wish(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    key = "a" * 43
    await repository.enlist(
        event,
        operation_id="one",
        participant_key=key,
        display_name="Player",
        wish_play="A",
    )
    await repository.confirm(event, operation_id="two", participant_key=key, character_wish="Doctor")
    await repository.cancel(event, operation_id="three", participant_key=key)
    cancelled = await repository.find_registration(event, key)
    assert cancelled is not None
    assert cancelled.character_wish == "Doctor"
    assert cancelled.attendance_status is AttendanceStatus.CANCELLED
    await repository.enlist(
        event,
        operation_id="four",
        participant_key=key,
        display_name="Player",
        wish_play="New A",
    )
    restored = await repository.find_registration(event, key)
    assert restored is not None
    assert restored.character_wish == "Doctor"
    assert restored.attendance_status is AttendanceStatus.WAITING


@pytest.mark.asyncio
async def test_second_enlist_keeps_confirmed_status_and_character_wish(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    repository = await initialized(disk_store, event)
    key = "a" * 43
    await repository.enlist(
        event,
        operation_id="one",
        participant_key=key,
        display_name="Player",
        wish_play="A",
    )
    await repository.confirm(event, operation_id="two", participant_key=key, character_wish="Doctor")
    await repository.enlist(
        event,
        operation_id="three",
        participant_key=key,
        display_name="Player",
        wish_play="New A",
    )
    registration = await repository.find_registration(event, key)
    assert registration is not None
    assert registration.character_wish == "Doctor"
    assert registration.attendance_status is AttendanceStatus.CONFIRMED
    assert registration.wish_play == "New A"


@pytest.mark.asyncio
async def test_duplicate_operation_does_not_upload_twice(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    arguments = dict(
        operation_id="same",
        participant_key="a" * 43,
        display_name="Player",
        wish_play="A",
    )
    assert await repository.enlist(event, **arguments)
    assert not await repository.enlist(event, **arguments)
    assert disk_store.replace_count[event.disk_resource_path] == 1


@pytest.mark.asyncio
async def test_formula_injection_is_neutralized_and_round_trips(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    await repository.enlist(
        event,
        operation_id="one",
        participant_key="a" * 43,
        display_name='=HYPERLINK("bad")',
        wish_play="+SUM(1,2)",
    )
    registration = await repository.find_registration(event, "a" * 43)
    assert registration is not None
    assert registration.display_name.startswith("=")
    raw = disk_store.files[event.disk_resource_path]
    assert b"HYPERLINK" in raw or raw.startswith(b"PK")


@pytest.mark.asyncio
async def test_waiting_user_cannot_edit_before_confirmation(disk_store: MemoryDiskStore, event: Event) -> None:
    repository = await initialized(disk_store, event)
    key = "a" * 43
    await repository.enlist(
        event,
        operation_id="one",
        participant_key=key,
        display_name="Player",
        wish_play="A",
    )
    with pytest.raises(OperationNotAllowed):
        await repository.update_character_wish(event, operation_id="two", participant_key=key, character_wish="Doctor")
