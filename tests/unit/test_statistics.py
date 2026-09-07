from __future__ import annotations

from datetime import UTC, datetime
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
    XLSX_MIME_TYPE,
    DriveFile,
    GoogleDriveRestClient,
    GoogleDriveStatisticsRepository,
)
from larp_bot.adapters.google_drive.statistics_workbook import StatisticsWorkbook
from larp_bot.adapters.memory import MemoryDeferredTransport
from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.application.conversation import (
    ADMIN,
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
    Button,
    Event,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    Registration,
    StatisticsPayload,
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


def test_add_game_automatically_rolls_over_and_is_idempotent_within_current_season() -> None:
    result = StatisticsWorkbook(template()).add_game(2026, "Осенняя игра", ["Новый Игрок"])
    assert result is not None
    current = sheet(result)
    assert current.title == "2026-2027"
    assert current["N1"].value == "Осенняя игра"
    assert current["N4"].value == 1
    assert StatisticsWorkbook(result).add_game(2026, "Осенняя игра", ["Другой Игрок"]) is None


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
    def __init__(self, content: bytes) -> None:
        self.file = DriveFile(
            id="permanent-file-id",
            name=ATTENDANCE_FILE_NAME,
            mimeType=XLSX_MIME_TYPE,
            webViewLink="https://drive.google.com/file/d/permanent-file-id/view",
        )
        self.content = content
        self.lookups: list[tuple[str, str]] = []
        self.replacements: list[tuple[str, bytes]] = []

    async def find_file(self, folder_id: str, name: str) -> DriveFile:
        self.lookups.append((folder_id, name))
        return self.file

    async def download(self, file_id: str) -> bytes:
        assert file_id == self.file.file_id
        return self.content

    async def replace(self, file_id: str, content: bytes) -> None:
        assert file_id == self.file.file_id
        self.content = content
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
    assert [file_id for file_id, _ in drive.replacements] == ["permanent-file-id"]
    assert drive.lookups == [
        ("stats-folder", ATTENDANCE_FILE_NAME),
        ("stats-folder", ATTENDANCE_FILE_NAME),
        ("stats-folder", ATTENDANCE_FILE_NAME),
    ]
    assert not await repository.edit(lambda book: book.add_game(2026, "Новая игра", ["Другой Игрок"]))
    assert len(drive.replacements) == 1


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
    assert STATS_ADD_GAME not in {button.label for button in game_menu.buttons}


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
    assert not publisher.commands


async def test_game_addition_uses_confirmed_cyrillic_names_and_is_idempotent(event: Event) -> None:
    engine, _users, _publisher, registrations = await engine_setup(MemoryDiskStore(), event, admin=True)
    for status, name in (
        (AttendanceStatus.CONFIRMED, "Новый Игрок"),
        (AttendanceStatus.WAITING, "Ждущий Игрок"),
        (AttendanceStatus.CANCELLED, "Бывший Игрок"),
    ):
        await registrations.import_missing(
            [
                Registration(
                    event_id=event.event_id,
                    participant_key=uuid4().hex * 2,
                    display_name=name,
                    attendance_status=status,
                    wish_play="Без пожеланий",
                )
            ]
        )
    drive = MemoryGoogleDrive(template())
    service = StatisticsService(
        GoogleDriveStatisticsRepository(drive, "stats-folder"),
        engine.admins,
        engine.administration,
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
    assert current["A5"].value == "Всего"
    assert current["N4"].value == 1
    retry = command.model_copy(update={"operation_id": str(uuid4())})
    assert "уже есть" in await service.apply(retry)
    assert len(drive.replacements) == 1
    await engine.events.archive(event.event_id, datetime(2026, 9, 2, tzinfo=UTC))
    with pytest.raises(DomainError, match="архивирована"):
        await service.apply(retry.model_copy(update={"operation_id": str(uuid4())}))


async def test_statistics_worker_delivers_permanent_link_and_rechecks_admin(event: Event) -> None:
    engine, users, publisher, _registrations = await engine_setup(MemoryDiskStore(), event, admin=True)
    await engine.handle(inbound(1, STATS_LINKS))
    drive = MemoryGoogleDrive(template())
    service = StatisticsService(
        GoogleDriveStatisticsRepository(drive, "stats-folder"), engine.admins, engine.administration
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
    assert "Посещаемость игроков" in transport.sent[0][3]
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


def test_invalid_workbook_and_unrecognized_footer_are_rejected() -> None:
    with pytest.raises(DomainError):
        StatisticsWorkbook(b"not a workbook")
    book = load_workbook(BytesIO(template()))
    book.active["A10"] = "Additional data that must not be lost"
    output = BytesIO()
    book.save(output)
    with pytest.raises(DomainError):
        StatisticsWorkbook(output.getvalue())
