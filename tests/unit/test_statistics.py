from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import PatternFill

from larp_bot.adapters.google_drive.statistics import (
    ATTENDANCE_FILE_NAME,
    MASTER_STATISTICS_FILE_NAME,
    XLSX_MIME_TYPE,
    DriveFile,
    GoogleDriveRestClient,
    GoogleDriveStatisticsRepository,
)
from larp_bot.adapters.google_drive.statistics_workbook import MasterStatisticsWorkbook, StatisticsWorkbook
from larp_bot.adapters.memory import MemoryDeferredTransport
from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.application.conversation import (
    ADMIN,
    BACK,
    MANAGE_STATISTICS,
    PAGE_BACK,
    PAGE_FORWARD,
    PAGE_SIZE,
    STATS_ADD_GAME,
    STATS_LINKS,
)
from larp_bot.application.services import DomainError, OrderedMutationService
from larp_bot.application.statistics import StatisticsService, current_season_year
from larp_bot.application.worker import OrderedWorker
from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    Button,
    Event,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    ProfileSyncPayload,
    Registration,
    StatisticsPayload,
    TelegramUser,
    VkUser,
)
from tests.conftest import MemoryDiskStore
from tests.unit.test_conversation import engine_setup, inbound
from tests.unit.test_worker import FakeConsumer


def template() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "2025-2026"
    sheet.append(
        ["Игрок", "Игр на текущий момент", "Игр на 01.09.2025", *[f"{n} игр" for n in range(5, 51, 5)], "Старая игра"]
    )
    sheet.append(["Иванов Пётр", "=C2+SUM(N2:N2)", 4, True, *([False] * 9), 1])
    sheet.append(["Петрова Анна", "=C3+SUM(N3:N3)", 0, *([False] * 10), "❌"])
    sheet.append(["Всего", '=COUNTIF(B2:B3,">0")'])
    sheet.append(["Участники сезона", "=SUMPRODUCT(--(B2:B3>C2:C3))"])
    sheet.append(["Новички", "=B4-C4"])
    sheet.append(["Активные игроки сезона", "=SUMPRODUCT(--(B2:B3>C2:C3+3))"])
    sheet["A2"].fill = PatternFill("solid", fgColor="D9D2E9")
    sheet.conditional_formatting.add(
        "N2:N3", CellIsRule(operator="equal", formula=["1"], fill=PatternFill("solid", fgColor="D9EAD3"))
    )
    sheet.conditional_formatting.add(
        "A2:A3", FormulaRule(formula=["AND($B2>=5,$D2=FALSE)"], fill=PatternFill("solid", fgColor="EAD1DC"))
    )
    historical = workbook.create_sheet("2024-2025")
    historical["A1"] = "Do not rewrite historical formula caches"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def master_template() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "2025-2026"
    sheet.append(
        [
            "Мастер",
            "Участие в сезоне",
            "Игр на начало сезона",
            "Игр на текущий момент",
            "1 игра",
            *[f"{n} игр" for n in range(5, 36, 5)],
            "Старая игра",
        ]
    )
    sheet.append(["Иванов Иван", "=D2-C2", 4, "=C2+SUM(M2:M2)", True, *([False] * 7), 1])
    sheet.append(["Петрова Анна", "=D3-C3", 0, "=C3+SUM(M3:M3)", False, *([False] * 7), None])
    sheet.append(["Всего", '=COUNTIF(B2:B3,">=0")', *([None] * 10), "=SUM(M2:M3)"])
    sheet.append(["Участники сезона", '=COUNTIF(B2:B3,">0")'])
    sheet.append(["Новички", '=COUNTIF(C2:C3,"=0")'])
    sheet["A2"].fill = PatternFill("solid", fgColor="FFDBF6")
    sheet["B2"].fill = PatternFill("solid", fgColor="FFF2CC")
    sheet["A4"].fill = PatternFill("solid", fgColor="FFDBF6")
    sheet["B4"].fill = PatternFill("solid", fgColor="FFDBF6")
    sheet["M4"].fill = PatternFill("solid", fgColor="FFDBF6")
    sheet.conditional_formatting.add(
        "M2:M3", CellIsRule(operator="equal", formula=["1"], fill=PatternFill("solid", fgColor="D9EAD3"))
    )
    sheet.conditional_formatting.add(
        "A2:A3",
        FormulaRule(formula=["AND($D2>=1,$E2=FALSE)"], fill=PatternFill("solid", fgColor="EAD1DC")),
    )
    historical = workbook.create_sheet("2024-2025")
    historical["A1"] = "Do not rewrite historical master formula caches"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def sheet(content: bytes, *, formulas: bool = False):
    return load_workbook(BytesIO(content), data_only=not formulas).worksheets[0]


