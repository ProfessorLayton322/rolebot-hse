from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol, cast
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from larp_bot.application.services import DomainError

from .statistics_workbook import StatisticsWorkbook

ATTENDANCE_FILE_NAME = "player_attendance_stats.xlsx"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DriveFile(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    file_id: str = Field(alias="id", min_length=1)
    name: str = Field(min_length=1)
    mime_type: str = Field(alias="mimeType", min_length=1)
    web_view_link: str | None = Field(default=None, alias="webViewLink")


class _DriveFileList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[DriveFile]


class DriveResponse(Protocol):
    @property
    def content(self) -> bytes: ...

    def json(self) -> object: ...

    def raise_for_status(self) -> None: ...


class DriveSession(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> DriveResponse: ...


class DriveStorage(Protocol):
    async def find_file(self, folder_id: str, name: str) -> DriveFile: ...

    async def download(self, file_id: str) -> bytes: ...

    async def replace(self, file_id: str, content: bytes) -> None: ...


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveRestClient:
    """Minimal Drive v3 client authenticated by a service-account JSON document."""

    def __init__(self, service_account_json: str, session: DriveSession | None = None) -> None:
        if session is not None:
            self.session = session
            return
        try:
            decoded = json.loads(service_account_json)
            if not isinstance(decoded, dict):
                raise ValueError("service-account document must be an object")
            credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                cast(Mapping[str, Any], decoded), scopes=[DRIVE_SCOPE]
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not a valid service-account document") from exc
        self.session = cast(DriveSession, AuthorizedSession(credentials))  # type: ignore[no-untyped-call]

    async def _request(self, method: str, url: str, **kwargs: Any) -> DriveResponse:
        return await asyncio.to_thread(self.session.request, method, url, timeout=30, **kwargs)

    async def find_file(self, folder_id: str, name: str) -> DriveFile:
        query = (
            f"'{_drive_query_literal(folder_id)}' in parents and "
            f"name = '{_drive_query_literal(name)}' and trashed = false"
        )
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files",
            params={
                "q": query,
                "spaces": "drive",
                "pageSize": 2,
                "fields": "files(id,name,mimeType,webViewLink)",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            },
        )
        response.raise_for_status()
        try:
            files = _DriveFileList.model_validate(response.json()).files
        except ValidationError as exc:
            raise RuntimeError("Google Drive returned invalid file metadata") from exc
        if not files:
            raise DomainError(f"Файл {name} не найден в настроенной папке Google Drive.")
        if len(files) > 1:
            raise DomainError(f"В настроенной папке Google Drive найдено несколько файлов {name}.")
        file = files[0]
        if file.mime_type != XLSX_MIME_TYPE:
            raise DomainError(f"Файл {name} должен быть файлом XLSX.")
        return file

    async def download(self, file_id: str) -> bytes:
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        response.raise_for_status()
        return response.content

    async def replace(self, file_id: str, content: bytes) -> None:
        response = await self._request(
            "PATCH",
            f"{DRIVE_UPLOAD_API}/files/{quote(file_id, safe='')}",
            params={"uploadType": "media", "supportsAllDrives": "true"},
            headers={"Content-Type": XLSX_MIME_TYPE},
            data=content,
        )
        response.raise_for_status()


class GoogleDriveStatisticsRepository:
    """Read and revise one permanent Drive XLSX without replacing its file resource."""

    def __init__(self, drive: DriveStorage, folder_id: str) -> None:
        if not folder_id.strip():
            raise ValueError("Google Drive folder ID must not be empty")
        self.drive = drive
        self.folder_id = folder_id.strip()

    async def _file(self) -> DriveFile:
        return await self.drive.find_file(self.folder_id, ATTENDANCE_FILE_NAME)

    async def link(self) -> str:
        file = await self._file()
        return file.web_view_link or f"https://drive.google.com/file/d/{quote(file.file_id, safe='')}/view"

    async def has_any_player(self, names: Collection[str]) -> bool:
        file = await self._file()
        content = await self.drive.download(file.file_id)
        return await asyncio.to_thread(lambda: StatisticsWorkbook(content).has_any_player(tuple(names)))

    async def edit(self, transform: Callable[[StatisticsWorkbook], bytes | None]) -> bool:
        file = await self._file()
        content = await self.drive.download(file.file_id)
        result = await asyncio.to_thread(lambda: transform(StatisticsWorkbook(content)))
        if result is None:
            return False
        await self.drive.replace(file.file_id, result)
        return True
