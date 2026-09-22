from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx
import pytest
from openpyxl import Workbook, load_workbook

from larp_bot.adapters.memory import MemoryEventRepository, MemoryRegistrationRepository
from larp_bot.adapters.yandex_disk.repository import (
    LEGACY_HEADERS,
    PPTX_MIME_TYPE,
    PUBLIC_HEADERS,
    STATEFUL_HEADERS,
    TABLEAU_10_COLORS,
    VISIBLE_HEADERS,
    YandexDiskRestClient,
    YandexDiskShowcaseRepository,
    public_showcase_workbook_bytes,
    showcase_workbook_bytes,
)
from larp_bot.application.services import RegistrationCatalog
from larp_bot.domain.models import AttendanceStatus, Event, Registration
from tests.conftest import MemoryDiskStore


async def initialized(store: MemoryDiskStore, event: Event) -> YandexDiskShowcaseRepository:
    repository = YandexDiskShowcaseRepository(store)
    public_url = await repository.create_event_workbook(event.disk_resource_path)
    assert public_url == "https://disk.example/public/1"
    return repository


@pytest.mark.asyncio
async def test_new_workbook_is_a_visible_only_showcase(disk_store: MemoryDiskStore, event: Event) -> None:
    await initialized(disk_store, event)
    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        headers = tuple(cell.value for cell in workbook.active[1])
        assert headers == VISIBLE_HEADERS
        assert headers[2:4] == ("Профиль ВКонтакте", "Профиль в Telegram")
        assert workbook.active.max_column == 9
        assert not any(
            workbook.active.column_dimensions[column].hidden for column in ("A", "B", "C", "D", "E", "F", "G", "H", "I")
        )
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_showcase_is_regenerated_from_registration_rows(disk_store: MemoryDiskStore, event: Event) -> None:
    showcase = await initialized(disk_store, event)
    registration = Registration(
        event_id=event.event_id,
        participant_key="a" * 43,
        display_name='=HYPERLINK("bad")',
        vk_profile="https://vk.com/player",
        telegram_profile="https://t.me/player",
        wish_play="+SUM(1,2)",
        larp_experience=True,
        crossplay=False,
        character_wish="Doctor",
        attendance_status=AttendanceStatus.CONFIRMED,
    )

    await showcase.replace(event, [registration])

    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]), data_only=False)
    try:
        assert tuple(workbook.active.cell(2, column).value for column in range(1, 10)) == (
            1,
            '\'=HYPERLINK("bad")',
            "https://vk.com/player",
            "https://t.me/player",
            "Да",
            "Нет",
            "'+SUM(1,2)",
            "Doctor",
            AttendanceStatus.CONFIRMED.value,
        )
        assert workbook.active.max_column == 9
    finally:
        workbook.close()


def test_confirmation_callback_payload_is_omitted_from_character_wish_cells() -> None:
    registration = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Иванов Иван",
        wish_play="Без пожеланий",
        character_wish="unexpected SELECT:CONFIRM:b49df060-85f7-45fd-851a-2e3dcf706320 callback",
    )

    for content, character_wish_column in (
        (showcase_workbook_bytes([registration]), 8),
        (public_showcase_workbook_bytes([registration]), 5),
    ):
        workbook = load_workbook(BytesIO(content))
        try:
            assert workbook.active.cell(2, character_wish_column).value is None
        finally:
            workbook.close()


