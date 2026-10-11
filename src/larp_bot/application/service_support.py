from __future__ import annotations

import logging
import re
import secrets
import string
from dataclasses import dataclass
from datetime import date

from larp_bot.domain.models import (
    AttendanceStatus,
    Button,
    Event,
    Registration,
    format_game_date,
)

LOGGER = logging.getLogger("larp_bot.application.services")

_CYRILLIC_TRANSLITERATION = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
_START_PARAMETER_ALPHABET = string.ascii_letters + string.digits
_MAX_START_PARAMETER_LENGTH = 64
_GAME_TIME_PERIOD_RE = re.compile(r"(?<!\d)([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)(?!\d)")


class DomainError(RuntimeError):
    pass


class EventNotFound(DomainError):
    pass


class RegistrationNotFound(DomainError):
    pass


class OperationNotAllowed(DomainError):
    pass


def same_day_signup_warning(game_date: date) -> str:
    return (
        "Важно: вы уже записаны на другую игру, которая проводится "
        f"{format_game_date(game_date)}. Вы сможете подтвердить своё участие только "
        "в одной игре, когда решите, в какую именно хотите играть."
    )


def _game_time_period(event_name: str) -> tuple[int, int] | None:
    """Extract a valid same-day time period from an event name as minutes."""

    match = _GAME_TIME_PERIOD_RE.search(event_name)
    if match is None:
        return None
    start_hour, start_minute, end_hour, end_minute = (int(value) for value in match.groups())
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if end <= start:
        return None
    return start, end


def confirmation_times_conflict(first: Event, second: Event) -> bool:
    """Return whether same-day confirmations must remain mutually exclusive."""

    first_period = _game_time_period(first.name)
    second_period = _game_time_period(second.name)
    if first_period is None or second_period is None:
        return True
    first_start, first_end = first_period
    second_start, second_end = second_period
    return max(first_start, second_start) < min(first_end, second_end)


@dataclass(frozen=True)
class MutationResult:
    text: str
    notifications_enabled: bool = True
    buttons: tuple[Button, ...] | None = None
    replaced_event_id: str | None = None


SIFT_PROMOTED_PREFIX = "promoted:"
SIFT_DEMOTED_PREFIX = "demoted:"


def _sift_snapshot_entry(prefix: str, participant_key: str) -> str:
    return f"{prefix}{participant_key}"


def _parse_sift_snapshot_entry(value: str) -> tuple[str, str] | None:
    for prefix in (SIFT_PROMOTED_PREFIX, SIFT_DEMOTED_PREFIX):
        if value.startswith(prefix):
            return prefix, value.removeprefix(prefix)
    return None


# A player's cancellation keeps the attendance status the player had before
# cancelling in the per-event retry snapshot, and records each master who has
# been told about it as a delivered entry.
CANCELLED_CONFIRMED_PREFIX = "cancelled-confirmed:"
CANCELLED_WAITING_PREFIX = "cancelled-waiting:"
CANCELLATION_MASTER_PREFIX = "master:"


def _cancellation_snapshot_entry(registration: Registration) -> str:
    confirmed = registration.attendance_status is AttendanceStatus.CONFIRMED
    prefix = CANCELLED_CONFIRMED_PREFIX if confirmed else CANCELLED_WAITING_PREFIX
    return f"{prefix}{registration.participant_key}"


def _parse_cancellation_snapshot_entry(value: str) -> tuple[bool, str] | None:
    """Return whether the cancelled player had confirmed, and their participant key."""

    for prefix, confirmed in ((CANCELLED_CONFIRMED_PREFIX, True), (CANCELLED_WAITING_PREFIX, False)):
        if value.startswith(prefix):
            return confirmed, value.removeprefix(prefix)
    return None


def event_slug(name: str) -> str:
    transliterated = name.casefold().replace("ё", "е")
    slug = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")
    return (slug or "game")[:40]


def event_table_paths(event_id: str, name: str) -> tuple[str, str]:
    """Build unique Disk paths while keeping the requested game name in each filename."""

    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", name.strip())
    safe_name = re.sub(r"\s+", " ", safe_name).strip(" .")[:120].rstrip(" .")
    if not safe_name:
        safe_name = event_slug(name)
    directory = f"disk:/larp-bot/events/{event_id}"
    return (
        f"{directory}/master_table_{safe_name}.xlsx",
        f"{directory}/public_table_{safe_name}.xlsx",
    )


def daily_pass_table_path(game_date: date) -> str:
    return f"disk:/larp-bot/passes/daily-{game_date.isoformat()}.xlsx"


def game_signup_token(name: str, game_date: date, *, random_suffix: str | None = None) -> str:
    """Build a readable token that is valid as a Telegram start parameter."""

    characters: list[str] = []
    for character in name.strip():
        replacement = _CYRILLIC_TRANSLITERATION.get(character.casefold())
        if replacement is not None:
            characters.append(replacement.capitalize() if character.isupper() else replacement)
        elif character.isascii() and character.isalnum():
            characters.append(character)
        else:
            characters.append("_")
    transliterated = "".join(characters)
    name_part = re.sub(r"_+", "_", transliterated).strip("_") or "game"
    suffix = (
        random_suffix
        if random_suffix is not None
        else "".join(secrets.choice(_START_PARAMETER_ALPHABET) for _ in range(3))
    )
    if len(suffix) != 3 or any(character not in _START_PARAMETER_ALPHABET for character in suffix):
        raise ValueError("signup token suffix must contain three alphanumeric characters")
    tail = f"_{game_date:%d_%m_%Y}_{suffix}"
    name_part = name_part[: _MAX_START_PARAMETER_LENGTH - len(tail)].rstrip("_") or "game"
    return f"{name_part}{tail}"
