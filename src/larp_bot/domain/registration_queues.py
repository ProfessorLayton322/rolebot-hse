"""Shared queue rules for ordinary games and optional registration answers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .models import AttendanceStatus, Event, Registration


def queue_key(row: Registration) -> tuple[datetime, str]:
    return row.answer_queue_at or row.created_at, row.participant_key


def active_queue(rows: Sequence[Registration], answer_id: str | None = None) -> list[Registration]:
    return sorted(
        (
            row
            for row in rows
            if row.attendance_status is not AttendanceStatus.CANCELLED
            and (answer_id is None or row.answer_id == answer_id)
        ),
        key=queue_key,
    )


def answer_capacity(event: Event, answer_id: str | None) -> int:
    if event.registration_question is None:
        return event.player_amount
    option = event.registration_question.option(answer_id)
    if option is None:
        raise ValueError("У записи отсутствует действующий ответ на дополнительный вопрос")
    return option.player_amount


def main_roster(event: Event, rows: Sequence[Registration]) -> list[Registration]:
    if event.registration_question is None:
        return active_queue(rows)[: event.player_amount]
    return [
        row
        for option in event.registration_question.options
        for row in active_queue(rows, option.option_id)[: option.player_amount]
    ]


def reserve_roster(event: Event, rows: Sequence[Registration]) -> list[Registration]:
    main_keys = {row.participant_key for row in main_roster(event, rows)}
    return [row for row in active_queue(rows) if row.participant_key not in main_keys]


def queue_position(event: Event, rows: Sequence[Registration], registration: Registration) -> int:
    answer_id = registration.answer_id if event.registration_question else None
    return next(
        index
        for index, row in enumerate(active_queue(rows, answer_id), 1)
        if row.participant_key == registration.participant_key
    )


def answer_text(event: Event, registration: Registration) -> str:
    if event.registration_question is None:
        return ""
    option = event.registration_question.option(registration.answer_id)
    return (
        f"Дополнительный вопрос: {event.registration_question.text}\n"
        f"Ваш ответ: {option.name if option else 'не выбран'}"
    )
