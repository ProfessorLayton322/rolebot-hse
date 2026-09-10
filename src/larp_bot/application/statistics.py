from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from larp_bot.adapters.google_drive.statistics import (
    ATTENDANCE_FILE_NAME,
    MASTER_STATISTICS_FILE_NAME,
    GoogleDriveStatisticsRepository,
)
from larp_bot.adapters.google_drive.statistics_workbook import StatisticsWorkbook
from larp_bot.domain.models import (
    AttendanceStatus,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    ProfileSyncPayload,
    StatisticsPayload,
    TelegramUser,
    User,
    event_name_with_date,
    event_reference,
    surname_and_name,
)
from larp_bot.domain.security import participant_key

from .ports import AdminConfigProvider
from .services import DomainError, EventAdministrationService, RegistrationService

MOSCOW = ZoneInfo("Europe/Moscow")


def current_season_year(at: datetime | None = None) -> int:
    current = datetime.now(MOSCOW) if at is None else at.astimezone(MOSCOW)
    return current.year if current.month >= 9 else current.year - 1


class StatisticsService:
    def __init__(
        self,
        repository: GoogleDriveStatisticsRepository,
        master_repository: GoogleDriveStatisticsRepository,
        admins: AdminConfigProvider,
        administration: EventAdministrationService,
        registrations: RegistrationService,
        season_year: Callable[[], int] = current_season_year,
    ) -> None:
        self.repository = repository
        self.master_repository = master_repository
        self.admins = admins
        self.administration = administration
        self.registrations = registrations
        self.season_year = season_year

    @staticmethod
    def _identity(user: User) -> tuple[Platform, int]:
        if isinstance(user, TelegramUser):
            return Platform.TELEGRAM, user.tg_id
        return Platform.VK, user.vk_id

    async def _grant_experience(
        self,
        event_id: str,
        confirmed_participant_keys: set[str],
        operation_id: str,
    ) -> None:
        users = await self.administration.users.list_all()
        experienced = []
        for user in users:
            platform, user_id = self._identity(user)
            key = participant_key(self.administration.participant_secret, platform, user_id, event_id)
            if key in confirmed_participant_keys:
                experienced.append((platform, user_id))

        async def grant(platform: Platform, user_id: int) -> None:
            await self.administration.users.grant_larp_experience(platform, user_id)
            await self.registrations.enqueue_profile_updates(
                platform,
                user_id,
                idempotency_key=f"{operation_id}:statistics-experience",
            )

        await asyncio.gather(*(grant(platform, user_id) for platform, user_id in experienced))

    async def sync_profile(self, command: OrderedRegistrationCommand) -> str:
        payload = command.payload
        assert isinstance(payload, ProfileSyncPayload)
        user = await self.administration.users.get(command.platform, command.platform_user_id)
        if user is None:
            return "Профиль уже отсутствует"
        experienced = await self.repository.has_any_player(payload.names)
        await self.administration.users.set_larp_experience(
            command.platform,
            command.platform_user_id,
            experienced,
        )
        await self.registrations.enqueue_profile_updates(
            command.platform,
            command.platform_user_id,
            idempotency_key=f"{command.operation_id}:{Operation.SYNC_PROFILE.value}",
        )
        return "Профиль синхронизирован"

    async def apply(self, command: OrderedRegistrationCommand) -> str:
        if not await self.admins.is_admin(command.platform, command.platform_user_id):
            raise DomainError("Недостаточно прав.")
        payload = command.payload
        assert isinstance(payload, StatisticsPayload)
        if payload.action == "link":
            player_link = await self.repository.link()
            master_link = await self.master_repository.link()
            return (
                "Ссылки на таблицы статистики\n\n"
                f"Посещаемость игроков — {ATTENDANCE_FILE_NAME}:\n{player_link}\n\n"
                f"Участие мастеров — {MASTER_STATISTICS_FILE_NAME}:\n{master_link}"
            )
        assert payload.game_id is not None
        event = await self.administration.events.get(payload.game_id)
        if event is None:
            raise DomainError("Игра не найдена.")
        if event.archived_at is not None:
            raise DomainError("Игра уже архивирована и не может быть добавлена в статистику.")
        await self.administration.catalog.ensure_migrated(event)
        registrations, leaders, users = await asyncio.gather(
            self.administration.catalog.registrations.list_for_event(event.event_id),
            self.administration.events.list_leaders(event.event_id),
            self.administration.users.list_all(),
        )
        # Keep only surname and name outside the dedicated pass table.
        # Keep the registration snapshot so subsequent profile edits cannot rename history.
        players = [
            surname_and_name(r.display_name) for r in registrations if r.attendance_status is AttendanceStatus.CONFIRMED
        ]
        users_by_identity = {self._identity(user): user for user in users}
        masters: list[str] = []
        for leader in leaders:
            user = users_by_identity.get((leader.platform, leader.platform_user_id))
            if user is None or user.full_name is None:
                raise DomainError("Не удалось найти профили всех мастеров игры.")
            masters.append(surname_and_name(user.full_name))
        season_year = self.season_year()

        def validate_names(names: list[str], role: str) -> None:
            invalid = [name for name in names if not re.fullmatch(r"[А-ЯЁа-яё-]+(?:\s+[А-ЯЁа-яё-]+)+", name.strip())]
            if invalid:
                raise DomainError(
                    f"У {role} отсутствует корректная фамилия и имя кириллицей: " + ", ".join(invalid[:5])
                )

        validate_names(players, "подтвердивших игроков")
        validate_names(masters, "мастеров игры")
        statistics_game_name = event_name_with_date(event.name, event.game_date)

        def player_transform(book: StatisticsWorkbook) -> bytes | None:
            return book.add_game(season_year, statistics_game_name, players)

        def master_transform(book: StatisticsWorkbook) -> bytes | None:
            return book.add_game(season_year, statistics_game_name, masters)

        # Fully parse and stage both revisions before replacing either Drive file.
        # A partial upload is retry-safe because each workbook independently rejects
        # a game column that is already present in the current season.
        player_edit = await self.repository.prepare(player_transform)
        master_edit = await self.master_repository.prepare(master_transform)
        player_changed = await self.repository.commit(player_edit)
        master_changed = await self.master_repository.commit(master_edit)
        await self._grant_experience(
            event.event_id,
            {r.participant_key for r in registrations if r.attendance_status is AttendanceStatus.CONFIRMED},
            command.operation_id,
        )
        if not player_changed and not master_changed:
            return f"Игра {event_reference(event.name, event.game_date)} уже есть в статистике. Таблицы не изменены."
        return (
            f"Игра {event_reference(event.name, event.game_date)} внесена в статистику сезона "
            f"{season_year}-{season_year + 1}."
        )