def test_rollover_retains_totals_awards_styles_and_historical_xml() -> None:
    original = template()
    result = StatisticsWorkbook(original).new_season(2026)
    assert result is not None
    current = sheet(result)
    assert current.title == "2026-2027"
    assert current.max_column == 13
    assert current["C1"].value == "Игр на 01.09.2026"
    assert current["B2"].value == current["C2"].value == 5
    assert current["D2"].value is True
    assert current["A2"].fill.fgColor.rgb == "00D9D2E9"
    assert sheet(result, formulas=True)["B2"].value == "=C2"
    assert StatisticsWorkbook(result).new_season(2026) is None
    with ZipFile(BytesIO(original)) as before, ZipFile(BytesIO(result)) as after:
        for path in before.namelist():
            if path.startswith("xl/worksheets/") or path == "xl/styles.xml":
                assert before.read(path) == after.read(path)


def test_game_matches_names_appends_before_summary_and_expands_formulas_and_colours() -> None:
    content = StatisticsWorkbook(template()).new_season(2026)
    assert content is not None
    result = StatisticsWorkbook(content).mark_game(
        "=Безопасное название", ["  ИВАНОВ  Петр ", "Новый Игрок", "Новый Игрок"]
    )
    assert result is not None
    current = sheet(result)
    assert current["N1"].value == "=Безопасное название"
    assert sheet(result, formulas=True)["N1"].data_type == "s"
    assert current["N2"].value == current["N4"].value == 1
    assert current["N3"].value is None
    assert current["B2"].value == 6
    assert current["A4"].value == "Новый Игрок"
    assert current["C4"].value == 0
    assert current["D4"].value is False
    assert current["A5"].value == "Всего"
    assert current["N5"].value == 2
    assert current["B6"].value == 2
    assert current["B7"].value == 1
    assert any(str(cf.sqref) == "N2:N4" for cf in current.conditional_formatting)
    assert any(str(cf.sqref) == "A2:A4" for cf in current.conditional_formatting)
    assert StatisticsWorkbook(result).mark_game("=Безопасное название", ["Другой Игрок"]) is None
    for index in range(32):
        result = StatisticsWorkbook(result).mark_game(f"Игра {index}", ["Новый Игрок"])
        assert result is not None
    assert sheet(result)["B4"].value == 33


def test_player_game_matches_a_parseable_table_name_with_name_and_surname_reversed() -> None:
    workbook = load_workbook(BytesIO(template()))
    workbook.active["A2"] = "  Пётр   Иванов  "
    output = BytesIO()
    workbook.save(output)

    statistics = StatisticsWorkbook(output.getvalue())
    assert statistics.has_any_player(["ИВАНОВ Петр"])
    result = statistics.mark_game("Новая игра", ["Иванов Петр"])

    assert result is not None
    current = sheet(result)
    assert current["O2"].value == 1
    assert current["O3"].value is None
    assert current["A4"].value == "Всего"


def test_player_game_prefers_an_exact_match_over_a_reversed_name_match() -> None:
    workbook = load_workbook(BytesIO(template()))
    workbook.active["A2"] = "Пётр Иванов"
    workbook.active["A3"] = "Иванов Пётр"
    output = BytesIO()
    workbook.save(output)

    result = StatisticsWorkbook(output.getvalue()).mark_game("Новая игра", ["Иванов Петр"])

    assert result is not None
    current = sheet(result)
    assert current["O2"].value is None
    assert current["O3"].value == 1


