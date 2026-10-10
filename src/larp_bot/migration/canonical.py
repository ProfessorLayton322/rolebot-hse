from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from larp_bot.domain.models import Event, Platform, Registration, TelegramUser, VkUser

SCHEMA_VERSION = 1
TABLE_NAMES = ("tg_users", "vk_users", "events", "event_leaders", "registrations")
MAX_DYNAMODB_ITEM_BYTES = 400 * 1024

type CanonicalRow = dict[str, Any]
type CanonicalTables = dict[str, list[CanonicalRow]]
type SourceName = Literal["ydb", "dynamodb"]


class CanonicalDocument(TypedDict):
    schema_version: int
    exported_at: str
    source: SourceName
    tables: CanonicalTables
    counts: dict[str, int]
    hashes: dict[str, str]


class CanonicalValidationError(ValueError):
    """The dump cannot be proved safe and lossless for migration."""


def utc_rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalValidationError("timestamps must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_rfc3339(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalValidationError("non-finite numbers are not valid canonical JSON")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise CanonicalValidationError(f"unsupported canonical value type: {type(value).__name__}")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalValidationError(f"value is not canonical JSON: {exc}") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def canonical_user(user: TelegramUser | VkUser) -> CanonicalRow:
    row = cast(CanonicalRow, _json_value(user.model_dump(mode="python")))
    if isinstance(user, TelegramUser):
        row["tg_id"] = str(user.tg_id)
        if user.linked_vk_id is not None:
            row["linked_vk_id"] = str(user.linked_vk_id)
    else:
        row["vk_id"] = str(user.vk_id)
    return row


def user_from_row(table: str, row: Mapping[str, Any]) -> TelegramUser | VkUser:
    data = deepcopy(dict(row))
    if table == "tg_users":
        data["tg_id"] = _canonical_integer(data.get("tg_id"), "tg_users.tg_id", unsigned=True)
        if data.get("linked_vk_id") is not None:
            data["linked_vk_id"] = _canonical_integer(
                data["linked_vk_id"],
                "tg_users.linked_vk_id",
                unsigned=True,
            )
        model: TelegramUser | VkUser = TelegramUser.model_validate(data)
    elif table == "vk_users":
        data["vk_id"] = _canonical_integer(data.get("vk_id"), "vk_users.vk_id", unsigned=True)
        model = VkUser.model_validate(data)
    else:
        raise CanonicalValidationError(f"invalid user table: {table}")
    if canonical_user(model) != row:
        raise CanonicalValidationError(f"{table} row is not in canonical form")
    return model


def canonical_event(event: Event) -> CanonicalRow:
    row = cast(CanonicalRow, _json_value(event.model_dump(mode="python")))
    if event.telegram_chat_id is not None:
        row["telegram_chat_id"] = str(event.telegram_chat_id)
    return row


def event_from_row(row: Mapping[str, Any]) -> Event:
    data = deepcopy(dict(row))
    if data.get("telegram_chat_id") is not None:
        data["telegram_chat_id"] = _canonical_integer(
            data["telegram_chat_id"],
            "events.telegram_chat_id",
            unsigned=False,
        )
    event = Event.model_validate(data)
    if canonical_event(event) != row:
        raise CanonicalValidationError("events row is not in canonical form")
    return event


def canonical_leader(
    event_id: str,
    platform: Platform,
    platform_user_id: int,
    created_at: datetime,
) -> CanonicalRow:
    return {
        "event_id": event_id,
        "platform": platform.value,
        "platform_user_id": str(platform_user_id),
        "created_at": utc_rfc3339(created_at),
    }


def leader_from_row(row: Mapping[str, Any]) -> tuple[str, Platform, int, datetime]:
    expected = {"event_id", "platform", "platform_user_id", "created_at"}
    if set(row) != expected:
        raise CanonicalValidationError("event_leaders row has unexpected fields")
    event_id = row.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise CanonicalValidationError("event_leaders.event_id must be a non-empty string")
    try:
        platform = Platform(str(row.get("platform")))
    except ValueError as exc:
        raise CanonicalValidationError("event_leaders.platform is invalid") from exc
    user_id = _canonical_integer(
        row.get("platform_user_id"),
        "event_leaders.platform_user_id",
        unsigned=True,
    )
    raw_created_at = row.get("created_at")
    if not isinstance(raw_created_at, str):
        raise CanonicalValidationError("event_leaders.created_at must be a UTC RFC3339 string")
    try:
        created_at = datetime.fromisoformat(raw_created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalValidationError("event_leaders.created_at must be a UTC RFC3339 string") from exc
    if canonical_leader(event_id, platform, user_id, created_at) != row:
        raise CanonicalValidationError("event_leaders row is not in canonical form")
    return event_id, platform, user_id, created_at


def canonical_registration(
    registration: Registration,
    participation_deliveries: Sequence[str] = (),
) -> CanonicalRow:
    deliveries = sorted(set(participation_deliveries))
    if len(deliveries) != len(participation_deliveries) or not all(
        isinstance(item, str) and item for item in participation_deliveries
    ):
        raise CanonicalValidationError("participation deliveries must be unique non-empty strings")
    row = cast(CanonicalRow, _json_value(registration.model_dump(mode="python")))
    row["participation_notification_deliveries"] = deliveries
    return row


def registration_from_row(row: Mapping[str, Any]) -> tuple[Registration, list[str]]:
    data = deepcopy(dict(row))
    deliveries = data.pop("participation_notification_deliveries", None)
    if not isinstance(deliveries, list) or not all(isinstance(item, str) and item for item in deliveries):
        raise CanonicalValidationError(
            "registrations.participation_notification_deliveries must be a list of non-empty strings"
        )
    if deliveries != sorted(set(deliveries)):
        raise CanonicalValidationError("registration participation deliveries must be sorted and unique")
    registration = Registration.model_validate(data)
    if canonical_registration(registration, deliveries) != row:
        raise CanonicalValidationError("registrations row is not in canonical form")
    return registration, deliveries


def _canonical_integer(value: Any, field: str, *, unsigned: bool) -> int:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CanonicalValidationError(f"{field} must be a canonical decimal string")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CanonicalValidationError(f"{field} must be a canonical decimal string") from exc
    bounds = (1, 2**64 - 1) if unsigned else (-(2**63), 2**63 - 1)
    if str(parsed) != value or not bounds[0] <= parsed <= bounds[1]:
        raise CanonicalValidationError(f"{field} must be a canonical decimal string")
    return parsed


def row_key(table: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    if table == "tg_users":
        return (str(row["tg_id"]),)
    if table == "vk_users":
        return (str(row["vk_id"]),)
    if table == "events":
        return (str(row["event_id"]),)
    if table == "event_leaders":
        return str(row["event_id"]), str(row["platform"]), str(row["platform_user_id"])
    if table == "registrations":
        return str(row["event_id"]), str(row["participant_key"])
    raise CanonicalValidationError(f"unknown canonical table: {table}")


def _canonicalize_table(table: str, rows: Sequence[Mapping[str, Any]]) -> list[CanonicalRow]:
    canonical: list[CanonicalRow] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise CanonicalValidationError(f"{table}[{index}] must be an object")
        try:
            if table in {"tg_users", "vk_users"}:
                canonical.append(canonical_user(user_from_row(table, row)))
            elif table == "events":
                canonical.append(canonical_event(event_from_row(row)))
            elif table == "event_leaders":
                canonical.append(canonical_leader(*leader_from_row(row)))
            elif table == "registrations":
                registration, deliveries = registration_from_row(row)
                canonical.append(canonical_registration(registration, deliveries))
            else:
                raise CanonicalValidationError(f"unknown canonical table: {table}")
        except CanonicalValidationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CanonicalValidationError(f"invalid {table}[{index}]: {exc}") from exc
    canonical.sort(key=lambda item: row_key(table, item))
    keys = [row_key(table, item) for item in canonical]
    duplicate = next((key for position, key in enumerate(keys[1:], 1) if key == keys[position - 1]), None)
    if duplicate is not None:
        raise CanonicalValidationError(f"duplicate primary key in {table}: {duplicate!r}")
    return canonical


def _validate_references(tables: CanonicalTables) -> None:
    event_ids = {row["event_id"] for row in tables["events"]}
    for table in ("event_leaders", "registrations"):
        for row in tables[table]:
            if row["event_id"] not in event_ids:
                raise CanonicalValidationError(f"{table} row references missing event {row['event_id']!r}")

    for field in ("signup_token", "telegram_chat_id"):
        owners: dict[str, str] = {}
        for row in tables["events"]:
            value = row.get(field)
            if value is None:
                continue
            previous = owners.setdefault(str(value), str(row["event_id"]))
            if previous != row["event_id"]:
                raise CanonicalValidationError(f"duplicate events.{field}: {value!r}")


def build_document(
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    source: SourceName,
    exported_at: datetime | None = None,
) -> CanonicalDocument:
    if set(tables) != set(TABLE_NAMES):
        missing = sorted(set(TABLE_NAMES) - set(tables))
        unexpected = sorted(set(tables) - set(TABLE_NAMES))
        raise CanonicalValidationError(f"invalid table set; missing={missing}, unexpected={unexpected}")
    normalized = {table: _canonicalize_table(table, tables[table]) for table in TABLE_NAMES}
    _validate_references(normalized)
    counts = {table: len(normalized[table]) for table in TABLE_NAMES}
    table_hashes = {table: _sha256(normalized[table]) for table in TABLE_NAMES}
    payload = {"schema_version": SCHEMA_VERSION, "tables": normalized, "counts": counts}
    hashes = table_hashes | {"payload": _sha256(payload)}
    document: CanonicalDocument = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utc_rfc3339(exported_at or datetime.now(UTC)),
        "source": source,
        "tables": normalized,
        "counts": counts,
        "hashes": hashes,
    }
    validate_dynamodb_item_sizes(document)
    return document


def validate_document(raw: Mapping[str, Any]) -> CanonicalDocument:
    expected_fields = {"schema_version", "exported_at", "source", "tables", "counts", "hashes"}
    if set(raw) != expected_fields:
        raise CanonicalValidationError("canonical document has unexpected or missing top-level fields")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise CanonicalValidationError(f"unsupported schema_version: {raw.get('schema_version')!r}")
    source = raw.get("source")
    if source not in {"ydb", "dynamodb"}:
        raise CanonicalValidationError("source must be 'ydb' or 'dynamodb'")
    exported_at = raw.get("exported_at")
    if not isinstance(exported_at, str):
        raise CanonicalValidationError("exported_at must be a UTC RFC3339 string")
    try:
        parsed_exported_at = datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalValidationError("exported_at must be a UTC RFC3339 string") from exc
    if utc_rfc3339(parsed_exported_at) != exported_at:
        raise CanonicalValidationError("exported_at must use canonical UTC microsecond precision")
    tables = raw.get("tables")
    if not isinstance(tables, Mapping):
        raise CanonicalValidationError("tables must be an object")
    rebuilt = build_document(
        cast(Mapping[str, Sequence[Mapping[str, Any]]], tables),
        source=cast(SourceName, source),
        exported_at=parsed_exported_at,
    )
    if raw.get("counts") != rebuilt["counts"]:
        raise CanonicalValidationError("canonical counts do not match table contents")
    if raw.get("hashes") != rebuilt["hashes"]:
        raise CanonicalValidationError("canonical hashes do not match table contents")
    if raw.get("tables") != rebuilt["tables"]:
        raise CanonicalValidationError("canonical tables are not deterministically sorted")
    return rebuilt


def load_document(path: Path) -> CanonicalDocument:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalValidationError(f"cannot read canonical dump: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CanonicalValidationError("canonical document must be an object")
    return validate_document(raw)


def write_document(path: Path, document: CanonicalDocument) -> None:
    validated = validate_document(document)
    if path.exists():
        raise CanonicalValidationError(f"refusing to overwrite existing dump: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise CanonicalValidationError(
            f"dump directory permissions are not private: {path.parent}; use a filesystem that supports chmod 0700"
        )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
            raise CanonicalValidationError(
                f"dump file permissions cannot be made private on this filesystem: {path.parent}"
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def require_private_dump(path: Path, dump_root: Path) -> Path:
    resolved_root = dump_root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise CanonicalValidationError(f"dump path must be inside {resolved_root}")
    if resolved.exists() and stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise CanonicalValidationError("dump file must not be accessible by group or other users")
    return resolved


def compare_documents(expected: CanonicalDocument, actual: CanonicalDocument) -> None:
    for table in TABLE_NAMES:
        if expected["counts"][table] != actual["counts"][table]:
            raise CanonicalValidationError(
                f"round-trip count mismatch for {table}: {expected['counts'][table]} != {actual['counts'][table]}"
            )
        if expected["hashes"][table] != actual["hashes"][table]:
            raise CanonicalValidationError(f"round-trip hash mismatch for {table}")
    if expected["hashes"]["payload"] != actual["hashes"]["payload"]:
        raise CanonicalValidationError("round-trip canonical payload hash mismatch")


def dynamodb_physical_items(document: CanonicalDocument) -> CanonicalTables:
    tables: CanonicalTables = {table: [] for table in TABLE_NAMES}
    for row in document["tables"]["tg_users"]:
        user = user_from_row("tg_users", row)
        assert isinstance(user, TelegramUser)
        indexes = {
            **({"telegram_handle": user.telegram_handle} if user.telegram_handle is not None else {}),
            **({"linked_vk_id": str(user.linked_vk_id)} if user.linked_vk_id is not None else {}),
        }
        tables["tg_users"].append(
            {"tg_id": str(user.tg_id), **indexes, "record_type": "user", "payload": user.model_dump_json()}
        )
    for row in document["tables"]["vk_users"]:
        user = user_from_row("vk_users", row)
        assert isinstance(user, VkUser)
        tables["vk_users"].append({"vk_id": str(user.vk_id), "record_type": "user", "payload": user.model_dump_json()})
    for row in document["tables"]["events"]:
        event = event_from_row(row)
        indexes = {
            **({"signup_token": event.signup_token} if event.signup_token is not None else {}),
            **({"telegram_chat_id": str(event.telegram_chat_id)} if event.telegram_chat_id is not None else {}),
        }
        tables["events"].append(
            {"event_id": event.event_id, **indexes, "record_type": "event", "payload": event.model_dump_json()}
        )
        for kind, value in (("signup", event.signup_token), ("telegram-chat", event.telegram_chat_id)):
            if value is not None:
                tables["events"].append(
                    {
                        "event_id": f"lock:{kind}:{value}",
                        "record_type": "unique_lock",
                        "owner_event_id": event.event_id,
                    }
                )
    for row in document["tables"]["event_leaders"]:
        event_id, platform, user_id, created_at = leader_from_row(row)
        tables["event_leaders"].append(
            {
                "event_id": event_id,
                "leader_key": f"{platform.value}:{user_id}",
                "record_type": "event_leader",
                "platform": platform.value,
                "platform_user_id": str(user_id),
                "created_at": utc_rfc3339(created_at),
            }
        )
    for row in document["tables"]["registrations"]:
        registration, deliveries = registration_from_row(row)
        indexes = (
            {"telegram_profile": registration.telegram_profile} if registration.telegram_profile is not None else {}
        )
        tables["registrations"].append(
            {
                "event_id": registration.event_id,
                "participant_key": registration.participant_key,
                **indexes,
                "record_type": "registration",
                "payload": registration.model_dump_json(),
                "participation_deliveries": deliveries,
            }
        )
    return tables


def _dynamodb_value_size(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, (int, float)):
        digits = len(str(value).replace("-", "").replace(".", ""))
        return max(1, (digits + 1) // 2 + 1)
    if isinstance(value, list):
        return 3 + len(value) + sum(_dynamodb_value_size(item) for item in value)
    if isinstance(value, Mapping):
        return (
            3
            + len(value)
            + sum(len(str(key).encode("utf-8")) + _dynamodb_value_size(item) for key, item in value.items())
        )
    raise CanonicalValidationError(f"unsupported DynamoDB value type: {type(value).__name__}")


def dynamodb_item_size(item: Mapping[str, Any]) -> int:
    return sum(len(key.encode("utf-8")) + _dynamodb_value_size(value) for key, value in item.items())


def validate_dynamodb_item_sizes(document: CanonicalDocument) -> None:
    for table, items in dynamodb_physical_items(document).items():
        for item in items:
            size = dynamodb_item_size(item | {"_revision": 1})
            if size > MAX_DYNAMODB_ITEM_BYTES:
                key = next(iter(item.values()), "unknown")
                raise CanonicalValidationError(
                    f"{table} item {key!r} is {size} bytes; DynamoDB maximum is {MAX_DYNAMODB_ITEM_BYTES}"
                )
