from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol, cast

import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Color, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from larp_bot.adapters.yandex_disk.achievements import (
    ACHIEVEMENT_TEMPLATE_PATH,
    achievement_presentation_bytes,
)
from larp_bot.domain.models import (
    DEFAULT_PLAYER_AMOUNT,
    MAX_PLAYER_AMOUNT,
    AttendanceStatus,
    Event,
    PassDetails,
    Registration,
    surname_and_name,
)

VISIBLE_HEADERS = (
    "№",
    "Имя",
    "Профиль ВКонтакте",
    "Профиль в Telegram",
    "Предыдущий опыт в LARP",
    "Готовность к кроссполу",
    "С кем хочу играть",
    "Пожелания по персонажу",
    "Текущий статус",
)
PUBLIC_HEADERS = (
    "№",
    "Имя",
    "Готовность к кроссполу",
    "С кем хочу играть",
    "Пожелания по персонажу",
    "Текущий статус",
)
# These layouts are read only while migrating deployments that used XLSX as
# storage. Newly generated master files contain VISIBLE_HEADERS; public files
# use PUBLIC_HEADERS.
STATEFUL_HEADERS = (
    "№",
    "Имя",
    "Предыдущий опыт в LARP",
    "Готовность к кроссполу",
    "С кем хочу играть",
    "Пожелания по персонажу",
    "Текущий статус",
    "participant_key",
    "last_operation_id",
    "updated_at",
)
LEGACY_HEADERS = (
    "Имя",
    "С кем хочу играть",
    "Пожелания по персонажу",
    "Статус",
    "participant_key",
    "last_operation_id",
    "updated_at",
)
PASS_TABLE_HEADERS = (
    "Фамилия (Кириллицей)",
    "Имя (Кириллицей)",
    "Отчество (Кириллицей)",
    "Иностранный гражданин (Да/Нет)",
    "Фамилия (Латиницей)",
    "Имя (Латиницей)",
    "Отчество (Латиницей)",
    "Телефон",
    "E-mail",
)
TABLEAU_10_COLORS = (
    "4E79A7",
    "F28E2B",
    "E15759",
    "76B7B2",
    "59A14F",
    "EDC948",
    "B07AA1",
    "FF9DA7",
    "9C755F",
    "BAB0AC",
)
CHARACTER_WISH_CALLBACK_RE = re.compile(r"select:confirm:[a-z0-9-]{8,80}", re.IGNORECASE)
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class WorkbookIntegrityError(RuntimeError):
    pass


class DiskObjectStore(Protocol):
    async def download(self, path: str) -> bytes: ...

    async def delete(self, path: str) -> None: ...


@dataclass(frozen=True)
class DiskUpload:
    role: str
    disk_path: str
    content: bytes
    content_type: str
    overwrite: bool
    publish: bool


UploadReadyCallback = Callable[[], Awaitable[None]]


class DiskUploader(Protocol):
    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> dict[str, str] | None: ...


def _cell_text(value: object) -> str:
    return "" if value is None else str(value)


def safe_cell(value: str) -> str:
    """Neutralize spreadsheet formulas while retaining the visible user text."""
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def character_wish_cell(value: str) -> str:
    """Hide leaked confirmation callback payloads from generated workbooks."""
    return "" if CHARACTER_WISH_CALLBACK_RE.search(value) else safe_cell(value)


def display_cell(value: object) -> str:
    text = _cell_text(value)
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":
        return text[1:]
    return text


def _bool_cell(value: bool | None) -> str:
    if value is None:
        return "Не указано"
    return "Да" if value else "Нет"


def _parse_bool(value: object) -> bool | None:
    text = _cell_text(value)
    if text == "Да":
        return True
    if text == "Нет":
        return False
    return None


def _format_sheet(sheet: Worksheet, widths: Sequence[float]) -> None:
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(widths))}1"
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width


def _ordered_active_registrations(registrations: Sequence[Registration]) -> list[Registration]:
    active = [
        registration
        for registration in registrations
        if registration.attendance_status is not AttendanceStatus.CANCELLED
    ]
    return sorted(active, key=lambda row: (row.created_at, row.participant_key))


