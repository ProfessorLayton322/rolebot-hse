from __future__ import annotations

from datetime import datetime

import pytest

from larp_bot.application.deadlines import (
    MOSCOW_TIMEZONE,
    closest_thursday_19,
    format_confirmation_deadline,
    parse_confirmation_deadline,
)


@pytest.mark.parametrize(
    "value",
    ["9.09.26 19:00", "09.9.26 19:00", "09.09.2026 19:00", "09.09.26 7:00", "31.02.26 19:00"],
)
def test_deadline_parser_requires_exact_format_and_valid_datetime(value: str) -> None:
    with pytest.raises(ValueError):
        parse_confirmation_deadline(value)


def test_deadline_round_trip_uses_moscow_time() -> None:
    parsed = parse_confirmation_deadline("10.09.26 19:00")
    assert parsed.tzinfo == MOSCOW_TIMEZONE
    assert format_confirmation_deadline(parsed) == "10.09.26 19:00"


def test_nearest_thursday_includes_today_and_sets_1900() -> None:
    thursday = datetime(2026, 9, 3, 21, 30, tzinfo=MOSCOW_TIMEZONE)
    wednesday = datetime(2026, 9, 2, 21, 30, tzinfo=MOSCOW_TIMEZONE)

    assert closest_thursday_19(thursday) == datetime(2026, 9, 3, 19, tzinfo=MOSCOW_TIMEZONE)
    assert closest_thursday_19(wednesday) == datetime(2026, 9, 3, 19, tzinfo=MOSCOW_TIMEZONE)