def test_player_game_does_not_reverse_a_table_name_with_more_than_two_parts() -> None:
    workbook = load_workbook(BytesIO(template()))
    workbook.active["A2"] = "Пётр Иванов Иванович"
    output = BytesIO()
    workbook.save(output)

    result = StatisticsWorkbook(output.getvalue()).mark_game("Новая игра", ["Иванов Петр"])

    assert result is not None
    current = sheet(result)
    assert current["O2"].value is None
    assert current["A4"].value == "Иванов Петр"
    assert current["O4"].value == 1


def test_add_game_automatically_rolls_over_and_is_idempotent_within_current_season() -> None:
    result = StatisticsWorkbook(template()).add_game(2026, "Осенняя игра", ["Новый Игрок"])
    assert result is not None
    current = sheet(result)
    assert current.title == "2026-2027"
    assert current["N1"].value == "Осенняя игра"
    assert current["N4"].value == 1
    assert StatisticsWorkbook(result).add_game(2026, "Осенняя игра", ["Другой Игрок"]) is None


def test_master_game_rollover_preserves_totals_formulas_styles_and_is_idempotent() -> None:
    original = master_template()
    result = MasterStatisticsWorkbook(original).add_game(
        2026,
        "=Осенняя игра",
        ["  ИВАНОВ  Иван ", "Новый Мастер", "Новый Мастер"],
    )
    assert result is not None
    current = sheet(result)
    formulas = sheet(result, formulas=True)
    assert current.title == "2026-2027"
    assert current["M1"].value == "=Осенняя игра"
    assert formulas["M1"].data_type == "s"
    assert current["B2"].value == 1
    assert current["C2"].value == 5
    assert current["D2"].value == 6
    assert formulas["B2"].value == "=D2-C2"
    assert formulas["D2"].value == "=C2+SUM(M2:M2)"
    assert current["A4"].value == "Новый Мастер"
    assert current["B4"].value == 1
    assert current["C4"].value == 0
    assert current["D4"].value == current["M4"].value == 1
    assert current["E4"].value is False
    assert current["A5"].value == "Всего"
    assert current["M5"].value == 2
    assert current["B6"].value == 2
    assert current["B7"].value == 2
    assert current["A4"].fill.fgColor.rgb == current["A2"].fill.fgColor.rgb
    assert current["B5"].fill.fgColor.rgb == "00FFDBF6"
    assert any(str(cf.sqref) == "M2:M4" for cf in current.conditional_formatting)
    assert any(str(cf.sqref) == "A2:A4" for cf in current.conditional_formatting)
    assert MasterStatisticsWorkbook(result).add_game(2026, "=Осенняя игра", ["Другой Мастер"]) is None
    with ZipFile(BytesIO(original)) as before, ZipFile(BytesIO(result)) as after:
        for path in before.namelist():
            if path.startswith("xl/worksheets/") or path == "xl/styles.xml":
                assert before.read(path) == after.read(path)


def test_master_game_appends_to_current_season_and_expands_real_formula_ranges() -> None:
    result = MasterStatisticsWorkbook(master_template()).mark_game(
        "Новая игра",
        ["Иванов Иван", "Новый Мастер"],
    )
    assert result is not None
    current = sheet(result)
    formulas = sheet(result, formulas=True)
    assert current.title == "2025-2026"
    assert current["N1"].value == "Новая игра"
    assert current["N2"].value == current["N4"].value == 1
    assert current["D2"].value == 6
    assert formulas["D2"].value == "=C2+SUM(M2:N2)"
    assert current["A5"].value == "Всего"
    assert current["N5"].value == 2
    assert any(str(cf.sqref) == "M2:N4" for cf in current.conditional_formatting)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2026, 8, 31, 20, 59, tzinfo=UTC), 2025),
        (datetime(2026, 8, 31, 21, 0, tzinfo=UTC), 2026),
        (datetime(2027, 8, 31, 20, 59, tzinfo=UTC), 2026),
    ],
)
def test_current_season_uses_moscow_september_boundary(instant: datetime, expected: int) -> None:
    assert current_season_year(instant) == expected


