from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from larp_bot.adapters.google_drive.statistics import ATTENDANCE_FILE_NAME, GoogleDriveStatisticsRepository
from larp_bot.adapters.google_drive.statistics_workbook import StatisticsWorkbook
from larp_bot.domain.models import AttendanceStatus, OrderedRegistrationCommand, StatisticsPayload

from .ports import AdminConfigProvider
from .services import DomainError, EventAdministrationService

MOSCOW = ZoneInfo("Europe/Moscow")


def current_season_year(at: datetime | None = None) -> int:
    current = datetime.now(MOSCOW) if at is None else at.astimezone(MOSCOW)
    return current.year if current.month >= 9 else current.year - 1


class StatisticsService:
    def __init__(
        self,
        repository: GoogleDriveStatisticsRepository,
        admins: AdminConfigProvider,
        administration: EventAdministrationService,
        season_year: Callable[[], int] = current_season_year,
    ) -> None:
        self.repository = repository
        self.admins = admins
        self.administration = administration
        self.season_year = season_year

    async def apply(self, command: OrderedRegistrationCommand) -> str:
        if not await self.admins.is_admin(command.platform, command.platform_user_id):
            raise DomainError("Недостаточно прав.")
        payload = command.payload
        assert isinstance(payload, StatisticsPayload)
        if payload.action == "link":
            return (
                "Ссылки на таблицы статистики\n\n"
                f"Посещаемость игроков — {ATTENDANCE_FILE_NAME}:\n{await self.repository.link()}"
            )
        assert payload.game_id is not None
        event = await self.administration.events.get(payload.game_id)
        if event is None:
            raise DomainError("Игра не найдена.")
        if event.archived_at is not None:
            raise DomainError("Игра уже архивирована и не может быть добавлена в статистику.")
        await self.administration.catalog.ensure_migrated(event)
        registrations = await self.administration.catalog.registrations.list_for_event(event.event_id)
        # Full names are stored as Cyrillic surname + name by the shared profile flow.
        # Keep the registration snapshot so subsequent profile edits cannot rename history.
        players = [r.display_name for r in registrations if r.attendance_status is AttendanceStatus.CONFIRMED]
        season_year = self.season_year()

        def transform(book: StatisticsWorkbook) -> bytes | None:
            if book.season_name == f"{season_year}-{season_year + 1}" and book.has_game(event.name):
                return None
            invalid = [name for name in players if not re.fullmatch(r"[А-ЯЁа-яё-]+(?:\s+[А-ЯЁа-яё-]+)+", name.strip())]
            if invalid:
                raise DomainError(
                    "У подтвердивших игроков отсутствует корректная фамилия и имя кириллицей: " + ", ".join(invalid[:5])
                )
            return book.add_game(season_year, event.name, players)

        changed = await self.repository.edit(transform)
        if not changed:
            return f"Игра «{event.name}» уже есть в статистике. Таблица не изменена."
        return f"Игра «{event.name}» внесена в статистику сезона {season_year}-{season_year + 1}."
