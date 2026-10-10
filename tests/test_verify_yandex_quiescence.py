from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scripts.verify_yandex_quiescence import object_counts


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self, **kwargs: str) -> list[dict[str, Any]]:
        assert kwargs == {"Bucket": "bucket", "Prefix": "prefix/"}
        return self.pages


class _Storage:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def get_paginator(self, operation: str) -> _Paginator:
        assert operation == "list_objects_v2"
        return _Paginator(self.pages)


def test_object_counts_separates_pending_and_abandoned_uploads() -> None:
    cutoff = datetime(2026, 10, 10, 5, tzinfo=UTC)
    storage = _Storage(
        [
            {
                "Contents": [
                    {"LastModified": cutoff - timedelta(seconds=1)},
                    {"LastModified": cutoff},
                    {"LastModified": cutoff + timedelta(minutes=1)},
                ]
            },
            {"Contents": [{}, {"LastModified": "invalid"}]},
        ]
    )

    assert object_counts(storage, "bucket", "prefix/", cutoff=cutoff) == (4, 1)


def test_object_counts_handles_empty_pages() -> None:
    storage = _Storage([{}, {"Contents": []}])

    assert object_counts(storage, "bucket", "prefix/", cutoff=datetime.now(UTC)) == (0, 0)
