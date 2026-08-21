from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol

import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from larp_bot.application.services import OperationNotAllowed, RegistrationNotFound
from larp_bot.domain.models import AttendanceStatus, Event, Registration

VISIBLE_HEADERS = (
    "№",
    "Имя",
    "Предыдущий опыт в LARP",
    "Готовность к кроссполу",
    "С кем хочу играть",
    "Пожелания по персонажу",
    "Текущий статус",
)
TECHNICAL_HEADERS = ("participant_key", "last_operation_id", "updated_at")
ALL_HEADERS = VISIBLE_HEADERS + TECHNICAL_HEADERS
LEGACY_HEADERS = (
    "Имя",
    "С кем хочу играть",
    "Пожелания по персонажу",
    "Статус",
    *TECHNICAL_HEADERS,
)


class WorkbookIntegrityError(RuntimeError):
    pass


class DiskObjectStore(Protocol):
    async def download(self, path: str) -> bytes: ...

    async def upload_new(self, path: str, content: bytes) -> None: ...

    async def replace(self, path: str, content: bytes) -> None: ...

    async def publish(self, path: str) -> str: ...

    async def delete(self, path: str) -> None: ...


def _cell_text(value: object) -> str:
    return "" if value is None else str(value)


def safe_cell(value: str) -> str:
    """Neutralize spreadsheet formulas while retaining the visible user text."""
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def display_cell(value: object) -> str:
    text = _cell_text(value)
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":
        return text[1:]
    return text


def _bool_cell(value: bool | None) -> str:
    if value is None:
        return "Не указано"
    return "Да" if value else "Нет"


def _format_sheet(sheet: Worksheet) -> None:
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = "A1:G1"
    widths = (8, 30, 25, 26, 35, 45, 18)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width
    for index in range(8, 11):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].hidden = True


def empty_workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Регистрация"
    sheet.append(ALL_HEADERS)
    _format_sheet(sheet)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _open_checked(content: bytes) -> tuple[Workbook, Worksheet]:
    try:
        workbook = load_workbook(BytesIO(content))
    except Exception as exc:
        raise WorkbookIntegrityError("registration resource is not a valid XLSX") from exc
    sheet = workbook.active
    actual = tuple(_cell_text(sheet.cell(1, column).value) for column in range(1, sheet.max_column + 1))
    if actual == LEGACY_HEADERS:
        # Existing public workbooks are migrated in memory and persisted by the
        # next mutation, so queued commands survive a rolling deployment.
        sheet.insert_cols(1)
        sheet.insert_cols(3, amount=2)
        for column, header in enumerate(ALL_HEADERS, start=1):
            sheet.cell(1, column, header)
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row, 1, row - 1)
            sheet.cell(row, 3, "Не указано")
            sheet.cell(row, 4, "Не указано")
        _format_sheet(sheet)
    elif actual != ALL_HEADERS:
        workbook.close()
        raise WorkbookIntegrityError(
            f"unexpected registration workbook schema: {json.dumps(actual, ensure_ascii=False)}"
        )
    return workbook, sheet


def _serialize(workbook: Workbook) -> bytes:
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _find_row(sheet: Worksheet, key: str) -> int | None:
    for row in range(2, sheet.max_row + 1):
        if _cell_text(sheet.cell(row, 8).value) == key:
            return row
    return None


def _registration(event_id: str, sheet: Worksheet, row: int) -> Registration:
    updated_raw = _cell_text(sheet.cell(row, 10).value)
    try:
        updated = datetime.fromisoformat(updated_raw)
    except ValueError:
        updated = datetime.now(UTC)
    return Registration(
        event_id=event_id,
        participant_key=_cell_text(sheet.cell(row, 8).value),
        display_name=display_cell(sheet.cell(row, 2).value),
        wish_play=display_cell(sheet.cell(row, 5).value),
        character_wish=display_cell(sheet.cell(row, 6).value),
        attendance_status=AttendanceStatus(_cell_text(sheet.cell(row, 7).value)),
        last_operation_id=_cell_text(sheet.cell(row, 9).value),
        updated_at=updated,
    )


