from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DEADLINE_FORMAT = "%d.%m.%y %H:%M"
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
NEAREST_THURSDAY = "Ближайший четверг 19:00"
_STRICT_DEADLINE = re.compile(r"\d{2}\.\d{2}\.\d{2} \d{2}:\d{2}")


def parse_confirmation_deadline(value: str) -> datetime:
    candidate = value.strip()
    if _STRICT_DEADLINE.fullmatch(candidate) is None:
        raise ValueError("invalid deadline format")
    return datetime.strptime(candidate, DEADLINE_FORMAT).replace(tzinfo=MOSCOW_TIMEZONE)


def format_confirmation_deadline(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline must include a timezone")
    return value.astimezone(MOSCOW_TIMEZONE).strftime(DEADLINE_FORMAT)


def closest_thursday_19(now: datetime | None = None) -> datetime:
    local_now = datetime.now(MOSCOW_TIMEZONE) if now is None else now.astimezone(MOSCOW_TIMEZONE)
    days_until_thursday = (3 - local_now.weekday()) % 7
    target_date = local_now.date() + timedelta(days=days_until_thursday)
    return datetime.combine(target_date, time(hour=19), tzinfo=MOSCOW_TIMEZONE)
