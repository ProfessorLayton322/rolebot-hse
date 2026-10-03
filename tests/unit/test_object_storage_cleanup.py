from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scripts.cleanup_object_storage import cleanup, cleanup_candidates


def version(key: str, age: timedelta, *, latest: bool, now: datetime) -> dict[str, object]:
    return {
        "Key": key,
        "VersionId": f"version-{key}",
        "LastModified": now - age,
        "IsLatest": latest,
    }


def test_cleanup_candidates_can_preserve_the_latest_state_marker() -> None:
    now = datetime(2026, 10, 3, tzinfo=UTC)
    page = {
        "Versions": [
            version("current", timedelta(days=3), latest=True, now=now),
            version("old", timedelta(days=2), latest=False, now=now),
            version("young", timedelta(hours=2), latest=False, now=now),
        ],
        "DeleteMarkers": [version("marker", timedelta(days=2), latest=False, now=now)],
    }

    candidates = cleanup_candidates([page], cutoff=now - timedelta(days=1), include_latest=False)

    assert {candidate["Key"] for candidate in candidates} == {"old", "marker"}


class FakePaginator:
    def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
        self.pages = pages

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        assert Bucket == "bucket"
        return self.pages.get(Prefix, [])


class FakeClient:
    def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
        self.paginator = FakePaginator(pages)
        self.deleted: list[dict[str, str]] = []

    def get_paginator(self, operation: str) -> FakePaginator:
        assert operation == "list_object_versions"
        return self.paginator

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> dict[str, object]:
        assert Bucket == "bucket"
        self.deleted.extend(Delete["Objects"])  # type: ignore[arg-type]
        return {}


def test_cleanup_deletes_stale_uploads_but_keeps_current_state() -> None:
    now = datetime(2026, 10, 3, tzinfo=UTC)
    client = FakeClient(
        {
            "larp-bot/uploads/": [{"Versions": [version("stale-upload", timedelta(hours=7), latest=True, now=now)]}],
            "larp-bot/upload-locks/": [
                {"DeleteMarkers": [version("old-lock-marker", timedelta(days=2), latest=False, now=now)]}
            ],
            "larp-bot/upload-state/": [
                {
                    "Versions": [
                        version("current-state", timedelta(days=3), latest=True, now=now),
                        version("old-state", timedelta(days=2), latest=False, now=now),
                    ]
                }
            ],
        }
    )

    deleted = cleanup(client, "bucket", now=now)

    assert deleted == 3
    assert {item["Key"] for item in client.deleted} == {"stale-upload", "old-lock-marker", "old-state"}