def _player_component_colors(registrations: Sequence[Registration]) -> dict[str, str]:
    identifiers = [
        tuple(
            identifier.casefold()
            for identifier in (surname_and_name(registration.display_name), registration.nickname)
            if identifier
        )
        for registration in registrations
    ]

    adjacency = [set[int]() for _ in registrations]
    for index, registration in enumerate(registrations):
        for wished_name in registration.wish_play.split(","):
            tokens = wished_name.casefold().split()
            if not tokens:
                continue
            for wished_index, candidate_identifiers in enumerate(identifiers):
                if wished_index == index:
                    continue
                if not any(all(token in candidate for token in tokens) for candidate in candidate_identifiers):
                    continue
                adjacency[index].add(wished_index)
                adjacency[wished_index].add(index)

    components: list[list[int]] = []
    visited: set[int] = set()
    for start in range(len(registrations)):
        if start in visited:
            continue
        component: list[int] = []
        stack = [start]
        visited.add(start)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        if len(component) > 1:
            components.append(component)

    components.sort(key=lambda component: (-len(component), min(component)))
    return {
        registrations[index].participant_key: color
        for component, color in zip(components, TABLEAU_10_COLORS, strict=False)
        for index in component
    }


def _paint_player_name(
    sheet: Worksheet,
    registration: Registration,
    component_colors: dict[str, str],
) -> None:
    color = component_colors.get(registration.participant_key)
    if color is not None:
        sheet.cell(sheet.max_row, 2).fill = PatternFill(fill_type="solid", fgColor=f"FF{color}")


def _append_reserve_separator(sheet: Worksheet, *, player_amount: int, column_count: int) -> None:
    if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
        raise ValueError(f"player_amount must be between 1 and {MAX_PLAYER_AMOUNT}")
    reserve_row = player_amount + 2
    sheet.merge_cells(start_row=reserve_row, start_column=1, end_row=reserve_row, end_column=column_count)
    cell = sheet.cell(reserve_row, 1, "ЗАПАС")
    cell.fill = PatternFill(fill_type="solid", fgColor="00B050")
    cell.font = Font(bold=True, color="FFFFFF")
    cell.alignment = Alignment(horizontal="center", vertical="center")


