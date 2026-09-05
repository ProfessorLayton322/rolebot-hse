from __future__ import annotations

import re

from larp_bot.adapters.yandex_disk.statistics import YandexDiskStatisticsRepository
from larp_bot.adapters.yandex_disk.statistics_workbook import StatisticsWorkbook
from larp_bot.domain.models import AttendanceStatus, OrderedRegistrationCommand, StatisticsPayload

from .ports import AdminConfigProvider
from .services import DomainError, EventAdministrationService


class StatisticsService:
    def __init__(
        self,
        repository: YandexDiskStatisticsRepository,
        admins: AdminConfigProvider,
        administration: EventAdministrationService,
    ) -> None:
        self.repository = repository
        self.admins = admins
        self.administration = administration

    async def apply(self, command: OrderedRegistrationCommand) -> str:
        if not await self.admins.is_admin(command.platform, command.platform_user_id):
            raise DomainError("Недостаточно прав.")
        payload = command.payload
        assert isinstance(payload, StatisticsPayload)
        if payload.action == "select":
            return await self.repository.select(payload.table_name, command.operation_id)
        if payload.action == "link":
            return f"Сводная статистика посещений:\n{await self.repository.link()}"
        stamp = command.created_at.strftime("%Y%m%d%H%M%S%f")
        if payload.action == "season":
            assert payload.season_year is not None
            year = payload.season_year
            changed = await self.repository.edit(command.operation_id, stamp, lambda book: book.new_season(year))
            return f"Сезон {year}-{year + 1}: " + (
                "создан, резервная копия сохранена." if changed else "уже существует."
            )
        assert payload.game_id is not None
        event = await self.administration.events.get(payload.game_id)
        if event is None:
            raise DomainError("Игра не найдена.")
        await self.administration.catalog.ensure_migrated(event)
        registrations = await self.administration.catalog.registrations.list_for_event(event.event_id)
        # Full names are stored as Cyrillic surname + name by the shared profile flow.
        # Keep the registration snapshot so subsequent profile edits cannot rename history.
        players = [r.display_name for r in registrations if r.attendance_status is AttendanceStatus.CONFIRMED]

        def transform(book: StatisticsWorkbook) -> bytes | None:
            if book.has_game(event.name):
                return None
            invalid = [name for name in players if not re.fullmatch(r"[А-ЯЁа-яё-]+(?:\s+[А-ЯЁа-яё-]+)+", name.strip())]
            if invalid:
                raise DomainError(
                    "У подтвердивших игроков отсутствует корректная фамилия и имя кириллицей: " + ", ".join(invalid[:5])
                )
            return book.mark_game(event.name, players)

        changed = await self.repository.edit(command.operation_id, stamp, transform)
        if not changed:
            return f"Игра «{event.name}» уже есть в статистике. Таблица не изменена."
        return f"Игра «{event.name}» внесена в статистику. Резервная копия сохранена."