class MemoryGoogleDrive:
    def __init__(self, content: bytes, master_content: bytes | None = None) -> None:
        contents = {ATTENDANCE_FILE_NAME: content}
        if master_content is not None:
            contents[MASTER_STATISTICS_FILE_NAME] = master_content
        self.files = {
            name: DriveFile(
                id=f"permanent-{index}-id",
                name=name,
                mimeType=XLSX_MIME_TYPE,
                webViewLink=f"https://drive.google.com/file/d/permanent-{index}-id/view",
            )
            for index, name in enumerate(contents, start=1)
        }
        self.contents = {self.files[name].file_id: value for name, value in contents.items()}
        self.lookups: list[tuple[str, str]] = []
        self.replacements: list[tuple[str, bytes]] = []

    @property
    def file(self) -> DriveFile:
        return self.files[ATTENDANCE_FILE_NAME]

    @property
    def content(self) -> bytes:
        return self.contents[self.file.file_id]

    async def find_file(self, folder_id: str, name: str) -> DriveFile:
        self.lookups.append((folder_id, name))
        return self.files[name]

    async def download(self, file_id: str) -> bytes:
        return self.contents[file_id]

    async def replace(self, file_id: str, content: bytes) -> None:
        self.contents[file_id] = content
        self.replacements.append((file_id, content))


class FakeResponse:
    def __init__(self, *, payload: object = None, content: bytes = b"") -> None:
        self.payload = payload
        self.content = content

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        pass


class FakeDriveSession:
    def __init__(self, files: list[dict[str, str]]) -> None:
        self.files = files
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if url.endswith("/files"):
            return FakeResponse(payload={"files": self.files})
        if method == "GET":
            return FakeResponse(content=b"workbook")
        return FakeResponse(payload={})


async def test_drive_client_finds_downloads_and_updates_the_same_permanent_file() -> None:
    session = FakeDriveSession(
        [
            {
                "id": "stable/id",
                "name": ATTENDANCE_FILE_NAME,
                "mimeType": XLSX_MIME_TYPE,
                "webViewLink": "https://drive.example/stable",
            }
        ]
    )
    client = GoogleDriveRestClient("unused for injected session", session)
    file = await client.find_file("folder'id", ATTENDANCE_FILE_NAME)
    assert file.file_id == "stable/id"
    assert "'folder\\'id' in parents" in session.calls[0][2]["params"]["q"]
    assert await client.download(file.file_id) == b"workbook"
    await client.replace(file.file_id, b"revised")
    assert session.calls[1][0:2] == ("GET", "https://www.googleapis.com/drive/v3/files/stable%2Fid")
    assert session.calls[2][0:2] == ("PATCH", "https://www.googleapis.com/upload/drive/v3/files/stable%2Fid")
    assert session.calls[2][2]["params"]["uploadType"] == "media"
    assert session.calls[2][2]["data"] == b"revised"
    assert not any(call[0] in {"POST", "DELETE"} for call in session.calls)


async def test_drive_client_requires_exactly_one_xlsx_with_the_fixed_name() -> None:
    missing = GoogleDriveRestClient("unused", FakeDriveSession([]))
    with pytest.raises(DomainError, match="не найден"):
        await missing.find_file("folder", ATTENDANCE_FILE_NAME)
    duplicate = {
        "id": "one",
        "name": ATTENDANCE_FILE_NAME,
        "mimeType": XLSX_MIME_TYPE,
        "webViewLink": "https://drive.example/one",
    }
    repeated = GoogleDriveRestClient("unused", FakeDriveSession([duplicate, duplicate | {"id": "two"}]))
    with pytest.raises(DomainError, match="несколько"):
        await repeated.find_file("folder", ATTENDANCE_FILE_NAME)
    wrong_type = GoogleDriveRestClient(
        "unused", FakeDriveSession([duplicate | {"mimeType": "application/vnd.google-apps.spreadsheet"}])
    )
    with pytest.raises(DomainError, match="XLSX"):
        await wrong_type.find_file("folder", ATTENDANCE_FILE_NAME)


