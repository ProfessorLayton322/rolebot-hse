from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote
from uuid import uuid4

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import credentials as oauth_credentials
from google.oauth2 import service_account
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from larp_bot.application.services import DomainError

from .statistics_workbook import MasterStatisticsWorkbook, StatisticsWorkbook

ATTENDANCE_FILE_NAME = "player_attendance_stats.xlsx"
MASTER_STATISTICS_FILE_NAME = "master_statistics.xlsx"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
JSON_MIME_TYPE = "application/json"


class DriveFile(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    file_id: str = Field(alias="id", min_length=1)
    name: str = Field(min_length=1)
    mime_type: str = Field(alias="mimeType", min_length=1)
    web_view_link: str | None = Field(default=None, alias="webViewLink")


class _DriveFileList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[DriveFile]


class _DrivePermission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    permission_type: str = Field(alias="type")
    role: str


class _DrivePermissionList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    permissions: list[_DrivePermission] = Field(default_factory=list)


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

    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile: ...

    async def delete_named_xlsx(self, folder_id: str, name: str) -> None: ...

    async def named_xlsx_exists(self, folder_id: str, name: str) -> bool: ...

    async def publish_file(self, file_id: str) -> None: ...


@dataclass(frozen=True)
class PreparedStatisticsEdit:
    file: DriveFile
    content: bytes | None


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveRestClient:
    """Minimal Drive v3 client authenticated by a Google credential document."""

    def __init__(self, service_account_json: str, session: DriveSession | None = None) -> None:
        if session is not None:
            self.session = session
            return
        try:
            decoded = json.loads(service_account_json)
            if not isinstance(decoded, dict):
                raise ValueError("credential document must be an object")
            credential_info = cast(Mapping[str, Any], decoded)
            if decoded.get("type") == "authorized_user":
                credentials = oauth_credentials.Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                    credential_info,
                    scopes=[DRIVE_SCOPE],
                )
            elif decoded.get("type") == "service_account":
                credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                    credential_info,
                    scopes=[DRIVE_SCOPE],
                )
            else:
                raise ValueError("credential type must be service_account or authorized_user")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not a valid Google credential document") from exc
        self.session = cast(DriveSession, AuthorizedSession(credentials))  # type: ignore[no-untyped-call]

    async def _request(self, method: str, url: str, **kwargs: Any) -> DriveResponse:
        return await asyncio.to_thread(self.session.request, method, url, timeout=30, **kwargs)

    async def find_file(self, folder_id: str, name: str) -> DriveFile:
        files = await self._find_files(folder_id, name)
        if not files:
            raise DomainError(f"Файл {name} не найден в настроенной папке Google Drive.")
        if len(files) > 1:
            raise DomainError(f"В настроенной папке Google Drive найдено несколько файлов {name}.")
        file = files[0]
        if file.mime_type != XLSX_MIME_TYPE:
            raise DomainError(f"Файл {name} должен быть файлом XLSX.")
        return file

    async def _find_files(self, folder_id: str, name: str) -> list[DriveFile]:
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
        return files

    async def download(self, file_id: str) -> bytes:
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        response.raise_for_status()
        return response.content

    async def replace(self, file_id: str, content: bytes) -> None:
        await self.replace_media(file_id, content, XLSX_MIME_TYPE)

    async def replace_media(self, file_id: str, content: bytes, mime_type: str) -> None:
        response = await self._request(
            "PATCH",
            f"{DRIVE_UPLOAD_API}/files/{quote(file_id, safe='')}",
            params={"uploadType": "media", "supportsAllDrives": "true"},
            headers={"Content-Type": mime_type},
            data=content,
        )
        response.raise_for_status()

    async def replace_with_metadata(
        self,
        file_id: str,
        *,
        name: str,
        mime_type: str,
        content: bytes,
    ) -> DriveFile:
        boundary = f"rolebot-{uuid4().hex}"
        metadata = json.dumps(
            {"name": name, "mimeType": mime_type},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        body = b"".join(
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                metadata,
                f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        response = await self._request(
            "PATCH",
            f"{DRIVE_UPLOAD_API}/files/{quote(file_id, safe='')}",
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,webViewLink",
            },
            headers={"Content-Type": f'multipart/related; boundary="{boundary}"'},
            data=body,
        )
        response.raise_for_status()
        try:
            return DriveFile.model_validate(response.json())
        except ValidationError as exc:
            raise RuntimeError("Google Drive returned invalid updated-file metadata") from exc

    async def find_named_files(self, folder_id: str, name: str) -> list[DriveFile]:
        return await self._find_files(folder_id, name)

    async def _create_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        boundary = f"rolebot-{uuid4().hex}"
        metadata = json.dumps(
            {"name": name, "mimeType": XLSX_MIME_TYPE, "parents": [folder_id]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        body = b"".join(
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                metadata,
                f"\r\n--{boundary}\r\nContent-Type: {XLSX_MIME_TYPE}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        response = await self._request(
            "POST",
            f"{DRIVE_UPLOAD_API}/files",
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,webViewLink",
            },
            headers={"Content-Type": f'multipart/related; boundary="{boundary}"'},
            data=body,
        )
        response.raise_for_status()
        try:
            return DriveFile.model_validate(response.json())
        except ValidationError as exc:
            raise RuntimeError("Google Drive returned invalid created-file metadata") from exc

    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        files = await self._find_files(folder_id, name)
        if len(files) > 1:
            raise RuntimeError(f"Google Drive contains duplicate mirrored table {name}")
        if not files:
            return await self._create_xlsx(folder_id, name, content)
        file = files[0]
        if file.mime_type != XLSX_MIME_TYPE:
            raise RuntimeError(f"Google Drive mirror destination {name} is not XLSX")
        await self.replace(file.file_id, content)
        return file

    async def delete_named_xlsx(self, folder_id: str, name: str) -> None:
        files = await self._find_files(folder_id, name)
        for file in files:
            if file.mime_type != XLSX_MIME_TYPE:
                raise RuntimeError(f"Google Drive mirror destination {name} is not XLSX")
            response = await self._request(
                "DELETE",
                f"{DRIVE_API}/files/{quote(file.file_id, safe='')}",
                params={"supportsAllDrives": "true"},
            )
            response.raise_for_status()

    async def named_xlsx_exists(self, folder_id: str, name: str) -> bool:
        files = await self._find_files(folder_id, name)
        if len(files) > 1:
            raise RuntimeError(f"Google Drive contains duplicate mirrored table {name}")
        if not files:
            return False
        if files[0].mime_type != XLSX_MIME_TYPE:
            raise RuntimeError(f"Google Drive mirror destination {name} is not XLSX")
        return True

    async def publish_file(self, file_id: str) -> None:
        encoded_id = quote(file_id, safe="")
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files/{encoded_id}/permissions",
            params={
                "supportsAllDrives": "true",
                "fields": "permissions(type,role)",
            },
        )
        response.raise_for_status()
        try:
            permissions = _DrivePermissionList.model_validate(response.json()).permissions
        except ValidationError as exc:
            raise RuntimeError("Google Drive returned invalid permission metadata") from exc
        if any(permission.permission_type == "anyone" and permission.role == "reader" for permission in permissions):
            return
        response = await self._request(
            "POST",
            f"{DRIVE_API}/files/{encoded_id}/permissions",
            params={"supportsAllDrives": "true"},
            headers={"Content-Type": "application/json"},
            json={"type": "anyone", "role": "reader"},
        )
        response.raise_for_status()


class GoogleDriveStatisticsRepository:
    """Read and revise one permanent Drive XLSX without replacing its file resource."""

    def __init__(
        self,
        drive: DriveStorage,
        folder_id: str,
        *,
        file_name: str = ATTENDANCE_FILE_NAME,
        workbook_type: type[StatisticsWorkbook] = StatisticsWorkbook,
    ) -> None:
        if not folder_id.strip():
            raise ValueError("Google Drive folder ID must not be empty")
        if not file_name.strip():
            raise ValueError("Google Drive statistics file name must not be empty")
        self.drive = drive
        self.folder_id = folder_id.strip()
        self.file_name = file_name.strip()
        self.workbook_type = workbook_type

    @classmethod
    def masters(cls, drive: DriveStorage, folder_id: str) -> GoogleDriveStatisticsRepository:
        return cls(
            drive,
            folder_id,
            file_name=MASTER_STATISTICS_FILE_NAME,
            workbook_type=MasterStatisticsWorkbook,
        )

    async def _file(self) -> DriveFile:
        return await self.drive.find_file(self.folder_id, self.file_name)

    async def link(self) -> str:
        file = await self._file()
        return file.web_view_link or f"https://drive.google.com/file/d/{quote(file.file_id, safe='')}/view"

    async def has_any_player(self, names: Collection[str]) -> bool:
        file = await self._file()
        content = await self.drive.download(file.file_id)
        return await asyncio.to_thread(lambda: self.workbook_type(content).has_any_player(tuple(names)))

    async def prepare(self, transform: Callable[[StatisticsWorkbook], bytes | None]) -> PreparedStatisticsEdit:
        file = await self._file()
        content = await self.drive.download(file.file_id)
        result = await asyncio.to_thread(lambda: transform(self.workbook_type(content)))
        return PreparedStatisticsEdit(file=file, content=result)

    async def commit(self, edit: PreparedStatisticsEdit) -> bool:
        if edit.content is None:
            return False
        await self.drive.replace(edit.file.file_id, edit.content)
        return True

    async def edit(self, transform: Callable[[StatisticsWorkbook], bytes | None]) -> bool:
        return await self.commit(await self.prepare(transform))
