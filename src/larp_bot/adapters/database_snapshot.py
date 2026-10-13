from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from botocore.exceptions import ClientError

from larp_bot.adapters.google_drive.apps_script import GoogleDriveAppsScriptClient
from larp_bot.adapters.google_drive.statistics import JSON_MIME_TYPE, DriveFile, GoogleDriveRestClient

GOOGLE_SNAPSHOT_NAME = "rolebot-database-snapshot.json"
GOOGLE_BOOTSTRAP_NAME = "events — rolebot database snapshot bootstrap.xlsx"
YANDEX_SNAPSHOT_PATH = "disk:/larp-bot/rolebot-database-snapshot.json"
NTFY_URL = "https://ntfy.sh/"
TOPIC_PATTERN = re.compile(r"[-_A-Za-z0-9]{1,64}\Z")


class SsmClient(Protocol):
    def get_parameter(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_parameter(self, **kwargs: Any) -> Mapping[str, Any]: ...


class GoogleDriveSnapshotUploader:
    def __init__(
        self,
        drive: GoogleDriveRestClient,
        bootstrap: GoogleDriveAppsScriptClient,
        folder_id: str,
    ) -> None:
        self.drive = drive
        self.bootstrap = bootstrap
        self.folder_id = folder_id

    @staticmethod
    def _url(file: DriveFile) -> str:
        return f"https://drive.google.com/file/d/{quote(file.file_id, safe='')}/view"

    async def upload(self, content: bytes, *, expected_url: str | None) -> str:
        files = await self.drive.find_named_files(self.folder_id, GOOGLE_SNAPSHOT_NAME)
        if len(files) > 1:
            raise RuntimeError("Google Drive contains duplicate database snapshots")
        if files:
            file = files[0]
            if file.mime_type != JSON_MIME_TYPE:
                raise RuntimeError("Google Drive database snapshot does not have JSON MIME type")
            if expected_url is not None and self._url(file) != expected_url:
                raise RuntimeError("Google Drive database snapshot file ID changed")
            await self.drive.replace_media(file.file_id, content, JSON_MIME_TYPE)
        else:
            if expected_url is not None:
                raise RuntimeError("Google Drive database snapshot with the persisted file ID is missing")
            placeholder = await self.bootstrap.upsert_xlsx(
                self.folder_id,
                GOOGLE_BOOTSTRAP_NAME,
                b"rolebot database snapshot bootstrap",
            )
            file = await self.drive.replace_with_metadata(
                placeholder.file_id,
                name=GOOGLE_SNAPSHOT_NAME,
                mime_type=JSON_MIME_TYPE,
                content=content,
            )
        await self.drive.publish_file(file.file_id)
        return self._url(file)


class YandexDiskSnapshotUploader:
    API = "https://cloud-api.yandex.net/v1/disk"
    MAX_ATTEMPTS = 5
    RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        if not token:
            raise ValueError("Yandex Disk token must not be empty")
        self.client = client
        self.headers = {"Authorization": f"OAuth {token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        allowed_status_codes: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < self.MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise RuntimeError(
                    f"Yandex Disk {operation} failed after {self.MAX_ATTEMPTS} attempts ({type(exc).__name__})"
                ) from exc
            if response.status_code not in self.RETRYABLE_STATUS_CODES or attempt == self.MAX_ATTEMPTS - 1:
                break
            await asyncio.sleep(2**attempt)

        if response.is_error and response.status_code not in allowed_status_codes:
            try:
                error = response.json()
            except ValueError:
                error = None
            description = error.get("description") if isinstance(error, Mapping) else None
            detail = f": {description}" if isinstance(description, str) else ""
            raise RuntimeError(f"Yandex Disk {operation} failed with HTTP {response.status_code}{detail}")
        return response

    async def _metadata(self) -> Mapping[str, Any] | None:
        response = await self._request(
            "GET",
            f"{self.API}/resources",
            operation="metadata lookup",
            allowed_status_codes=frozenset({404}),
            headers=self.headers,
            params={"path": YANDEX_SNAPSHOT_PATH, "fields": "path,public_url"},
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("Yandex Disk returned invalid snapshot metadata")
        return payload

    async def upload(self, content: bytes, *, expected_url: str | None) -> str:
        if expected_url is not None:
            metadata = await self._metadata()
            if metadata is None:
                raise RuntimeError("Yandex Disk database snapshot with the persisted link is missing")
            if metadata.get("public_url") != expected_url:
                raise RuntimeError("Yandex Disk database snapshot public URL changed")

        link_response = await self._request(
            "GET",
            f"{self.API}/resources/upload",
            operation="upload-link request",
            headers=self.headers,
            params={"path": YANDEX_SNAPSHOT_PATH, "overwrite": "true"},
        )
        href = link_response.json().get("href")
        if not isinstance(href, str) or not href.startswith("https://"):
            raise RuntimeError("Yandex Disk did not return an HTTPS snapshot upload URL")

        await self._request(
            "PUT",
            href,
            operation="content upload",
            headers={"Content-Type": JSON_MIME_TYPE},
            content=content,
        )
        await self._request(
            "PUT",
            f"{self.API}/resources/publish",
            operation="publish",
            headers=self.headers,
            params={"path": YANDEX_SNAPSHOT_PATH},
        )

        metadata = await self._metadata()
        public_url = metadata.get("public_url") if metadata is not None else None
        if not isinstance(public_url, str) or not public_url.startswith("https://"):
            raise RuntimeError("Yandex Disk database snapshot has no public URL")
        return public_url


class SsmSnapshotLinkStore:
    def __init__(self, client: SsmClient, parameter_names: Mapping[str, str]) -> None:
        self.client = client
        self.parameter_names = dict(parameter_names)

    def _name(self, destination: str) -> str:
        try:
            return self.parameter_names[destination]
        except KeyError as exc:
            raise ValueError(f"unknown snapshot destination: {destination}") from exc

    async def get(self, destination: str) -> str | None:
        try:
            response = await asyncio.to_thread(self.client.get_parameter, Name=self._name(destination))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise
        parameter = response.get("Parameter")
        value = parameter.get("Value") if isinstance(parameter, Mapping) else None
        if not isinstance(value, str) or not value.startswith("https://"):
            raise RuntimeError(f"persisted {destination} snapshot URL is invalid")
        return value

    async def create(self, destination: str, url: str) -> None:
        if not url.startswith("https://"):
            raise ValueError("snapshot URL must use HTTPS")
        await asyncio.to_thread(
            self.client.put_parameter,
            Name=self._name(destination),
            Description=f"Permanent {destination} URL for the Rolebot canonical database snapshot",
            Type="String",
            Tier="Standard",
            Value=url,
            Overwrite=False,
        )


class NtfySnapshotAlertPublisher:
    def __init__(self, topic: str, client: httpx.AsyncClient) -> None:
        if TOPIC_PATTERN.fullmatch(topic) is None:
            raise ValueError("ntfy topic must contain 1-64 letters, numbers, underscores, or dashes")
        self.topic = topic
        self.client = client

    async def upload_failed(self, destinations: tuple[str, ...], exported_at: str) -> None:
        labels = {"google_drive": "Google Drive", "yandex_disk": "Yandex Disk"}
        failed = ", ".join(labels.get(destination, destination) for destination in destinations)
        response = await self.client.post(
            NTFY_URL,
            json={
                "topic": self.topic,
                "title": "ALARM: database snapshot upload failed",
                "message": f"Failed destinations: {failed}\nSnapshot time (UTC+3): {exported_at}",
                "priority": 5,
                "tags": ["rotating_light", "floppy_disk"],
            },
        )
        response.raise_for_status()