def test_invalid_service_account_json_is_rejected_without_exposing_its_contents() -> None:
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        GoogleDriveRestClient("not-json")


async def test_repository_keeps_the_file_id_name_and_link_while_updating_content() -> None:
    drive = MemoryGoogleDrive(template())
    repository = GoogleDriveStatisticsRepository(drive, "stats-folder")
    original_link = await repository.link()
    assert await repository.edit(lambda book: book.add_game(2026, "Новая игра", ["Новый Игрок"]))
    assert await repository.link() == original_link
    assert drive.file.name == ATTENDANCE_FILE_NAME
    assert [file_id for file_id, _ in drive.replacements] == ["permanent-1-id"]
    assert drive.lookups == [
        ("stats-folder", ATTENDANCE_FILE_NAME),
        ("stats-folder", ATTENDANCE_FILE_NAME),
        ("stats-folder", ATTENDANCE_FILE_NAME),
    ]
    assert not await repository.edit(lambda book: book.add_game(2026, "Новая игра", ["Другой Игрок"]))
    assert len(drive.replacements) == 1


async def test_repository_finds_existing_player_with_normalized_name() -> None:
    repository = GoogleDriveStatisticsRepository(MemoryGoogleDrive(template()), "stats-folder")

    assert await repository.has_any_player({"  ИВАНОВ  Петр "})
    assert not await repository.has_any_player({"Новый Игрок"})


async def test_profile_sync_derives_experience_and_fans_out_projection_updates(event: Event) -> None:
    store = MemoryDiskStore()
    engine, users, publisher, registrations = await engine_setup(store, event)
    user = TelegramUser(
        tg_id=2,
        full_name="Текущее Имя",
        vk_url="https://vk.com/id2",
        crossplay=False,
        needs_pass=False,
    )
    await users.save(user)
    key = engine.registrations.key(Platform.TELEGRAM, user.tg_id, event.event_id)
    await registrations.enlist(
        event.event_id,
        operation_id="existing-enlist",
        participant_key=key,
        display_name=user.full_name,
        wish_play="Без пожеланий",
    )
    drive = MemoryGoogleDrive(template(), master_template())
    statistics = StatisticsService(
        GoogleDriveStatisticsRepository(drive, "stats-folder"),
        GoogleDriveStatisticsRepository.masters(drive, "stats-folder"),
        engine.admins,
        engine.administration,
        engine.registrations,
    )
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id="profile:telegram:2",
        operation=Operation.SYNC_PROFILE,
        platform=Platform.TELEGRAM,
        platform_user_id=user.tg_id,
        payload=ProfileSyncPayload(names=["Иванов Пётр", user.full_name]),
    )
    transport = MemoryDeferredTransport()
    consumer = FakeConsumer([QueueEnvelope(command, "profile-sync")])
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(engine.events, engine.administration.catalog),
        users,
        transport,
        statistics=statistics,
    )

    assert await worker.run() == 1

    synced = await users.get(Platform.TELEGRAM, user.tg_id)
    assert synced is not None and synced.larp_experience is True
    assert consumer.deleted == ["profile-sync"]
    assert transport.sent == []
    assert [queued.operation for queued in publisher.commands] == [Operation.UPDATE_PROFILE]
    assert publisher.commands[0].event_id == event.event_id


