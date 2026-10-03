"""Permanently remove obsolete versions from the shared deployment bucket."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

STORAGE_ENDPOINT = "https://storage.yandexcloud.net"
UPLOAD_MAX_AGE = timedelta(hours=6)
STATE_VERSION_MAX_AGE = timedelta(days=1)
UPLOAD_PREFIXES = ("larp-bot/uploads/", "larp-bot/upload-locks/")
STATE_PREFIX = "larp-bot/upload-state/"


def cleanup_candidates(
    pages: Iterable[Mapping[str, Any]],
    *,
    cutoff: datetime,
    include_latest: bool,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for page in pages:
        entries = [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
        for entry in entries:
            if entry["LastModified"] > cutoff or (entry.get("IsLatest", False) and not include_latest):
                continue
            candidates.append({"Key": str(entry["Key"]), "VersionId": str(entry["VersionId"])})
    return candidates


def listed_versions(client: Any, bucket: str, prefix: str) -> Iterable[Mapping[str, Any]]:
    paginator = client.get_paginator("list_object_versions")
    return paginator.paginate(Bucket=bucket, Prefix=prefix)


def delete_versions(client: Any, bucket: str, objects: list[dict[str, str]]) -> int:
    deleted = 0
    for offset in range(0, len(objects), 1000):
        batch = objects[offset : offset + 1000]
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        errors = response.get("Errors", [])
        if errors:
            raise RuntimeError(f"Object Storage rejected version cleanup: {errors}")
        deleted += len(batch)
    return deleted


def cleanup(client: Any, bucket: str, *, now: datetime) -> int:
    candidates: list[dict[str, str]] = []
    for prefix in UPLOAD_PREFIXES:
        candidates.extend(
            cleanup_candidates(
                listed_versions(client, bucket, prefix),
                cutoff=now - UPLOAD_MAX_AGE,
                include_latest=True,
            )
        )
    candidates.extend(
        cleanup_candidates(
            listed_versions(client, bucket, STATE_PREFIX),
            cutoff=now - STATE_VERSION_MAX_AGE,
            include_latest=False,
        )
    )
    return delete_versions(client, bucket, candidates)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    client = boto3.client("s3", endpoint_url=STORAGE_ENDPOINT)
    deleted = cleanup(client, args.bucket, now=datetime.now(UTC))
    print(f"Deleted {deleted} obsolete Object Storage versions")


if __name__ == "__main__":
    main()
