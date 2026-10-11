from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import requests
from pydantic import BaseModel, ConfigDict, ValidationError

from .statistics import XLSX_MIME_TYPE, DriveFile

SCHEMA_VERSION = 1
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class AppsScriptResponse(Protocol):
    def json(self) -> object: ...

    def raise_for_status(self) -> None: ...


class AppsScriptPost(Protocol):
    def __call__(self, url: str, **kwargs: Any) -> AppsScriptResponse: ...


class _GatewayResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool
    operation: str | None = None
    request_id: str
    error: str | None = None
    message: str | None = None
    exists: bool | None = None
    existed: bool | None = None
    name: str | None = None
    file_id: str | None = None
    url: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None


class GoogleDriveAppsScriptClient:
    """HMAC-authenticated client for the owner-executed Drive web app."""

    def __init__(
        self,
        endpoint_url: str,
        hmac_secret: str,
        expected_folder_id: str,
        *,
        post: AppsScriptPost | None = None,
        clock: Callable[[], float] = time.time,
        request_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        parsed = urlparse(endpoint_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "script.google.com"
            or re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", parsed.path) is None
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Google Apps Script endpoint must be an https://script.google.com/macros/s/.../exec URL")
        if len(hmac_secret) < 32:
            raise ValueError("Google Apps Script HMAC secret must contain at least 32 characters")
        if not expected_folder_id.strip():
            raise ValueError("Google Drive folder ID must not be empty")

        self.endpoint_url = endpoint_url
        self.hmac_secret = hmac_secret
        self.expected_folder_id = expected_folder_id.strip()
        self.post = post or cast(AppsScriptPost, requests.post)
        self.clock = clock
        self.request_id_factory = request_id_factory

    async def _request(self, operation: str, *, name: str = "", content: bytes = b"") -> _GatewayResponse:
        timestamp = int(self.clock())
        request_id = str(self.request_id_factory())
        content_hash = hashlib.sha256(content).hexdigest()
        canonical = "\n".join(
            (
                str(SCHEMA_VERSION),
                str(timestamp),
                request_id,
                operation,
                name,
                content_hash,
            )
        )
        signature = base64.urlsafe_b64encode(
            hmac.digest(self.hmac_secret.encode("utf-8"), canonical.encode("utf-8"), "sha256")
        ).rstrip(b"=")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": timestamp,
            "request_id": request_id,
            "operation": operation,
            "name": name,
            "content_sha256": content_hash,
            "content_base64": base64.b64encode(content).decode("ascii") if content else "",
            "signature": signature.decode("ascii"),
        }

        response = await asyncio.to_thread(
            self.post,
            self.endpoint_url,
            json=payload,
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
        try:
            result = _GatewayResponse.model_validate(response.json())
        except (TypeError, ValueError, ValidationError) as exc:
            raise RuntimeError("Google Drive Apps Script returned an invalid response") from exc
        if result.request_id != request_id:
            raise RuntimeError("Google Drive Apps Script returned a mismatched request ID")
        if not result.ok:
            code = result.error or "unknown_error"
            message = result.message or "request rejected"
            raise RuntimeError(f"Google Drive Apps Script rejected {operation}: {code}: {message}")
        if result.operation != operation:
            raise RuntimeError("Google Drive Apps Script returned a mismatched operation")
        return result

    def _require_folder(self, folder_id: str) -> None:
        if folder_id != self.expected_folder_id:
            raise RuntimeError("Google Drive Apps Script received an unexpected folder ID")

    async def validate_target(self) -> str:
        result = await self._request("health")
        if result.folder_id != self.expected_folder_id or not result.folder_name:
            raise RuntimeError("Google Drive Apps Script is configured for an unexpected folder")
        return result.folder_name

    async def upsert_xlsx(self, folder_id: str, name: str, content: bytes) -> DriveFile:
        self._require_folder(folder_id)
        result = await self._request("upsert_xlsx", name=name, content=content)
        if result.exists is not True or not result.file_id or result.name != name or not result.url:
            raise RuntimeError("Google Drive Apps Script returned incomplete uploaded-file metadata")
        return DriveFile(
            id=result.file_id,
            name=result.name,
            mimeType=XLSX_MIME_TYPE,
            webViewLink=result.url,
        )

    async def delete_named_xlsx(self, folder_id: str, name: str) -> None:
        self._require_folder(folder_id)
        result = await self._request("trash_xlsx", name=name)
        if result.existed is None or result.name != name:
            raise RuntimeError("Google Drive Apps Script returned incomplete delete metadata")

    async def named_xlsx_exists(self, folder_id: str, name: str) -> bool:
        self._require_folder(folder_id)
        result = await self._request("exists_xlsx", name=name)
        if result.exists is None or result.name != name:
            raise RuntimeError("Google Drive Apps Script returned incomplete existence metadata")
        return result.exists

    async def publish_file(self, file_id: str) -> None:
        if not file_id:
            raise ValueError("Google Drive file ID must not be empty")
        # The gateway publishes the file before returning a successful upsert.