async def test_statistics_admin_menu_picker_and_global_fifo_only_show_active_games(event: Event) -> None:
    archived = event.model_copy(
        update={
            "event_id": "archived-event",
            "name": "Архивная игра",
            "archived_at": datetime(2026, 8, 1, tzinfo=UTC),
        }
    )
    engine, _users, publisher, _registrations = await engine_setup(MemoryDiskStore(), event, archived, admin=True)
    menu = await engine.handle(inbound(1, ADMIN))
    assert MANAGE_STATISTICS in {button.value for button in menu.buttons}
    assert not {
        "📊 Выбрать таблицу статистики",
        "🗓 Начать новый сезон",
        "🔗 Ссылка на статистику",
    } & {button.value for button in menu.buttons}

    statistics_menu = await engine.handle(inbound(2, MANAGE_STATISTICS))
    assert [button.value for button in statistics_menu.buttons[:2]] == [STATS_LINKS, STATS_ADD_GAME]
    picker = await engine.handle(inbound(3, STATS_ADD_GAME))
    assert f"stats:game:{event.event_id}" in {button.value for button in picker.buttons}
    assert "stats:game:archived-event" not in {button.value for button in picker.buttons}
    await engine.handle(inbound(4, f"stats:game:{event.event_id}"))
    await engine.handle(inbound(5, STATS_LINKS))
    assert [command.payload.action for command in publisher.commands] == ["game", "link"]
    assert all(
        command.event_id == "statistics" and command.operation is Operation.STATISTICS for command in publisher.commands
    )

    game_menu = await engine.handle(inbound(6, f"ag:manage:{event.event_id}"))
    add_from_game = next(button for button in game_menu.buttons if button.label == STATS_ADD_GAME)
    assert add_from_game.value == f"stats:game:{event.event_id}"
    response = await engine.handle(inbound(7, add_from_game.value, callback=True))
    assert response.deferred is True
    assert response.buttons == [Button(label=BACK, value=MANAGE_STATISTICS)]
    assert [command.payload.action for command in publisher.commands] == ["game", "link", "game"]
    assert publisher.commands[-1].payload.game_id == event.event_id


async def test_statistics_game_picker_uses_shared_bidirectional_pagination(event: Event) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        event.model_copy(
            update={
                "event_id": f"event-{index:04d}",
                "name": f"Game {index}",
                "created_at": base.replace(second=index),
                "updated_at": base.replace(second=index),
            }
        )
        for index in range(PAGE_SIZE + 1)
    ]
    engine, _users, _publisher, _registrations = await engine_setup(
        MemoryDiskStore(),
        *events,
        admin=True,
    )

    first = await engine.handle(inbound(1, STATS_ADD_GAME))
    assert len([button for button in first.buttons if button.value.startswith("stats:game:")]) == PAGE_SIZE
    assert PAGE_BACK not in {button.label for button in first.buttons}

    second = await engine.handle(
        inbound(2, next(button.value for button in first.buttons if button.label == PAGE_FORWARD), callback=True)
    )
    assert len([button for button in second.buttons if button.value.startswith("stats:game:")]) == 1
    assert PAGE_BACK in {button.label for button in second.buttons}
    assert PAGE_FORWARD not in {button.label for button in second.buttons}
    assert second.buttons[-1] == Button(label="Отмена", value=MANAGE_STATISTICS)


async def test_statistics_actions_are_denied_to_moderators_even_with_forged_callbacks(event: Event) -> None:
    engine, _users, publisher, _registrations = await engine_setup(MemoryDiskStore(), event, gamemaster=True)
    menu = await engine.handle(inbound(1, ADMIN))
    assert MANAGE_STATISTICS not in {button.value for button in menu.buttons}
    for index, action in enumerate(
        (MANAGE_STATISTICS, STATS_LINKS, STATS_ADD_GAME, f"stats:game:{event.event_id}"), start=2
    ):
        response = await engine.handle(inbound(index, action))
        assert "прав" in response.text
    await engine.events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=1),
    )
    game_menu = await engine.handle(inbound(6, f"ag:manage:{event.event_id}"))
    assert STATS_ADD_GAME not in {button.label for button in game_menu.buttons}
    assert not publisher.commands