@pytest.mark.asyncio
async def test_public_showcase_omits_all_contact_columns_and_values(disk_store: MemoryDiskStore, event: Event) -> None:
    public_path = "disk:/larp-bot/events/event-a1/public_table_Лесной предел.xlsx"
    event = event.model_copy(
        update={
            "public_table_resource_path": public_path,
            "public_table_public_url": "https://disk.example/public/2",
        }
    )
    showcase = await initialized(disk_store, event)
    await showcase.create_public_event_workbook(public_path)
    registration = Registration(
        event_id=event.event_id,
        participant_key="a" * 43,
        display_name="Иванов Иван Иванович",
        nickname="Ворон",
        vk_profile="https://vk.com/private-player",
        telegram_profile="https://t.me/private-player",
        wish_play="Anyone",
        larp_experience=True,
        crossplay=False,
        character_wish="Doctor",
        attendance_status=AttendanceStatus.CONFIRMED,
    )

    await showcase.replace(event, [registration])

    workbook = load_workbook(BytesIO(disk_store.files[public_path]), data_only=False)
    try:
        values = tuple(cell.value for row in workbook.active.iter_rows() for cell in row)
        assert tuple(cell.value for cell in workbook.active[1]) == PUBLIC_HEADERS
        assert tuple(workbook.active.cell(2, column).value for column in range(1, 7)) == (
            1,
            "Ворон",
            "Нет",
            "Anyone",
            "Doctor",
            AttendanceStatus.CONFIRMED.value,
        )
        assert workbook.active.max_column == 6
        assert not any("vk.com" in str(value) or "t.me" in str(value) for value in values)
    finally:
        workbook.close()

    master = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]), data_only=False)
    try:
        assert master.active["B2"].value == "Иванов Иван"
    finally:
        master.close()


