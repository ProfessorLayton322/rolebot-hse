from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from larp_bot.application.deadlines import (
    MOSCOW_TIMEZONE,
    closest_friday_18,
    closest_thursday_19,
    format_confirmation_deadline,
    next_night_03,
    next_saturday_game_date,
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


def test_nearest_friday_uses_today_before_1800_and_next_week_afterwards() -> None:
    friday_before = datetime(2026, 9, 4, 17, 59, tzinfo=MOSCOW_TIMEZONE)
    friday_at_opening = datetime(2026, 9, 4, 18, tzinfo=MOSCOW_TIMEZONE)
    thursday_in_utc = datetime(2026, 9, 3, 22, tzinfo=UTC)

    assert closest_friday_18(friday_before) == datetime(2026, 9, 4, 18, tzinfo=MOSCOW_TIMEZONE)
    assert closest_friday_18(friday_at_opening) == datetime(2026, 9, 11, 18, tzinfo=MOSCOW_TIMEZONE)
    assert closest_friday_18(thursday_in_utc) == datetime(2026, 9, 4, 18, tzinfo=MOSCOW_TIMEZONE)


def test_next_saturday_is_one_week_after_closest_saturday_in_moscow() -> None:
    friday_in_utc_but_saturday_in_moscow = datetime(2026, 9, 11, 22, tzinfo=UTC)
    sunday_in_moscow = datetime(2026, 9, 13, 12, tzinfo=MOSCOW_TIMEZONE)

    assert next_saturday_game_date(friday_in_utc_but_saturday_in_moscow) == date(2026, 9, 19)
    assert next_saturday_game_date(sunday_in_moscow) == date(2026, 9, 26)


def test_next_night_is_three_am_moscow_on_day_after_game() -> None:
    assert next_night_03(date(2026, 11, 14)) == datetime(2026, 11, 15, 3, tzinfo=MOSCOW_TIMEZONE)
