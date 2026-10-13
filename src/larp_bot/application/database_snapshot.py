from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from larp_bot.migration.canonical import CanonicalDocument

UTC_PLUS_THREE = timezone(timedelta(hours=3))


class SnapshotExporter(Protocol):
    async def export_document(self) -> CanonicalDocument: ...


class SnapshotDestination(Protocol):
    async def upload(self, content: bytes, *, expected_url: str | None) -> str: ...


class SnapshotLinkStore(Protocol):
    async def get(self, destination: str) -> str | None: ...

    async def create(self, destination: str, url: str) -> None: ...


class SnapshotAlertPublisher(Protocol):
    async def upload_failed(self, destinations: tuple[str, ...], exported_at: str) -> None: ...


@dataclass(frozen=True)
class SnapshotResult:
    exported_at: str
    payload_sha256: str
    urls: dict[str, str]


class DatabaseSnapshotError(RuntimeError):
    pass


def _utc_plus_three_rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot timestamps must include a timezone")
    return value.astimezone(UTC_PLUS_THREE).isoformat(timespec="microseconds")


def render_snapshot(document: CanonicalDocument, *, exported_at: datetime) -> tuple[bytes, str]:
    snapshot: dict[str, Any] = dict(document)
    timestamp = _utc_plus_three_rfc3339(exported_at)
    snapshot["exported_at"] = timestamp
    return (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(), timestamp


class DatabaseSnapshotService:
    def __init__(
        self,
        exporter: SnapshotExporter,
        destinations: Mapping[str, SnapshotDestination],
        links: SnapshotLinkStore,
        alerts: SnapshotAlertPublisher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not destinations:
            raise ValueError("at least one snapshot destination is required")
        self.exporter = exporter
        self.destinations = dict(destinations)
        self.links = links
        self.alerts = alerts
        self.clock = clock or (lambda: datetime.now(UTC_PLUS_THREE))

    async def _upload_one(self, name: str, destination: SnapshotDestination, content: bytes) -> tuple[str, str]:
        expected_url = await self.links.get(name)
        url = await destination.upload(content, expected_url=expected_url)
        if expected_url is not None and url != expected_url:
            raise DatabaseSnapshotError(f"{name} returned a different permanent URL")
        if expected_url is None:
            await self.links.create(name, url)
        return name, url

    async def run(self) -> SnapshotResult:
        try:
            document = await self.exporter.export_document()
            content, exported_at = render_snapshot(document, exported_at=self.clock())
        except Exception as exc:
            destinations = tuple(sorted(self.destinations))
            timestamp = _utc_plus_three_rfc3339(self.clock())
            await self.alerts.upload_failed(destinations, timestamp)
            raise DatabaseSnapshotError("database snapshot export failed") from exc

        names = tuple(self.destinations)
        outcomes = await asyncio.gather(
            *(self._upload_one(name, self.destinations[name], content) for name in names),
            return_exceptions=True,
        )
        urls: dict[str, str] = {}
        failed: list[str] = []
        causes: list[BaseException] = []
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failed.append(name)
                causes.append(outcome)
            else:
                destination, url = outcome
                urls[destination] = url

        if failed:
            await self.alerts.upload_failed(tuple(failed), exported_at)
            raise DatabaseSnapshotError(f"database snapshot upload failed: {', '.join(failed)}") from causes[0]

        return SnapshotResult(
            exported_at=exported_at,
            payload_sha256=document["hashes"]["payload"],
            urls=urls,
        )
