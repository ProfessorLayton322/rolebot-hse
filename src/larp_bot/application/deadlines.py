from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEADLINE_FORMAT = "%d.%m.%y %H:%M"
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
NEAREST_THURSDAY = "Ближайший четверг 19:00"
NEAREST_FRIDAY = "Ближайшая пятница 18:00"
NEXT_SATURDAY = "Следующая суббота"
NEXT_NIGHT_03 = "03:00 ночи после игры"
TUESDAY_BEFORE_GAME_18 = "Вторник перед игрой 18:00"
_STRICT_DEADLINE = re.compile(r"\d{2}\.\d{2}\.\d{2} \d{2}:\d{2}")


def parse_confirmation_deadline(value: str) -> datetime:
    candidate = value.strip()
    if _STRICT_DEADLINE.fullmatch(candidate) is None:
        raise ValueError("invalid deadline format")
    return datetime.strptime(candidate, DEADLINE_FORMAT).replace(tzinfo=MOSCOW_TIMEZONE)


def parse_scheduled_action_time(value: str) -> datetime:
    scheduled_at = parse_confirmation_deadline(value)
    if scheduled_at.minute != 0:
        raise ValueError("scheduled action minutes must be zero")
    return scheduled_at


def format_confirmation_deadline(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline must include a timezone")
    return value.astimezone(MOSCOW_TIMEZONE).strftime(DEADLINE_FORMAT)


def closest_thursday_19(now: datetime | None = None) -> datetime:
    local_now = datetime.now(MOSCOW_TIMEZONE) if now is None else now.astimezone(MOSCOW_TIMEZONE)
    days_until_thursday = (3 - local_now.weekday()) % 7
    target_date = local_now.date() + timedelta(days=days_until_thursday)
    return datetime.combine(target_date, time(hour=19), tzinfo=MOSCOW_TIMEZONE)


def closest_friday_18(now: datetime | None = None) -> datetime:
    local_now = datetime.now(MOSCOW_TIMEZONE) if now is None else now.astimezone(MOSCOW_TIMEZONE)
    days_until_friday = (4 - local_now.weekday()) % 7
    candidate = datetime.combine(
        local_now.date() + timedelta(days=days_until_friday),
        time(hour=18),
        tzinfo=MOSCOW_TIMEZONE,
    )
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate


def next_saturday_game_date(now: datetime | None = None) -> date:
    local_now = datetime.now(MOSCOW_TIMEZONE) if now is None else now.astimezone(MOSCOW_TIMEZONE)
    days_until_closest_saturday = (5 - local_now.weekday()) % 7
    return local_now.date() + timedelta(days=days_until_closest_saturday + 7)


def next_night_03(game_date: date) -> datetime:
    """Return 03:00 Moscow time on the calendar day after a game."""

    return datetime.combine(game_date + timedelta(days=1), time(hour=3), tzinfo=MOSCOW_TIMEZONE)


def tuesday_before_game_18(game_date: date) -> datetime:
    """Return 18:00 Moscow time on the Tuesday strictly before a game."""

    days_since_tuesday = (game_date.weekday() - 1) % 7 or 7
    return datetime.combine(
        game_date - timedelta(days=days_since_tuesday),
        time(hour=18),
        tzinfo=MOSCOW_TIMEZONE,
    )