def showcase_workbook_bytes(
    registrations: Sequence[Registration] = (),
    *,
    player_amount: int = DEFAULT_PLAYER_AMOUNT,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Регистрация"
    sheet.append(VISIBLE_HEADERS)
    # Keep the first player_amount rows fixed as main-roster slots. Padding
    # prevents the green separator from moving while later signups flow below.
    ordered_registrations = _ordered_active_registrations(registrations)
    component_colors = _player_component_colors(ordered_registrations)
    for number, registration in enumerate(ordered_registrations[:player_amount], start=1):
        sheet.append(
            (
                number,
                safe_cell(surname_and_name(registration.display_name)),
                safe_cell(registration.vk_profile),
                safe_cell(registration.telegram_profile or ""),
                _bool_cell(registration.larp_experience),
                _bool_cell(registration.crossplay),
                safe_cell(registration.wish_play),
                character_wish_cell(registration.character_wish),
                registration.attendance_status.value,
            )
        )
        _paint_player_name(sheet, registration, component_colors)
    for number in range(len(ordered_registrations) + 1, player_amount + 1):
        sheet.append((number,))
    _append_reserve_separator(sheet, player_amount=player_amount, column_count=len(VISIBLE_HEADERS))
    for number, registration in enumerate(ordered_registrations[player_amount:], start=player_amount + 1):
        sheet.append(
            (
                number,
                safe_cell(surname_and_name(registration.display_name)),
                safe_cell(registration.vk_profile),
                safe_cell(registration.telegram_profile or ""),
                _bool_cell(registration.larp_experience),
                _bool_cell(registration.crossplay),
                safe_cell(registration.wish_play),
                character_wish_cell(registration.character_wish),
                registration.attendance_status.value,
            )
        )
        _paint_player_name(sheet, registration, component_colors)
    _format_sheet(sheet, (8, 30, 32, 32, 25, 26, 35, 45, 18))
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def public_showcase_workbook_bytes(
    registrations: Sequence[Registration] = (),
    *,
    player_amount: int = DEFAULT_PLAYER_AMOUNT,
) -> bytes:
    """Render the player-safe projection without Telegram or VK contacts."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Регистрация"
    sheet.append(PUBLIC_HEADERS)
    ordered_registrations = _ordered_active_registrations(registrations)
    component_colors = _player_component_colors(ordered_registrations)
    for number, registration in enumerate(ordered_registrations[:player_amount], start=1):
        sheet.append(
            (
                number,
                safe_cell(registration.nickname or surname_and_name(registration.display_name)),
                _bool_cell(registration.crossplay),
                safe_cell(registration.wish_play),
                character_wish_cell(registration.character_wish),
                registration.attendance_status.value,
            )
        )
        _paint_player_name(sheet, registration, component_colors)
    for number in range(len(ordered_registrations) + 1, player_amount + 1):
        sheet.append((number,))
    _append_reserve_separator(sheet, player_amount=player_amount, column_count=len(PUBLIC_HEADERS))
    for number, registration in enumerate(ordered_registrations[player_amount:], start=player_amount + 1):
        sheet.append(
            (
                number,
                safe_cell(registration.nickname or surname_and_name(registration.display_name)),
                _bool_cell(registration.crossplay),
                safe_cell(registration.wish_play),
                character_wish_cell(registration.character_wish),
                registration.attendance_status.value,
            )
        )
        _paint_player_name(sheet, registration, component_colors)
    _format_sheet(sheet, (8, 30, 26, 35, 45, 18))
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def pass_table_workbook_bytes(profiles: Sequence[PassDetails] = ()) -> bytes:
    """Render the pass-list schema and header style used by the supplied venue template."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Лист1"
    sheet.append(PASS_TABLE_HEADERS)
    thin = Side(style="thin", color="000000")
    header_fill = PatternFill(fill_type="solid", fgColor=Color(theme=5))
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(name="Calibri Light", size=14)
        cell.alignment = Alignment(wrap_text=True)
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        cell.number_format = "@"
    sheet.row_dimensions[1].height = 57
    patronym_width = 24.28515625
    widths = (
        24.85546875,
        22.140625,
        patronym_width,
        13,
        40.28515625,
        22.140625,
        patronym_width,
        33.85546875,
        43.7109375,
    )
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width

    for profile in profiles:
        sheet.append(
            (
                profile.surname_cyrillic,
                profile.name_cyrillic,
                profile.patronym_cyrillic,
                "Да" if profile.foreigner else "Нет",
                profile.surname_latin if profile.foreigner else None,
                profile.name_latin if profile.foreigner else None,
                profile.patronym_latin if profile.foreigner else None,
                profile.mobile_phone,
                profile.email,
            )
        )
        sheet.row_dimensions[sheet.max_row].height = 19.5
        for cell in sheet[sheet.max_row]:
            cell.font = Font(name="Calibri Light", size=14)
            cell.alignment = Alignment(wrap_text=True)
            cell.number_format = "@"

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _legacy_registrations(event_id: str, content: bytes) -> list[Registration]:
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False)
    except Exception as exc:
        raise WorkbookIntegrityError("registration resource is not a valid XLSX") from exc
    try:
        sheet = workbook.active
        actual = tuple(_cell_text(cell.value) for cell in next(sheet.iter_rows(min_row=1, max_row=1)))
        if actual not in {STATEFUL_HEADERS, LEGACY_HEADERS}:
            raise WorkbookIntegrityError(
                f"unexpected legacy registration workbook schema: {json.dumps(actual, ensure_ascii=False)}"
            )
        registrations: list[Registration] = []
        stateful = actual == STATEFUL_HEADERS
        for row in sheet.iter_rows(min_row=2, values_only=True):
            key_index = 7 if stateful else 4
            participant = _cell_text(row[key_index] if len(row) > key_index else None)
            if not participant:
                continue
            updated_index = 9 if stateful else 6
            updated_raw = _cell_text(row[updated_index] if len(row) > updated_index else None)
            try:
                updated_at = datetime.fromisoformat(updated_raw)
            except ValueError:
                updated_at = datetime.now(UTC)
            registrations.append(
                Registration(
                    event_id=event_id,
                    participant_key=participant,
                    display_name=display_cell(row[1] if stateful else row[0]),
                    larp_experience=_parse_bool(row[2]) if stateful else None,
                    crossplay=_parse_bool(row[3]) if stateful else None,
                    wish_play=display_cell(row[4] if stateful else row[1]),
                    character_wish=display_cell(row[5] if stateful else row[2]),
                    attendance_status=AttendanceStatus(_cell_text(row[6] if stateful else row[3])),
                    last_operation_id=_cell_text(row[8] if stateful else row[5]),
                    created_at=updated_at,
                    updated_at=updated_at,
                )
            )
        return registrations
    finally:
        workbook.close()