async def test_game_addition_uses_confirmed_cyrillic_names_and_is_idempotent(event: Event) -> None:
    event.game_date = date(2026, 11, 14)
    engine, users, publisher, registrations = await engine_setup(MemoryDiskStore(), event, admin=True)
    admin = await users.get(Platform.TELEGRAM, 1)
    assert admin is not None
    admin.larp_experience = False
    await users.save(admin)
    await users.save(
        VkUser(
            vk_id=7,
            full_name="Второй Игрок Игрокович",
            crossplay=False,
            larp_experience=False,
            needs_pass=False,
        )
    )
    await engine.events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.TELEGRAM, platform_user_id=1),
    )
    await engine.events.add_leader(
        event.event_id,
        BotIdentity(platform=Platform.VK, platform_user_id=7),
    )
    for status, name, key in (
        (
            AttendanceStatus.CONFIRMED,
            "Новый Игрок",
            engine.registrations.key(Platform.TELEGRAM, 1, event.event_id),
        ),
        (
            AttendanceStatus.CONFIRMED,
            "Второй Игрок Игрокович",
            engine.registrations.key(Platform.VK, 7, event.event_id),
        ),
        (AttendanceStatus.WAITING, "Ждущий Игрок", uuid4().hex * 2),
        (AttendanceStatus.CANCELLED, "Бывший Игрок", uuid4().hex * 2),
    ):
        await registrations.import_missing(
            [
                Registration(
                    event_id=event.event_id,
                    participant_key=key,
                    display_name=name,
                    attendance_status=status,
                    wish_play="Без пожеланий",
                )
            ]
        )
    drive = MemoryGoogleDrive(template(), master_template())
    service = StatisticsService(
        GoogleDriveStatisticsRepository(drive, "stats-folder"),
        GoogleDriveStatisticsRepository.masters(drive, "stats-folder"),
        engine.admins,
        engine.administration,
        engine.registrations,
        season_year=lambda: 2026,
    )
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id="statistics",
        operation=Operation.STATISTICS,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        payload=StatisticsPayload(action="game", game_id=event.event_id),
    )
    assert "2026-2027" in await service.apply(command)
    current = sheet(drive.content)
    assert current["A4"].value == "Новый Игрок"
    assert current["A5"].value == "Второй Игрок"
    assert current["A6"].value == "Всего"
    assert current["N4"].value == 1
    assert current["N5"].value == 1
    assert current["N1"].value == "Лесной предел (14.11.26)"
    master_file = drive.files[MASTER_STATISTICS_FILE_NAME]
    master_sheet = sheet(drive.contents[master_file.file_id])
    assert master_sheet["A4"].value == "Иван Иванов"
    assert master_sheet["A5"].value == "Второй Игрок"
    assert master_sheet["M4"].value == 1
    assert master_sheet["M5"].value == 1
    assert master_sheet["M1"].value == "Лесной предел (14.11.26)"
    experienced_admin = await users.get(Platform.TELEGRAM, 1)
    experienced_vk = await users.get(Platform.VK, 7)
    assert experienced_admin is not None and experienced_admin.larp_experience is True
    assert experienced_vk is not None and experienced_vk.larp_experience is True
    profile_updates = [command for command in publisher.commands if command.operation is Operation.UPDATE_PROFILE]
    assert [command.event_id for command in profile_updates] == [event.event_id, event.event_id]
    retry = command.model_copy(update={"operation_id": str(uuid4())})
    assert "уже есть" in await service.apply(retry)
    assert len(drive.replacements) == 2
    await engine.events.archive(event.event_id, datetime(2026, 9, 2, tzinfo=UTC))
    with pytest.raises(DomainError, match="архивирована"):
        await service.apply(retry.model_copy(update={"operation_id": str(uuid4())}))