def test_public_showcase_falls_back_to_surname_and_name_without_nickname() -> None:
    registration = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Петров Пётр Петрович",
        wish_play="Без пожеланий",
    )

    workbook = load_workbook(BytesIO(public_showcase_workbook_bytes([registration])))
    try:
        assert workbook.active["B2"].value == "Петров Пётр"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_cancelled_registrations_are_omitted_from_showcase(disk_store: MemoryDiskStore, event: Event) -> None:
    showcase = await initialized(disk_store, event)
    registrations = [
        Registration(
            event_id=event.event_id,
            participant_key="a" * 43,
            display_name="Cancelled player",
            wish_play="Anyone",
            attendance_status=AttendanceStatus.CANCELLED,
        ),
        Registration(
            event_id=event.event_id,
            participant_key="b" * 43,
            display_name="Active player",
            wish_play="Anyone",
        ),
    ]

    await showcase.replace(event, registrations)

    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        rows = [tuple(row) for row in workbook.active.iter_rows(min_row=2, max_col=2, values_only=True)]
        assert rows[0] == (1, "Active player")
        assert all(name is None for _, name in rows[1:])
        assert workbook.active.cell(event.player_amount + 2, 1).value == "ЗАПАС"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_showcase_orders_rows_by_first_signup_even_when_input_is_arbitrary(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    showcase = await initialized(disk_store, event)
    first_signup = datetime(2026, 9, 1, 10, tzinfo=UTC)
    registrations = [
        Registration(
            event_id=event.event_id,
            participant_key="c" * 43,
            display_name="Newest",
            wish_play="Anyone",
            created_at=first_signup + timedelta(hours=2),
        ),
        Registration(
            event_id=event.event_id,
            participant_key="a" * 43,
            display_name="Oldest, updated",
            wish_play="Anyone",
            created_at=first_signup,
            updated_at=first_signup + timedelta(days=1),
        ),
        Registration(
            event_id=event.event_id,
            participant_key="b" * 43,
            display_name="Middle",
            wish_play="Anyone",
            created_at=first_signup + timedelta(hours=1),
        ),
    ]

    await showcase.replace(event, registrations)

    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        rows = [tuple(row) for row in workbook.active.iter_rows(min_row=2, max_col=2, values_only=True)]
        assert rows[:3] == [
            (1, "Oldest, updated"),
            (2, "Middle"),
            (3, "Newest"),
        ]
        assert all(name is None for _, name in rows[3:])
    finally:
        workbook.close()


def test_co_player_wishes_color_only_matching_player_names_in_both_showcases() -> None:
    registrations = [
        Registration(
            event_id="event-a1",
            participant_key="a" * 43,
            display_name="Иванов Иван Иванович",
            wish_play="Петров Пётр, Сидоров Семён",
        ),
        Registration(
            event_id="event-a1",
            participant_key="b" * 43,
            display_name="Петров Пётр Петрович",
            wish_play="Без пожеланий",
        ),
        Registration(
            event_id="event-a1",
            participant_key="c" * 43,
            display_name="Сидоров Семён Семёнович",
            wish_play="Без пожеланий",
        ),
        Registration(
            event_id="event-a1",
            participant_key="d" * 43,
            display_name="Орлов Олег Олегович",
            wish_play="фёдор фёд",
        ),
        Registration(
            event_id="event-a1",
            participant_key="e" * 43,
            display_name="Фёдоров Фёдор Фёдорович",
            wish_play="Без пожеланий",
        ),
    ]

    for content, column_count in (
        (showcase_workbook_bytes(registrations), len(VISIBLE_HEADERS)),
        (public_showcase_workbook_bytes(registrations), len(PUBLIC_HEADERS)),
    ):
        workbook = load_workbook(BytesIO(content))
        try:
            sheet = workbook.active
            for row in range(2, 5):
                assert sheet.cell(row, 2).fill.fill_type == "solid"
                assert sheet.cell(row, 2).fill.fgColor.rgb == f"FF{TABLEAU_10_COLORS[0]}"
                assert all(
                    sheet.cell(row, column).fill.fill_type is None
                    for column in range(1, column_count + 1)
                    if column != 2
                )
            for row in (5, 6):
                assert sheet.cell(row, 2).fill.fill_type == "solid"
                assert sheet.cell(row, 2).fill.fgColor.rgb == f"FF{TABLEAU_10_COLORS[1]}"
        finally:
            workbook.close()


def test_co_player_wishes_match_all_whitespace_tokens_against_name_or_nickname_substrings() -> None:
    registrations = [
        Registration(
            event_id="event-a1",
            participant_key="a" * 43,
            display_name="Ищущий Игрок",
            wish_play="кен eyep",
        ),
        Registration(
            event_id="event-a1",
            participant_key="b" * 43,
            display_name="Сасаки Хайсе",
            nickname="Канеки Кен/Eyepatch",
            wish_play="Без пожеланий",
        ),
        Registration(
            event_id="event-a1",
            participant_key="c" * 43,
            display_name="Иванов Пётр",
            nickname="Кен",
            wish_play="Без пожеланий",
        ),
    ]

    for content in (showcase_workbook_bytes(registrations), public_showcase_workbook_bytes(registrations)):
        workbook = load_workbook(BytesIO(content))
        try:
            sheet = workbook.active
            assert sheet["B2"].fill.fgColor.rgb == f"FF{TABLEAU_10_COLORS[0]}"
            assert sheet["B3"].fill.fgColor.rgb == f"FF{TABLEAU_10_COLORS[0]}"
            assert sheet["B4"].fill.fill_type is None
        finally:
            workbook.close()


def test_ten_largest_wish_components_receive_tableau_10_colors() -> None:
    registrations: list[Registration] = []
    pairs: list[tuple[str, str]] = []
    for index in range(10):
        first_name = f"Фамилия{index} Имя{index}А"
        second_name = f"Фамилия{index} Имя{index}Б"
        pairs.append((first_name, second_name))
        registrations.extend(
            (
                Registration(
                    event_id="event-a1",
                    participant_key=f"{index * 2:043d}",
                    display_name=first_name,
                    wish_play=second_name,
                ),
                Registration(
                    event_id="event-a1",
                    participant_key=f"{index * 2 + 1:043d}",
                    display_name=second_name,
                    wish_play="Без пожеланий",
                ),
            )
        )
    largest_names = ("Большой Один", "Большой Два", "Большой Три")
    registrations.extend(
        (
            Registration(
                event_id="event-a1",
                participant_key=f"{20:043d}",
                display_name=largest_names[0],
                wish_play=f"{largest_names[1]}, {largest_names[2]}",
            ),
            Registration(
                event_id="event-a1",
                participant_key=f"{21:043d}",
                display_name=largest_names[1],
                wish_play="Без пожеланий",
            ),
            Registration(
                event_id="event-a1",
                participant_key=f"{22:043d}",
                display_name=largest_names[2],
                wish_play="Без пожеланий",
            ),
        )
    )

    workbook = load_workbook(BytesIO(showcase_workbook_bytes(registrations)))
    try:
        sheet = workbook.active
        name_cells = {sheet.cell(row, 2).value: sheet.cell(row, 2) for row in range(2, sheet.max_row + 1)}
        for name in largest_names:
            assert name_cells[name].fill.fgColor.rgb == f"FF{TABLEAU_10_COLORS[0]}"
        for index, pair in enumerate(pairs[:9], start=1):
            for name in pair:
                assert name_cells[name].fill.fgColor.rgb == f"FF{TABLEAU_10_COLORS[index]}"
        for name in pairs[9]:
            assert name_cells[name].fill.fill_type is None
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_main_roster_slots_and_green_reserve_row_are_fixed_in_both_showcases(
    disk_store: MemoryDiskStore,
    event: Event,
) -> None:
    master_path = event.disk_resource_path
    public_path = "disk:/larp-bot/events/event-a1/public_table_Лесной предел.xlsx"
    event = event.model_copy(
        update={
            "player_amount": 2,
            "public_table_resource_path": public_path,
            "public_table_public_url": "https://disk.example/public/2",
        }
    )
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(master_path, player_amount=event.player_amount)
    await showcase.create_public_event_workbook(public_path, player_amount=event.player_amount)
    first_signup = datetime(2026, 9, 1, 10, tzinfo=UTC)
    registrations = [
        Registration(
            event_id=event.event_id,
            participant_key=character * 43,
            display_name=name,
            wish_play="Anyone",
            created_at=first_signup + timedelta(minutes=index),
        )
        for index, (character, name) in enumerate((("a", "First"), ("b", "Second"), ("c", "Reserve")))
    ]

    await showcase.replace(event, registrations)

    for path, column_count in ((master_path, len(VISIBLE_HEADERS)), (public_path, len(PUBLIC_HEADERS))):
        workbook = load_workbook(BytesIO(disk_store.files[path]))
        try:
            sheet = workbook.active
            assert [sheet.cell(row, 2).value for row in (2, 3, 5)] == ["First", "Second", "Reserve"]
            assert sheet.cell(4, 1).value == "ЗАПАС"
            assert [cell.value for cell in sheet[4] if cell.value is not None] == ["ЗАПАС"]
            assert str(next(iter(sheet.merged_cells.ranges))) == f"A4:{chr(64 + column_count)}4"
            assert sheet.cell(4, 1).fill.fill_type == "solid"
            assert sheet.cell(4, 1).fill.fgColor.rgb.endswith("00B050")
            assert sheet.cell(4, 1).alignment.horizontal == "center"
        finally:
            workbook.close()

    registrations[0].attendance_status = AttendanceStatus.CANCELLED
    await showcase.replace(event, registrations)

    for path in (master_path, public_path):
        workbook = load_workbook(BytesIO(disk_store.files[path]))
        try:
            sheet = workbook.active
            assert [sheet.cell(row, 2).value for row in (2, 3)] == ["Second", "Reserve"]
            assert sheet.cell(4, 1).value == "ЗАПАС"
            assert sheet.max_row == 4
        finally:
            workbook.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [STATEFUL_HEADERS, LEGACY_HEADERS])
