from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

import boto3
import httpx
from botocore.config import Config

from larp_bot.adapters.database_snapshot import (
    GoogleDriveSnapshotUploader,
    NtfySnapshotAlertPublisher,
    SsmSnapshotLinkStore,
    YandexDiskSnapshotUploader,
)
from larp_bot.adapters.google_drive.apps_script import GoogleDriveAppsScriptClient
from larp_bot.adapters.google_drive.statistics import GoogleDriveRestClient
from larp_bot.application.database_snapshot import DatabaseSnapshotService
from larp_bot.migration.dynamodb import DynamoMigrationStore, DynamoTableNames

AWS_CONFIG = Config(retries={"mode": "adaptive", "total_max_attempts": 20})
SECRET_KEYS = (
    "google-service-account-json",
    "google-apps-script-hmac-secret",
    "yandex-disk-token",
)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _secrets(ssm: Any, prefix: str) -> dict[str, str]:
    names = [f"{prefix}{key}" for key in SECRET_KEYS]
    response = ssm.get_parameters(Names=names, WithDecryption=True)
    invalid = response.get("InvalidParameters", [])
    values = {parameter["Name"].removeprefix(prefix): parameter["Value"] for parameter in response["Parameters"]}
    if invalid or set(values) != set(SECRET_KEYS):
        raise RuntimeError("database snapshot runtime secrets are incomplete")
    return values


async def _run() -> dict[str, object]:
    ssm = boto3.client("ssm", config=AWS_CONFIG)
    prefix = _required_env("AWS_PARAMETER_PREFIX")
    secrets = await asyncio.to_thread(_secrets, ssm, prefix)
    exporter = DynamoMigrationStore(
        boto3.client("dynamodb", config=AWS_CONFIG),
        DynamoTableNames(
            tg_users=_required_env("DYNAMODB_TG_USERS_TABLE"),
            vk_users=_required_env("DYNAMODB_VK_USERS_TABLE"),
            events=_required_env("DYNAMODB_EVENTS_TABLE"),
            event_leaders=_required_env("DYNAMODB_EVENT_LEADERS_TABLE"),
            registrations=_required_env("DYNAMODB_REGISTRATIONS_TABLE"),
        ),
    )
    folder_id = _required_env("GOOGLE_FOLDER_ID")
    drive = GoogleDriveRestClient(secrets["google-service-account-json"])
    bootstrap = GoogleDriveAppsScriptClient(
        _required_env("GOOGLE_APPS_SCRIPT_URL"),
        secrets["google-apps-script-hmac-secret"],
        folder_id,
    )
    links = SsmSnapshotLinkStore(
        ssm,
        {
            "google_drive": _required_env("DATABASE_SNAPSHOT_GOOGLE_URL_PARAMETER"),
            "yandex_disk": _required_env("DATABASE_SNAPSHOT_YANDEX_URL_PARAMETER"),
        },
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        service = DatabaseSnapshotService(
            exporter,
            {
                "google_drive": GoogleDriveSnapshotUploader(drive, bootstrap, folder_id),
                "yandex_disk": YandexDiskSnapshotUploader(secrets["yandex-disk-token"], http),
            },
            links,
            NtfySnapshotAlertPublisher(_required_env("NTFY_TOPIC"), http),
            required_destinations=frozenset({"google_drive"}),
        )
        result = await service.run()
    return {
        "exported_at": result.exported_at,
        "payload_sha256": result.payload_sha256,
        "google_drive_url": result.urls["google_drive"],
        "yandex_disk_url": result.urls["yandex_disk"],
    }


def handler(event: Mapping[str, Any], context: Any) -> dict[str, object]:
    del event, context
    return asyncio.run(_run())