async def test_statistics_stages_both_workbooks_before_replacing_either(event: Event) -> None:
    engine, _users, _publisher, _registrations = await engine_setup(MemoryDiskStore(), event, admin=True)
    drive = MemoryGoogleDrive(template(), b"not a workbook")
    service = StatisticsService(
        GoogleDriveStatisticsRepository(drive, "stats-folder"),
        GoogleDriveStatisticsRepository.masters(drive, "stats-folder"),
        engine.admins,
        engine.administration,
        engine.registrations,
        season_year=lambda: 2026,
    )
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id="statistics",
        operation=Operation.STATISTICS,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        payload=StatisticsPayload(action="game", game_id=event.event_id),
    )

    with pytest.raises(DomainError, match="таблица мастеров"):
        await service.apply(command)

    assert not drive.replacements


async def test_statistics_worker_delivers_permanent_link_and_rechecks_admin(event: Event) -> None:
    engine, users, publisher, _registrations = await engine_setup(MemoryDiskStore(), event, admin=True)
    await engine.handle(inbound(1, STATS_LINKS))
    drive = MemoryGoogleDrive(template(), master_template())
    service = StatisticsService(
        GoogleDriveStatisticsRepository(drive, "stats-folder"),
        GoogleDriveStatisticsRepository.masters(drive, "stats-folder"),
        engine.admins,
        engine.administration,
        engine.registrations,
    )
    command = publisher.commands[0]
    transport = MemoryDeferredTransport()
    consumer = FakeConsumer([QueueEnvelope(command, "receipt")])
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(engine.events, engine.administration.catalog),
        users,
        transport,
        statistics=service,
    )
    assert await worker.run() == 1
    assert consumer.deleted == ["receipt"]
    assert drive.file.web_view_link in transport.sent[0][3]
    assert drive.files[MASTER_STATISTICS_FILE_NAME].web_view_link in transport.sent[0][3]
    assert "Посещаемость игроков" in transport.sent[0][3]
    assert "Участие мастеров" in transport.sent[0][3]
    unauthorized = command.model_copy(update={"platform_user_id": 999})
    with pytest.raises(DomainError, match="прав"):
        await service.apply(unauthorized)
    assert not drive.replacements


def test_supplied_workbook_when_available() -> None:
    path = Path(__file__).resolve().parents[2] / "Копия Сводная статистика посещений.xlsx"
    if not path.exists():
        pytest.skip("Private sample workbook is not committed to the repository")
    original = path.read_bytes()
    result = StatisticsWorkbook(original).add_game(2026, "Проверка", ["Новый Игрок", "Абдулазизова Софья"])
    assert result is not None
    assert sheet(result)["B4"].value == 5
    with ZipFile(BytesIO(original)) as before, ZipFile(BytesIO(result)) as after:
        for name in before.namelist():
            if name.startswith("xl/worksheets/"):
                assert before.read(name) == after.read(name)


def test_supplied_master_workbook_when_available() -> None:
    path = Path(__file__).resolve().parents[2] / MASTER_STATISTICS_FILE_NAME
    if not path.exists():
        pytest.skip("Private master statistics workbook is not committed to the repository")
    original = path.read_bytes()
    result = MasterStatisticsWorkbook(original).add_game(2026, "Проверка", ["Новый Мастер", "Лука Филин"])
    assert result is not None
    current = sheet(result)
    assert current.title == "2026-2027"
    assert current["M1"].value == "Проверка"
    assert current["M102"].value == 1
    assert current["A103"].value == "Новый Мастер"
    assert current["M103"].value == 1
    assert current["A104"].value == "Всего"
    assert current["M104"].value == 2
    with ZipFile(BytesIO(original)) as before, ZipFile(BytesIO(result)) as after:
        for name in before.namelist():
            if name.startswith("xl/worksheets/") or name == "xl/styles.xml":
                assert before.read(name) == after.read(name)


def test_invalid_workbook_and_unrecognized_footer_are_rejected() -> None:
    with pytest.raises(DomainError):
        StatisticsWorkbook(b"not a workbook")
    book = load_workbook(BytesIO(template()))
    book.active["A10"] = "Additional data that must not be lost"
    output = BytesIO()
    book.save(output)
    with pytest.raises(DomainError):
        StatisticsWorkbook(output.getvalue())
