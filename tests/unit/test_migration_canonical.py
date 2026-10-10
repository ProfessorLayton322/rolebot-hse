from __future__ import annotations

import json
import stat
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone

import pytest

from larp_bot.domain.models import Event, Platform, Registration, TelegramUser, VkUser
from larp_bot.migration.canonical import (
    CanonicalValidationError,
    build_document,
    canonical_event,
    canonical_leader,
    canonical_registration,
    canonical_user,
    compare_documents,
    load_document,
    validate_document,
    write_document,
)


def sample_tables() -> dict[str, list[dict[str, object]]]:
    created = datetime(2026, 1, 2, 3, 4, 5, 6789, tzinfo=timezone(timedelta(hours=3)))
    event = Event(
        event_id="event-a1",
        name="Лесной предел",
        disk_resource_path="disk:/larp-bot/events/event-a1.xlsx",
        public_registration_url="https://example.test/event",
        signup_token="forest_1",
        telegram_chat_id=-1001234567890,
        created_at=created,
        updated_at=created,
    )
    registration = Registration(
        event_id=event.event_id,
        participant_key="a" * 43,
        display_name="Игрок",
        wish_play="Следопыт",
        created_at=created,
        updated_at=created,
    )
    return {
        "tg_users": [
            canonical_user(
                TelegramUser(
                    tg_id=9223372036854775807,
                    linked_vk_id=9007199254740993,
                    created_at=created,
                    updated_at=created,
                )
            )
        ],
        "vk_users": [canonical_user(VkUser(vk_id=9007199254740993, created_at=created, updated_at=created))],
        "events": [canonical_event(event)],
        "event_leaders": [canonical_leader(event.event_id, Platform.TELEGRAM, 9223372036854775807, created)],
        "registrations": [canonical_registration(registration, ["delivery-1"])],
    }


def test_canonical_document_is_deterministic_and_hashes_ignore_export_metadata() -> None:
    tables = sample_tables()
    for rows in tables.values():
        rows.reverse()
    first = build_document(tables, source="ydb", exported_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = build_document(
        deepcopy(tables),
        source="dynamodb",
        exported_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    compare_documents(first, second)
    assert first["tables"]["tg_users"][0]["tg_id"] == "9223372036854775807"
    assert first["tables"]["tg_users"][0]["linked_vk_id"] == "9007199254740993"
    assert first["tables"]["events"][0]["telegram_chat_id"] == "-1001234567890"
    assert first["tables"]["events"][0]["created_at"] == "2026-01-02T00:04:05.006789Z"
    assert len(first["hashes"]["payload"]) == 64


@pytest.mark.parametrize("table", ["tg_users", "vk_users", "events", "event_leaders", "registrations"])
def test_duplicate_primary_keys_are_rejected(table: str) -> None:
    tables = sample_tables()
    tables[table].append(deepcopy(tables[table][0]))

    with pytest.raises(CanonicalValidationError, match="duplicate primary key"):
        build_document(tables, source="ydb")


def test_foreign_keys_and_application_models_are_validated() -> None:
    tables = sample_tables()
    tables["registrations"][0]["event_id"] = "missing-event"
    with pytest.raises(CanonicalValidationError, match="references missing event"):
        build_document(tables, source="ydb")

    tables = sample_tables()
    tables["tg_users"][0]["tg_id"] = 123
    with pytest.raises(CanonicalValidationError, match="canonical decimal string"):
        build_document(tables, source="ydb")


def test_tampered_counts_hashes_and_sorting_are_rejected() -> None:
    document = build_document(sample_tables(), source="ydb")
    tampered = deepcopy(document)
    tampered["counts"]["events"] = 10
    with pytest.raises(CanonicalValidationError, match="counts"):
        validate_document(tampered)

    tampered = deepcopy(document)
    tampered["hashes"]["events"] = "0" * 64
    with pytest.raises(CanonicalValidationError, match="hashes"):
        validate_document(tampered)


def test_dynamodb_item_size_is_checked_before_migration() -> None:
    tables = sample_tables()
    tables["tg_users"][0]["dialog_context"] = {"oversized": "я" * 205_000}

    with pytest.raises(CanonicalValidationError, match="DynamoDB maximum"):
        build_document(tables, source="ydb")


def test_dump_is_written_with_restrictive_permissions(tmp_path) -> None:
    document = build_document(sample_tables(), source="ydb")
    path = tmp_path / "private" / "dump.json"

    write_document(path, document)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_document(path) == document
    assert json.loads(path.read_text(encoding="utf-8"))["hashes"]["payload"] == document["hashes"]["payload"]
