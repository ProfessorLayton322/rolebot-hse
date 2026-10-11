"""Game administration, table export, and roster query services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import uuid4

from larp_bot.domain.models import (
    MAX_PLAYER_AMOUNT,
    AttendanceStatus,
    BotIdentity,
    Event,
    Platform,
    Registration,
    TelegramUser,
    User,
)
from larp_bot.domain.security import participant_key

from .identity import identities_for_local_user
from .notification_helpers import (
    _active_main_roster,
)
from .ports import EventRepository, RegistrationShowcaseRepository, UserRepository
from .registration_services import (
    RegistrationCatalog,
    _daily_pass_profiles,
    _event_pass_profiles,
    _unarchived_events,
)
from .service_support import (
    EventNotFound,
    OperationNotAllowed,
    daily_pass_table_path,
    event_table_paths,
    game_signup_token,
)


@dataclass(frozen=True)
class PassTableExport:
    public_url: str | None
    created: bool
    row_count: int | None


@dataclass(frozen=True)
class AchievementExport:
    public_url: str | None
    created: bool


@dataclass(frozen=True)
class RegistrationPage:
    rows: list[Registration]
    has_previous: bool
    has_next: bool


class EventAdministrationService:
    def __init__(
        self,
        events: EventRepository,
        showcase: RegistrationShowcaseRepository,
        catalog: RegistrationCatalog,
        users: UserRepository,
        participant_secret: str,
    ) -> None:
        self.events = events
        self.showcase = showcase
        self.catalog = catalog
        self.users = users
        self.participant_secret = participant_secret

    @property
    def deferred_uploads(self) -> bool:
        return self.showcase.deferred_uploads

    @property
    def mirrors_google_tables(self) -> bool:
        return bool(getattr(self.showcase, "mirrors_google_tables", False))

    async def create_event(
        self,
        name: str,
        game_date: date,
        player_amount: int,
        leaders: Sequence[BotIdentity],
        registration_open_at: datetime | None = None,
        confirmation_open_at: datetime | None = None,
        confirmation_deadline: datetime | None = None,
        scheduled_close_at: datetime | None = None,
        *,
        requester: BotIdentity | None = None,
        reply_user_id: int | None = None,
        master_label: str | None = None,
        masters_pending: bool = False,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> Event | None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Название игры не может быть пустым")
        if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
            raise ValueError(f"Количество игроков должно быть от 1 до {MAX_PLAYER_AMOUNT}")
        if (confirmation_open_at is None) != (confirmation_deadline is None):
            raise ValueError("Открытие подтверждения и дедлайн должны быть заданы вместе")
        if (
            registration_open_at is not None
            and scheduled_close_at is not None
            and registration_open_at >= scheduled_close_at
        ):
            raise ValueError("Регистрация должна открываться раньше закрытия игры")
        if confirmation_open_at is not None and confirmation_deadline is not None:
            if confirmation_deadline <= confirmation_open_at:
                raise ValueError("Дедлайн подтверждения должен быть позже его открытия")
            if registration_open_at is not None and confirmation_open_at <= registration_open_at:
                raise ValueError("Подтверждение должно открываться после регистрации")
            if scheduled_close_at is not None and confirmation_deadline >= scheduled_close_at:
                raise ValueError("Дедлайн подтверждения должен быть раньше закрытия игры")
        signup_token: str | None = None
        for _ in range(20):
            candidate = game_signup_token(clean_name, game_date)
            if await self.events.get_by_signup_token(candidate) is None:
                signup_token = candidate
                break
        if signup_token is None:
            raise RuntimeError("Не удалось создать уникальный токен регистрации")
        event_id = str(uuid4())
        master_path, public_path = event_table_paths(event_id, clean_name)
        pass_path = f"disk:/larp-bot/passes/{event_id}.xlsx"
        leader_keys = {(leader.platform, leader.platform_user_id) for leader in leaders}
        leader_pass_profiles = []
        for user in await self.users.list_all():
            identity = (Platform.TELEGRAM, user.tg_id) if isinstance(user, TelegramUser) else (Platform.VK, user.vk_id)
            if identity in leader_keys and user.needs_pass is True and user.pass_details is not None:
                leader_pass_profiles.append(user.pass_details)
        callback: dict[str, object] = {
            "kind": "event_created",
            "event": {
                "event_id": event_id,
                "name": clean_name,
                "game_date": game_date.isoformat(),
                "player_amount": player_amount,
                "disk_resource_path": master_path,
                "public_table_resource_path": public_path,
                "pass_table_resource_path": pass_path,
                "registration_open_at": registration_open_at.isoformat() if registration_open_at else None,
                "confirmation_open_at": confirmation_open_at.isoformat() if confirmation_open_at else None,
                "confirmation_deadline": confirmation_deadline.isoformat() if confirmation_deadline else None,
                "scheduled_close_at": scheduled_close_at.isoformat() if scheduled_close_at else None,
                "signup_token": signup_token,
            },
            "leaders": [leader.model_dump(mode="json") for leader in leaders],
            "master_label": master_label,
            "masters_pending": masters_pending,
        }
        if requester is not None:
            callback["requester"] = {
                "platform": requester.platform.value,
                "user_id": reply_user_id or requester.platform_user_id,
                "profile_user_id": requester.platform_user_id,
            }
        urls = await self.showcase.create_event_bundle(
            master_path,
            public_path,
            pass_path,
            leader_pass_profiles,
            player_amount=player_amount,
            callback=callback,
            before_invoke=before_invoke,
        )
        if urls is None:
            return None
        event = Event(
            event_id=event_id,
            name=clean_name,
            game_date=game_date,
            player_amount=player_amount,
            disk_resource_path=master_path,
            public_registration_url=urls["master"],
            public_table_resource_path=public_path,
            public_table_public_url=urls["public"],
            pass_table_resource_path=pass_path,
            pass_table_public_url=urls["pass"],
            registration_open_at=registration_open_at,
            confirmation_open_at=confirmation_open_at,
            confirmation_deadline=confirmation_deadline,
            scheduled_close_at=scheduled_close_at,
            signup_token=signup_token,
        )
        await self.events.create(event, leaders)
        await self.refresh_daily_pass_table(game_date)
        return event

    async def ensure_event_tables(
        self,
        event: Event,
        *,
        requester: BotIdentity | None = None,
        reply_user_id: int | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> Event:
        event = await self.catalog.ensure_migrated(event)
        event = await self.catalog.ensure_public_table(
            event,
            requester=requester,
            reply_user_id=reply_user_id,
            before_invoke=before_invoke,
        )
        if event.public_table_public_url is None:
            return event
        return await self.catalog.refresh(event)

    async def _registration_page(
        self,
        event_id: str,
        *,
        statuses: frozenset[AttendanceStatus],
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> RegistrationPage:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        rows = list(
            await self.catalog.registrations.list_page_for_event(
                event_id,
                statuses=statuses,
                after=after,
                before=before,
                limit=limit,
            )
        )
        if not rows:
            return RegistrationPage(rows=[], has_previous=False, has_next=False)
        first = (rows[0].created_at, rows[0].participant_key)
        last = (rows[-1].created_at, rows[-1].participant_key)
        previous, following = await asyncio.gather(
            self.catalog.registrations.list_page_for_event(
                event_id,
                statuses=statuses,
                before=first,
                limit=1,
            ),
            self.catalog.registrations.list_page_for_event(
                event_id,
                statuses=statuses,
                after=last,
                limit=1,
            ),
        )
        return RegistrationPage(rows=rows, has_previous=bool(previous), has_next=bool(following))

    async def active_registration_page(
        self,
        event_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> RegistrationPage:
        return await self._registration_page(
            event_id,
            statuses=frozenset({AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED}),
            after=after,
            before=before,
            limit=limit,
        )

    async def waiting_registration_page(
        self,
        event_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> RegistrationPage:
        return await self._registration_page(
            event_id,
            statuses=frozenset({AttendanceStatus.WAITING}),
            after=after,
            before=before,
            limit=limit,
        )

    async def confirmed_reserve_registration_page(
        self,
        event_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> RegistrationPage:
        if after is not None and before is not None:
            raise ValueError("only one registration-page cursor may be supplied")
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        active = [
            registration
            for registration in await self.catalog.registrations.list_for_event(event_id)
            if registration.attendance_status is not AttendanceStatus.CANCELLED
        ]
        active.sort(key=lambda registration: (registration.created_at, registration.participant_key))
        reserve = [
            registration
            for registration in active[event.player_amount :]
            if registration.attendance_status is AttendanceStatus.CONFIRMED
        ]
        if after is not None:
            reserve = [
                registration
                for registration in reserve
                if (registration.created_at, registration.participant_key) > after
            ]
            rows = reserve[:limit]
        elif before is not None:
            reserve = [
                registration
                for registration in reserve
                if (registration.created_at, registration.participant_key) < before
            ]
            rows = reserve[-limit:]
        else:
            rows = reserve[:limit]
        if not rows:
            return RegistrationPage(rows=[], has_previous=False, has_next=False)

        all_reserve = [
            registration
            for registration in active[event.player_amount :]
            if registration.attendance_status is AttendanceStatus.CONFIRMED
        ]
        first_cursor = (rows[0].created_at, rows[0].participant_key)
        last_cursor = (rows[-1].created_at, rows[-1].participant_key)
        return RegistrationPage(
            rows=rows,
            has_previous=any(
                (registration.created_at, registration.participant_key) < first_cursor for registration in all_reserve
            ),
            has_next=any(
                (registration.created_at, registration.participant_key) > last_cursor for registration in all_reserve
            ),
        )

    async def can_promote_confirmed_reserve(self, event_id: str, participant_key: str) -> bool:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        active = [
            registration
            for registration in await self.catalog.registrations.list_for_event(event_id)
            if registration.attendance_status is not AttendanceStatus.CANCELLED
        ]
        active.sort(key=lambda registration: (registration.created_at, registration.participant_key))
        return any(
            registration.attendance_status is AttendanceStatus.WAITING for registration in active[: event.player_amount]
        ) and any(
            registration.participant_key == participant_key
            and registration.attendance_status is AttendanceStatus.CONFIRMED
            for registration in active[event.player_amount :]
        )

    async def has_unconfirmed_main_slot(self, event_id: str) -> bool:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        registrations = await self.catalog.registrations.list_for_event(event_id)
        return any(
            registration.attendance_status is AttendanceStatus.WAITING
            for registration in _active_main_roster(registrations, event.player_amount)
        )

    async def waiting_registration_count(self, event_id: str) -> int:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        return sum(
            registration.attendance_status is AttendanceStatus.WAITING
            for registration in await self.catalog.registrations.list_for_event(event_id)
        )

    async def confirmed_registration_count(self, event_id: str, *, main_roster_only: bool = False) -> int:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        registrations = list(await self.catalog.registrations.list_for_event(event_id))
        if main_roster_only:
            registrations = _active_main_roster(registrations, event.player_amount)
        return sum(registration.attendance_status is AttendanceStatus.CONFIRMED for registration in registrations)

    async def active_registration_count(self, event_id: str) -> int:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        return sum(
            registration.attendance_status in {AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED}
            for registration in await self.catalog.registrations.list_for_event(event_id)
        )

    async def in_main_roster(self, event_id: str, participant_key: str) -> bool:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        await self.catalog.ensure_migrated(event)
        registrations = await self.catalog.registrations.list_for_event(event_id)
        return any(
            registration.participant_key == participant_key
            for registration in _active_main_roster(registrations, event.player_amount)
        )

    async def registration_user(self, event_id: str, registration_key: str) -> User | None:
        if await self.events.get(event_id) is None:
            raise EventNotFound("Игра не найдена")
        for user in await self.users.list_all():
            if isinstance(user, TelegramUser):
                platform, uid = Platform.TELEGRAM, user.tg_id
            else:
                platform, uid = Platform.VK, user.vk_id
            if participant_key(self.participant_secret, platform, uid, event_id) == registration_key:
                return user
        return None

    async def find_telegram_user_by_profile(self, handle: str) -> TelegramUser | None:
        registrations = await self.catalog.registrations.find_by_telegram_profile(
            f"https://t.me/{handle.removeprefix('@')}"
        )
        if not registrations:
            return None
        telegram_users = [user for user in await self.users.list_all() if isinstance(user, TelegramUser)]
        matches = {
            user.tg_id: user
            for registration in registrations
            for user in telegram_users
            if participant_key(self.participant_secret, Platform.TELEGRAM, user.tg_id, registration.event_id)
            == registration.participant_key
        }
        return next(iter(matches.values())) if len(matches) == 1 else None

    async def get_registration(self, event_id: str, participant_key: str) -> Registration | None:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        return await self.catalog.get(event, participant_key)

    async def active_game_dates(self) -> list[date]:
        return sorted(
            {event.game_date for event in await _unarchived_events(self.events) if event.game_date is not None}
        )

    async def create_daily_pass_table(
        self,
        game_date: date,
        *,
        requester: BotIdentity | None = None,
        reply_user_id: int | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> PassTableExport:
        day_events, profiles = await _daily_pass_profiles(
            game_date,
            self.events,
            self.catalog,
            self.users,
            self.participant_secret,
        )
        if not day_events:
            raise OperationNotAllowed("На выбранную дату нет активных неархивированных игр")
        disk_path = daily_pass_table_path(game_date)
        existed = await self.showcase.pass_table_exists(disk_path)
        callback: dict[str, object] = {
            "kind": "daily_passes",
            "game_date": game_date.isoformat(),
            "resource_path": disk_path,
        }
        if requester is not None:
            callback["requester"] = {
                "platform": requester.platform.value,
                "user_id": reply_user_id or requester.platform_user_id,
                "profile_user_id": requester.platform_user_id,
            }
        public_url = await self.showcase.upsert_published_pass_table(
            disk_path,
            profiles,
            callback=callback,
            before_invoke=before_invoke,
        )
        return PassTableExport(public_url, created=not existed, row_count=len(profiles))

    async def refresh_daily_pass_table(self, game_date: date | None) -> None:
        if game_date is None:
            return
        disk_path = daily_pass_table_path(game_date)
        if not await self.showcase.pass_table_exists(disk_path):
            return
        _, profiles = await _daily_pass_profiles(
            game_date,
            self.events,
            self.catalog,
            self.users,
            self.participant_secret,
        )
        await self.showcase.replace_pass_table(disk_path, profiles)

    async def refresh_daily_pass_tables_for_user(self, platform: Platform, platform_user_id: int) -> None:
        identities = await identities_for_local_user(self.users, platform, platform_user_id)
        active_dates: set[date] = set()
        for event in await _unarchived_events(self.events):
            if event.game_date is None:
                continue
            registrations, leaders = await asyncio.gather(
                self.catalog.registrations.list_for_event(event.event_id),
                self.events.list_leaders(event.event_id),
            )
            active_registration_keys = {
                registration.participant_key
                for registration in registrations
                if registration.attendance_status is not AttendanceStatus.CANCELLED
            }
            leader_keys = {(leader.platform, leader.platform_user_id) for leader in leaders}
            if any(
                (identity.platform, identity.platform_user_id) in leader_keys
                or participant_key(
                    self.participant_secret,
                    identity.platform,
                    identity.platform_user_id,
                    event.event_id,
                )
                in active_registration_keys
                for identity in identities
            ):
                active_dates.add(event.game_date)
        await asyncio.gather(*(self.refresh_daily_pass_table(game_date) for game_date in active_dates))

    async def create_pass_table(
        self,
        event_id: str,
        *,
        requester: BotIdentity | None = None,
        reply_user_id: int | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> PassTableExport:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.pass_table_public_url is not None:
            await self.refresh_pass_table(event.event_id)
            return PassTableExport(event.pass_table_public_url, created=False, row_count=None)

        await self.catalog.ensure_migrated(event)
        profiles = await _event_pass_profiles(
            event,
            self.events,
            self.catalog.registrations,
            self.users,
            self.participant_secret,
        )

        disk_path = f"disk:/larp-bot/passes/{event.event_id}.xlsx"
        public_url = await self.showcase.create_pass_table(
            disk_path,
            profiles,
            callback={
                "kind": "event_tables",
                "event_id": event.event_id,
                "resource_path": disk_path,
                "component": "pass",
                **(
                    {
                        "requester": {
                            "platform": requester.platform.value,
                            "user_id": reply_user_id or requester.platform_user_id,
                            "profile_user_id": requester.platform_user_id,
                        }
                    }
                    if requester is not None
                    else {}
                ),
            },
            before_invoke=before_invoke,
        )
        if public_url is None:
            return PassTableExport(None, created=True, row_count=len(profiles))
        try:
            if not await self.events.set_pass_table(event.event_id, disk_path, public_url):
                raise EventNotFound("Игра была удалена во время создания таблицы")
        except Exception:
            await self.showcase.delete_pass_table(disk_path)
            raise
        return PassTableExport(public_url, created=True, row_count=len(profiles))

    async def refresh_pass_table(self, event_id: str) -> None:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.pass_table_resource_path is not None:
            await self.catalog.ensure_migrated(event)
            profiles = await _event_pass_profiles(
                event,
                self.events,
                self.catalog.registrations,
                self.users,
                self.participant_secret,
            )
            google_url = await self.showcase.replace_pass_table(event.pass_table_resource_path, profiles)
            if google_url is not None:
                await self.events.set_google_table_links(event.event_id, pass_url=google_url)
        await self.refresh_daily_pass_table(event.game_date)

    async def export_achievements(
        self,
        event_id: str,
        *,
        requester: BotIdentity | None = None,
        reply_user_id: int | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> AchievementExport:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")

        replacing = event.achievement_public_url is not None
        disk_path = event.achievement_resource_path or f"disk:/larp-bot/achievements/{event.event_id}.pptx"
        public_url = await self.showcase.create_achievement_presentation(
            disk_path,
            event.name,
            overwrite=replacing,
            callback={
                "kind": "achievement",
                "event_id": event.event_id,
                "resource_path": disk_path,
                **(
                    {
                        "requester": {
                            "platform": requester.platform.value,
                            "user_id": reply_user_id or requester.platform_user_id,
                            "profile_user_id": requester.platform_user_id,
                        }
                    }
                    if requester is not None
                    else {}
                ),
            },
            before_invoke=before_invoke,
        )
        if public_url is None:
            return AchievementExport(None, created=not replacing)
        if replacing:
            return AchievementExport(event.achievement_public_url, created=False)
        try:
            if not await self.events.set_achievement(event.event_id, disk_path, public_url):
                current = await self.events.get(event.event_id)
                if current is not None and current.achievement_public_url is not None:
                    return AchievementExport(current.achievement_public_url, created=False)
                raise EventNotFound("Игра была удалена во время выгрузки ачивок")
        except Exception:
            await self.showcase.delete_achievement_presentation(disk_path)
            raise
        return AchievementExport(public_url, created=True)