class YandexDiskRestClient:
    API = "https://cloud-api.yandex.net/v1/disk"

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._headers = {"Authorization": f"OAuth {token}"}

    async def _link(self, endpoint: str, path: str, *, overwrite: bool = False) -> str:
        response = await self._client.get(
            f"{self.API}/{endpoint}",
            headers=self._headers,
            params={"path": path, "overwrite": str(overwrite).lower()},
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

    async def _upload(self, path: str, content: bytes, *, overwrite: bool) -> None:
        href = await self._link("resources/upload", path, overwrite=overwrite)
        response = await self._client.put(
            href,
            content=content,
            headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        )
        response.raise_for_status()

    async def _ensure_parent_directories(self, path: str) -> None:
        if not path.startswith("disk:/") or "/" not in path.removeprefix("disk:/"):
            return
        parent = path.rsplit("/", 1)[0]
        segments = parent.removeprefix("disk:/").split("/")
        for index in range(1, len(segments) + 1):
            directory = "disk:/" + "/".join(segments[:index])
            response = await self._client.put(
                f"{self.API}/resources",
                headers=self._headers,
                params={"path": directory},
            )
            if response.status_code != 409:
                response.raise_for_status()

    async def upload_new(self, path: str, content: bytes) -> None:
        await self._ensure_parent_directories(path)
        await self._upload(path, content, overwrite=False)

    async def replace(self, path: str, content: bytes) -> None:
        # overwrite=true updates the same Disk resource; it does not delete/re-publish it.
        await self._upload(path, content, overwrite=True)

    async def publish(self, path: str) -> str:
        response = await self._client.put(f"{self.API}/resources/publish", headers=self._headers, params={"path": path})
        response.raise_for_status()
        metadata = await self._client.get(
            f"{self.API}/resources", headers=self._headers, params={"path": path, "fields": "public_url"}
        )
        metadata.raise_for_status()
        public_url = metadata.json().get("public_url")
        if not isinstance(public_url, str) or not public_url.startswith("https://"):
            raise RuntimeError("Yandex Disk resource was published without a public URL")
        return public_url

    async def delete(self, path: str) -> None:
        response = await self._client.delete(
            f"{self.API}/resources",
            headers=self._headers,
            params={"path": path, "permanently": "true"},
        )
        if response.status_code != 404:
            response.raise_for_status()


class YandexDiskRegistrationRepository:
    def __init__(self, store: DiskObjectStore) -> None:
        self.store = store

    async def create_event_workbook(self, disk_path: str) -> str:
        await self.store.upload_new(disk_path, empty_workbook_bytes())
        try:
            return await self.store.publish(disk_path)
        except Exception:
            await self.store.delete(disk_path)
            raise

    async def delete_event_workbook(self, disk_path: str) -> None:
        await self.store.delete(disk_path)

    async def find_registration(self, event: Event, participant_key: str) -> Registration | None:
        content = await self.store.download(event.disk_resource_path)
        workbook, sheet = await asyncio.to_thread(_open_checked, content)
        try:
            row = _find_row(sheet, participant_key)
            return None if row is None else _registration(event.event_id, sheet, row)
        finally:
            workbook.close()

    async def _mutate(
        self,
        event: Event,
        participant_key: str,
        operation_id: str,
        mutation: Callable[[Worksheet, int | None], None],
    ) -> bool:
        content = await self.store.download(event.disk_resource_path)
        workbook, sheet = await asyncio.to_thread(_open_checked, content)
        row = _find_row(sheet, participant_key)
        if row is not None and _cell_text(sheet.cell(row, 9).value) == operation_id:
            workbook.close()
            return False
        try:
            mutation(sheet, row)
            new_row = _find_row(sheet, participant_key)
            if new_row is None:
                raise WorkbookIntegrityError("mutation did not produce a participant row")
            sheet.cell(new_row, 9, operation_id)
            sheet.cell(new_row, 10, datetime.now(UTC).isoformat())
            serialized = await asyncio.to_thread(_serialize, workbook)
        except Exception:
            workbook.close()
            raise
        await self.store.replace(event.disk_resource_path, serialized)
        return True

    async def enlist(
        self,
        event: Event,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        wish_play: str,
        larp_experience: bool | None = None,
        crossplay: bool | None = None,
    ) -> bool:
        def mutation(sheet: Worksheet, row: int | None) -> None:
            target = row or sheet.max_row + 1
            existing_status = AttendanceStatus(_cell_text(sheet.cell(target, 7).value)) if row else None
            sheet.cell(target, 1, target - 1)
            sheet.cell(target, 2, safe_cell(display_name))
            sheet.cell(target, 3, _bool_cell(larp_experience))
            sheet.cell(target, 4, _bool_cell(crossplay))
            sheet.cell(target, 5, safe_cell(wish_play))
            if row is None:
                sheet.cell(target, 6, "")
                sheet.cell(target, 7, AttendanceStatus.WAITING.value)
                sheet.cell(target, 8, participant_key)
            elif existing_status is AttendanceStatus.CANCELLED:
                sheet.cell(target, 7, AttendanceStatus.WAITING.value)

        return await self._mutate(event, participant_key, operation_id, mutation)

    async def confirm(
        self,
        event: Event,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool:
        def mutation(sheet: Worksheet, row: int | None) -> None:
            if row is None:
                raise RegistrationNotFound("registration row does not exist")
            # Both values are written to the local workbook before one replacement upload.
            sheet.cell(row, 6, safe_cell(character_wish))
            sheet.cell(row, 7, AttendanceStatus.CONFIRMED.value)

        return await self._mutate(event, participant_key, operation_id, mutation)

    async def update_character_wish(
        self,
        event: Event,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool:
        def mutation(sheet: Worksheet, row: int | None) -> None:
            if row is None:
                raise RegistrationNotFound("registration row does not exist")
            status = AttendanceStatus(_cell_text(sheet.cell(row, 7).value))
            if status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("character wish cannot be edited while cancelled")
            if status is AttendanceStatus.WAITING and not _cell_text(sheet.cell(row, 6).value):
                raise OperationNotAllowed("first character wish must be supplied with confirmation")
            sheet.cell(row, 6, safe_cell(character_wish))

        return await self._mutate(event, participant_key, operation_id, mutation)

    async def cancel(self, event: Event, *, operation_id: str, participant_key: str) -> bool:
        def mutation(sheet: Worksheet, row: int | None) -> None:
            if row is None:
                raise RegistrationNotFound("registration row does not exist")
            sheet.cell(row, 7, AttendanceStatus.CANCELLED.value)

        return await self._mutate(event, participant_key, operation_id, mutation)