class YandexDiskRestClient:
    API = "https://cloud-api.yandex.net/v1/disk"

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._headers = {"Authorization": f"OAuth {token}"}

    async def _link(self, endpoint: str, path: str) -> str:
        response = await self._client.get(
            f"{self.API}/{endpoint}",
            headers=self._headers,
            params={"path": path},
        )
        response.raise_for_status()
        href = response.json().get("href")
        if not isinstance(href, str) or not href.startswith("https://"):
            raise RuntimeError("Yandex Disk did not return an HTTPS transfer URL")
        return href

    async def download(self, path: str) -> bytes:
        href = await self._link("resources/download", path)
        response = await self._client.get(href, follow_redirects=True)
        response.raise_for_status()
        return response.content

    async def exists(self, path: str) -> bool:
        response = await self._client.get(
            f"{self.API}/resources", headers=self._headers, params={"path": path, "fields": "path"}
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def list_files(self, path: str) -> list[str]:
        files: list[str] = []
        offset = 0
        while True:
            response = await self._client.get(
                f"{self.API}/resources",
                headers=self._headers,
                params={"path": path, "limit": 1000, "offset": offset},
            )
            response.raise_for_status()
            embedded = response.json()["_embedded"]
            items = embedded["items"]
            files.extend(item["path"] for item in items if item["type"] == "file")
            offset += len(items)
            if offset >= embedded["total"]:
                return files
            if not items:
                raise RuntimeError("Yandex Disk returned an incomplete directory listing")

    async def delete(self, path: str) -> None:
        response = await self._client.delete(
            f"{self.API}/resources",
            headers=self._headers,
            params={"path": path, "permanently": "true"},
        )
        if response.status_code != 404:
            response.raise_for_status()


class YandexDiskShowcaseRepository:
    def __init__(
        self,
        store: DiskObjectStore,
        uploader: DiskUploader | None = None,
        *,
        achievement_template: bytes | None = None,
    ) -> None:
        self.store = store
        self.uploader = uploader if uploader is not None else cast(DiskUploader, store)
        self.deferred_uploads = uploader is not None or bool(getattr(store, "deferred_uploads", False))
        self._achievement_template = achievement_template

    async def _submit_one(
        self,
        disk_path: str,
        content: bytes,
        *,
        content_type: str,
        overwrite: bool,
        publish: bool,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
        role: str = "file",
    ) -> str | None:
        result = await self.uploader.submit(
            [DiskUpload(role, disk_path, content, content_type, overwrite, publish)],
            callback=callback,
            before_invoke=before_invoke,
        )
        return None if result is None else result.get(role)

    async def create_event_workbook(
        self,
        disk_path: str,
        registrations: Sequence[Registration] = (),
        *,
        player_amount: int = DEFAULT_PLAYER_AMOUNT,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> str | None:
        content = await asyncio.to_thread(showcase_workbook_bytes, registrations, player_amount=player_amount)
        return await self._submit_one(
            disk_path,
            content,
            content_type=XLSX_MIME_TYPE,
            overwrite=False,
            publish=True,
            callback=callback,
            before_invoke=before_invoke,
        )

    async def create_public_event_workbook(
        self,
        disk_path: str,
        registrations: Sequence[Registration] = (),
        *,
        player_amount: int = DEFAULT_PLAYER_AMOUNT,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> str | None:
        content = await asyncio.to_thread(public_showcase_workbook_bytes, registrations, player_amount=player_amount)
        return await self._submit_one(
            disk_path,
            content,
            content_type=XLSX_MIME_TYPE,
            overwrite=False,
            publish=True,
            callback=callback,
            before_invoke=before_invoke,
        )

    async def create_event_bundle(
        self,
        master_path: str,
        public_path: str,
        pass_path: str,
        pass_profiles: Sequence[PassDetails],
        *,
        player_amount: int,
        callback: dict[str, object],
        before_invoke: UploadReadyCallback | None = None,
    ) -> dict[str, str] | None:
        master, public, passes = await asyncio.gather(
            asyncio.to_thread(showcase_workbook_bytes, player_amount=player_amount),
            asyncio.to_thread(public_showcase_workbook_bytes, player_amount=player_amount),
            asyncio.to_thread(pass_table_workbook_bytes, pass_profiles),
        )
        return await self.uploader.submit(
            [
                DiskUpload("master", master_path, master, XLSX_MIME_TYPE, False, True),
                DiskUpload("public", public_path, public, XLSX_MIME_TYPE, False, True),
                DiskUpload("pass", pass_path, passes, XLSX_MIME_TYPE, False, True),
            ],
            callback=callback,
            before_invoke=before_invoke,
        )

    async def delete_event_workbook(self, disk_path: str) -> None:
        await self.store.delete(disk_path)

    async def create_pass_table(
        self,
        disk_path: str,
        profiles: Sequence[PassDetails],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> str | None:
        content = await asyncio.to_thread(pass_table_workbook_bytes, profiles)
        return await self._submit_one(
            disk_path,
            content,
            content_type=XLSX_MIME_TYPE,
            overwrite=False,
            publish=True,
            callback=callback,
            before_invoke=before_invoke,
            role="pass",
        )

    async def replace_pass_table(self, disk_path: str, profiles: Sequence[PassDetails]) -> None:
        content = await asyncio.to_thread(pass_table_workbook_bytes, profiles)
        await self._submit_one(
            disk_path,
            content,
            content_type=XLSX_MIME_TYPE,
            overwrite=True,
            publish=False,
        )

    async def delete_pass_table(self, disk_path: str) -> None:
        await self.store.delete(disk_path)

    async def create_achievement_presentation(
        self,
        disk_path: str,
        game_name: str,
        *,
        overwrite: bool = False,
        callback: dict[str, object] | None = None,
        before_invoke: UploadReadyCallback | None = None,
    ) -> str | None:
        template = self._achievement_template
        if template is None:
            template = await asyncio.to_thread(ACHIEVEMENT_TEMPLATE_PATH.read_bytes)
            self._achievement_template = template
        content = await asyncio.to_thread(achievement_presentation_bytes, template, game_name)
        return await self._submit_one(
            disk_path,
            content,
            content_type=PPTX_MIME_TYPE,
            overwrite=overwrite,
            publish=True,
            callback=callback,
            before_invoke=before_invoke,
            role="achievement",
        )

    async def delete_achievement_presentation(self, disk_path: str) -> None:
        await self.store.delete(disk_path)

    async def read_legacy_registrations(self, event: Event) -> Sequence[Registration]:
        content = await self.store.download(event.disk_resource_path)
        return await asyncio.to_thread(_legacy_registrations, event.event_id, content)

    async def replace(self, event: Event, registrations: Sequence[Registration]) -> None:
        master_content, public_content = await asyncio.gather(
            asyncio.to_thread(showcase_workbook_bytes, registrations, player_amount=event.player_amount),
            asyncio.to_thread(public_showcase_workbook_bytes, registrations, player_amount=event.player_amount),
        )
        uploads = [
            DiskUpload(
                "master",
                event.master_table_resource_path,
                master_content,
                XLSX_MIME_TYPE,
                True,
                False,
            )
        ]
        if event.public_table_resource_path is not None:
            uploads.append(
                DiskUpload(
                    "public",
                    event.public_table_resource_path,
                    public_content,
                    XLSX_MIME_TYPE,
                    True,
                    False,
                )
            )
        await self.uploader.submit(uploads)