async def test_legacy_state_is_imported_once_then_removed_from_xlsx(
    headers: tuple[str, ...], disk_store: MemoryDiskStore, event: Event
) -> None:
    event.registrations_migrated_at = None
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    if headers == STATEFUL_HEADERS:
        sheet.append(
            (
                1,
                "Иван",
                "Да",
                "Нет",
                "Алиса",
                "Doctor",
                "Подтверждено",
                "a" * 43,
                "old-op",
                "2026-08-21T00:00:00+00:00",
            )
        )
    else:
        sheet.append(("Иван", "Алиса", "Doctor", "Подтверждено", "a" * 43, "old-op", "2026-08-21T00:00:00+00:00"))
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    disk_store.files[event.disk_resource_path] = output.getvalue()

    events = MemoryEventRepository([event])
    registrations = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    catalog = RegistrationCatalog(events, registrations, showcase)
    migrated = await catalog.get(event, "a" * 43)

    assert migrated is not None
    assert migrated.character_wish == "Doctor"
    assert migrated.larp_experience is (True if headers == STATEFUL_HEADERS else None)
    stored_event = await events.get(event.event_id)
    assert stored_event is not None and stored_event.registrations_migrated_at is not None
    assert stored_event.public_table_resource_path is not None
    assert stored_event.public_table_public_url is not None
    rendered = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        assert tuple(cell.value for cell in rendered.active[1]) == VISIBLE_HEADERS
        assert rendered.active.max_column == 9
    finally:
        rendered.close()
    public_rendered = load_workbook(BytesIO(disk_store.files[stored_event.public_table_resource_path]))
    try:
        assert tuple(cell.value for cell in public_rendered.active[1]) == PUBLIC_HEADERS
        assert public_rendered.active.max_column == 6
    finally:
        public_rendered.close()


@pytest.mark.asyncio
async def test_optional_telegram_profile_is_rendered_as_blank(disk_store: MemoryDiskStore, event: Event) -> None:
    showcase = await initialized(disk_store, event)
    registration = Registration(
        event_id=event.event_id,
        participant_key="a" * 43,
        display_name="Player",
        vk_profile="https://vk.com/id1",
        telegram_profile=None,
        wish_play="Anyone",
    )

    await showcase.replace(event, [registration])

    workbook = load_workbook(BytesIO(disk_store.files[event.disk_resource_path]))
    try:
        assert workbook.active["C2"].value == "https://vk.com/id1"
        assert workbook.active["D2"].value is None
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_yandex_disk_download_follows_transfer_redirect() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "cloud-api.yandex.net":
            return httpx.Response(200, json={"href": "https://downloader.disk.yandex.ru/file"})
        if request.url.host == "downloader.disk.yandex.ru":
            return httpx.Response(302, headers={"Location": "https://storage.yandex.net/file"})
        return httpx.Response(200, content=b"workbook")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        disk = YandexDiskRestClient("token", client)
        assert await disk.download("disk:/larp-bot/events/event.xlsx") == b"workbook"

    assert [httpx.URL(url).host for url in requests] == [
        "cloud-api.yandex.net",
        "downloader.disk.yandex.ru",
        "storage.yandex.net",
    ]


@pytest.mark.asyncio
async def test_yandex_disk_upload_uses_presentation_content_type() -> None:
    content_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cloud-api.yandex.net":
            return httpx.Response(200, json={"href": "https://uploader.disk.yandex.net/file"})
        content_types.append(request.headers["Content-Type"])
        return httpx.Response(201)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        disk = YandexDiskRestClient("token", client)
        await disk._upload("disk:/larp-bot/achievements/event-a1.pptx", b"presentation", overwrite=False)

    assert content_types == [PPTX_MIME_TYPE]


@pytest.mark.asyncio
async def test_ydb_state_does_not_depend_on_showcase_contents(disk_store: MemoryDiskStore, event: Event) -> None:
    showcase = await initialized(disk_store, event)
    registration = Registration(
        event_id=event.event_id,
        participant_key="a" * 43,
        display_name="Player",
        wish_play="Anyone",
    )
    events = MemoryEventRepository([event])
    registrations = MemoryRegistrationRepository([registration])
    catalog = RegistrationCatalog(events, registrations, showcase)
    disk_store.files[event.disk_resource_path] = b"not an xlsx"

    found = await catalog.get(event, registration.participant_key)

    assert found == registration


def test_legacy_timestamp_is_timezone_aware() -> None:
    registration = Registration(
        event_id="event-a1",
        participant_key="a" * 43,
        display_name="Player",
        wish_play="Anyone",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert registration.created_at.tzinfo is UTC
