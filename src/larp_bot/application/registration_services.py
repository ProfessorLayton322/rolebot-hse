"""Registration catalog and player-facing registration services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from larp_bot.domain.models import (
    MAX_EVENT_PAGE_SIZE,
    AttendanceStatus,
    BotIdentity,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    CoPlayerWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventDetailsPayload,
    EventStatus,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    PlayerAmountPayload,
    ProfileSyncPayload,
    Registration,
    ReplyContext,
    TelegramUser,
    User,
    normalize_telegram_handle,
)
from larp_bot.domain.security import participant_key

from .identity import identities_for_local_user
from .notification_helpers import (
    _active_main_roster,
    _chat_link_recipient_keys,
    is_plain_chat_link,
)
from .pass_preferences import event_pass_preferences
from .ports import (
    EventRepository,
    OrderedCommandPublisher,
    RegistrationRepository,
    RegistrationShowcaseRepository,
    UserRepository,
)
from .service_support import (
    LOGGER,
    EventNotFound,
    OperationNotAllowed,
    confirmation_times_conflict,
    event_table_paths,
)


class RegistrationCatalog:
    """Registration source of truth plus its derived public showcase."""

    def __init__(
        self,
        events: EventRepository,
        registrations: RegistrationRepository,
        showcase: RegistrationShowcaseRepository,
        *,
        users: UserRepository | None = None,
        participant_secret: str | None = None,
        legacy_registration_imports: bool = True,
    ) -> None:
        if users is not None and participant_secret is None:
            raise ValueError("pass preferences require a participant secret")
        self.events = events
        self.registrations = registrations
        self.showcase = showcase
        self.users = users
        self.participant_secret = participant_secret
        self.legacy_registration_imports = legacy_registration_imports

    async def ensure_migrated(self, event: Event) -> Event:
        if event.registrations_migrated_at is not None:
            return event
        if self.legacy_registration_imports:
            legacy = await self.showcase.read_legacy_registrations(event)
            await self.registrations.import_missing(legacy)
        # AWS imports already contain the authoritative DynamoDB registration
        # rows, so they must not wait for legacy Yandex Disk. The frozen Yandex
        # runtime retains its one-time workbook import through the default.
        event = await self.refresh(event)
        migrated_at = datetime.now(UTC)
        await self.events.mark_registrations_migrated(event.event_id, migrated_at)
        event.registrations_migrated_at = migrated_at
        return event

    async def get(self, event: Event, participant_key: str) -> Registration | None:
        await self.ensure_migrated(event)
        return await self.registrations.get(event.event_id, participant_key)

    async def ensure_public_table(
        self,
        event: Event,
        registrations: list[Registration] | None = None,
        *,
        requester: BotIdentity | None = None,
        reply_user_id: int | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> Event:
        """Attach the contact-free workbook to an event created before the dual-table rollout."""

        if event.public_table_resource_path is not None:
            return event
        rows = registrations
        if rows is None:
            rows = list(await self.registrations.list_for_event(event.event_id))
        _, disk_path = event_table_paths(event.event_id, event.name)
        callback: dict[str, object] = {
            "kind": "event_tables",
            "event_id": event.event_id,
            "resource_path": disk_path,
            "component": "public",
        }
        if requester is not None:
            callback["requester"] = {
                "platform": requester.platform.value,
                "user_id": reply_user_id or requester.platform_user_id,
                "profile_user_id": requester.platform_user_id,
            }
        public_url = await self.showcase.create_public_event_workbook(
            disk_path,
            rows,
            player_amount=event.player_amount,
            callback=callback,
            before_invoke=before_invoke,
        )
        if public_url is None:
            return event
        google_url = public_url if bool(getattr(self.showcase, "mirrors_google_tables", False)) else None
        try:
            attached = await self.events.set_public_table(event.event_id, disk_path, public_url, google_url)
        except Exception:
            await self.showcase.delete_event_workbook(disk_path)
            raise
        if attached:
            return Event.model_validate(
                event.model_dump()
                | {
                    "public_table_resource_path": disk_path,
                    "public_table_public_url": public_url,
                    "public_table_google_url": google_url,
                }
            )

        current = await self.events.get(event.event_id)
        if current is not None and current.public_table_resource_path is not None:
            if current.public_table_resource_path != disk_path:
                await self.showcase.delete_event_workbook(disk_path)
            return current
        await self.showcase.delete_event_workbook(disk_path)
        raise EventNotFound("Игра была удалена во время создания публичной таблицы")

    async def refresh(self, event: Event) -> Event:
        event, _ = await self.refresh_with_rows(event)
        return event

    async def refresh_with_rows(self, event: Event) -> tuple[Event, list[Registration]]:
        """Refresh the public workbook and return the roster already read for it."""

        rows = list(await self.registrations.list_for_event(event.event_id))
        event = await self.ensure_public_table(event, rows)
        active_keys = {row.participant_key for row in rows if row.attendance_status is not AttendanceStatus.CANCELLED}
        answers: dict[str, bool | None] = {}
        if active_keys and self.users is not None:
            assert self.participant_secret is not None
            answers = event_pass_preferences(
                await self.users.list_pass_preferences(),
                event_id=event.event_id,
                secret=self.participant_secret,
                active_keys=active_keys,
            )
        urls = await self.showcase.replace(event, rows, needs_pass_by_key=answers)
        if urls is not None and any(key.startswith("google:") for key in urls):
            await self.events.set_google_table_links(
                event.event_id,
                master_url=urls.get("google:master"),
                public_url=urls.get("google:public"),
            )
            event = await self.events.get(event.event_id) or event
        return event, rows

    async def archive(self, event: Event) -> None:
        await self.events.archive(event.event_id, datetime.now(UTC))

    async def delete(self, event: Event) -> None:
        paths = [event.master_table_resource_path]
        if event.public_table_resource_path is not None:
            paths.append(event.public_table_resource_path)
        pass_table_path = event.pass_table_resource_path or f"disk:/larp-bot/passes/{event.event_id}.xlsx"
        achievement_path = event.achievement_resource_path or f"disk:/larp-bot/achievements/{event.event_id}.pptx"
        await asyncio.gather(
            *(self.showcase.delete_event_workbook(path) for path in dict.fromkeys(paths)),
            self.showcase.delete_pass_table(pass_table_path),
            self.showcase.delete_achievement_presentation(achievement_path),
        )
        await self.registrations.delete_for_event(event.event_id)
        await self.events.delete(event.event_id)


async def _event_pass_profiles(
    event: Event,
    events: EventRepository,
    registrations: RegistrationRepository,
    users: UserRepository,
    participant_secret: str,
    registration_rows: Sequence[Registration] | None = None,
) -> list[PassDetails]:
    if registration_rows is None:
        fetched_registration_rows, user_rows, leader_rows = await asyncio.gather(
            registrations.list_for_event(event.event_id),
            users.list_all(),
            events.list_leaders(event.event_id),
        )
    else:
        fetched_registration_rows = registration_rows
        user_rows, leader_rows = await asyncio.gather(
            users.list_all(),
            events.list_leaders(event.event_id),
        )
    active_registration_keys = {
        registration.participant_key
        for registration in fetched_registration_rows
        if registration.attendance_status is not AttendanceStatus.CANCELLED
    }
    leader_keys = {(leader.platform, leader.platform_user_id) for leader in leader_rows}
    profiles: list[PassDetails] = []
    for user in user_rows:
        if user.needs_pass is not True or user.pass_details is None:
            continue
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        uid = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
        key = participant_key(participant_secret, platform, uid, event.event_id)
        if key in active_registration_keys or (platform, uid) in leader_keys:
            profiles.append(user.pass_details)
    return profiles


async def _unarchived_events(events: EventRepository) -> list[Event]:
    rows: list[Event] = []
    after: tuple[datetime, str] | None = None
    while True:
        page = list(await events.list_page(after=after, archived=False, limit=MAX_EVENT_PAGE_SIZE))
        rows.extend(page)
        if len(page) < MAX_EVENT_PAGE_SIZE:
            return rows
        last = page[-1]
        after = (last.created_at, last.event_id)


def _user_identity(user: User) -> tuple[Platform, int]:
    if isinstance(user, TelegramUser):
        return Platform.TELEGRAM, user.tg_id
    return Platform.VK, user.vk_id


async def _daily_pass_profiles(
    game_date: date,
    events: EventRepository,
    catalog: RegistrationCatalog,
    users: UserRepository,
    participant_secret: str,
) -> tuple[list[Event], list[PassDetails]]:
    day_events = [event for event in await _unarchived_events(events) if event.game_date == game_date]
    if not day_events:
        return [], []
    day_events = list(await asyncio.gather(*(catalog.ensure_migrated(event) for event in day_events)))
    registration_rows, leader_rows, user_rows = await asyncio.gather(
        asyncio.gather(*(catalog.registrations.list_for_event(event.event_id) for event in day_events)),
        asyncio.gather(*(events.list_leaders(event.event_id) for event in day_events)),
        users.list_all(),
    )
    active_keys = [
        {
            registration.participant_key
            for registration in rows
            if registration.attendance_status is not AttendanceStatus.CANCELLED
        }
        for rows in registration_rows
    ]
    leader_keys = [{(leader.platform, leader.platform_user_id) for leader in rows} for rows in leader_rows]
    local_users = list(user_rows)
    linked_users = await asyncio.gather(*(users.find_linked(user) for user in local_users))
    seen_identities: set[tuple[Platform, int]] = set()
    profiles: list[PassDetails] = []
    for user, linked in zip(local_users, linked_users, strict=True):
        group = {
            identity: profile
            for profile in (user, linked)
            if profile is not None
            for identity in [_user_identity(profile)]
        }
        identities = set(group)
        if identities & seen_identities:
            continue
        seen_identities.update(identities)
        belongs_to_day = any(
            identity in leaders
            or participant_key(participant_secret, identity[0], identity[1], event.event_id) in registrations
            for event, registrations, leaders in zip(day_events, active_keys, leader_keys, strict=True)
            for identity in identities
        )
        if not belongs_to_day:
            continue
        current = max(
            group.values(),
            key=lambda profile: (profile.updated_at, *_user_identity(profile)),
        )
        if current.needs_pass is not True or current.pass_details is None:
            continue
        profiles.append(current.pass_details)
    return day_events, profiles


@dataclass(frozen=True)
class RegisteredGamesPage:
    rows: list[tuple[Event, Registration]]
    first_event: Event | None
    last_event: Event | None
    has_previous: bool
    has_next: bool


@dataclass(frozen=True)
class EventPage:
    events: list[Event]
    has_previous: bool
    has_next: bool


@dataclass(frozen=True)
class GameParticipation:
    """A waiting or confirmed registration, event-master membership, or both."""

    registration: Registration | None
    is_leader: bool


@dataclass(frozen=True)
class GameOverview:
    participation: GameParticipation
    in_main_roster: bool
    chat_links: list[str]


def _effective_registration(registrations: Sequence[Registration | None]) -> Registration | None:
    """Choose one view without deleting duplicate records from linked profiles."""

    available = [registration for registration in registrations if registration is not None]
    if not available:
        return None
    status_rank = {
        AttendanceStatus.CANCELLED: 0,
        AttendanceStatus.WAITING: 1,
        AttendanceStatus.CONFIRMED: 2,
    }
    highest = max(status_rank[registration.attendance_status] for registration in available)
    return min(
        (registration for registration in available if status_rank[registration.attendance_status] == highest),
        key=lambda registration: (registration.created_at, registration.participant_key),
    )


async def player_registrations_on_date(
    events: EventRepository,
    catalog: RegistrationCatalog,
    participant_secret: str,
    platform: Platform,
    user_id: int,
    game_date: date,
    *,
    statuses: Collection[AttendanceStatus],
    exclude_event_id: str | None = None,
    identities: Sequence[BotIdentity] | None = None,
    conflicts_with: Event | None = None,
) -> list[tuple[Event, Registration]]:
    """Find a player's active registrations on one game date.

    Events are paged because the event repository deliberately caps list queries
    at MAX_EVENT_PAGE_SIZE rows. Archived legacy games have no date and never
    enter this scan.
    """

    accepted = set(statuses)
    after: tuple[datetime, str] | None = None
    matches: list[tuple[Event, Registration]] = []
    while True:
        page = list(await events.list_page(after=after, archived=False, limit=MAX_EVENT_PAGE_SIZE))
        if not page:
            return matches
        dated_events = [
            event
            for event in page
            if event.game_date == game_date
            and event.event_id != exclude_event_id
            and (conflicts_with is None or confirmation_times_conflict(conflicts_with, event))
        ]
        local_identities = identities or (BotIdentity(platform=platform, platform_user_id=user_id),)
        registration_groups = await asyncio.gather(
            *(
                asyncio.gather(
                    *(
                        catalog.get(
                            event,
                            participant_key(
                                participant_secret,
                                identity.platform,
                                identity.platform_user_id,
                                event.event_id,
                            ),
                        )
                        for identity in local_identities
                    )
                )
                for event in dated_events
            )
        )
        matches.extend(
            (event, registration)
            for event, registrations in zip(dated_events, registration_groups, strict=True)
            if (registration := _effective_registration(registrations)) is not None
            if registration is not None and registration.attendance_status in accepted
        )
        if len(page) < MAX_EVENT_PAGE_SIZE:
            return matches
        last = page[-1]
        after = (last.created_at, last.event_id)


class RegistrationService:
    def __init__(
        self,
        events: EventRepository,
        catalog: RegistrationCatalog,
        publisher: OrderedCommandPublisher,
        participant_secret: str,
        users: UserRepository | None = None,
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.publisher = publisher
        self.participant_secret = participant_secret
        self.users = users

    def key(self, platform: Platform, user_id: int, event_id: str) -> str:
        return participant_key(self.participant_secret, platform, user_id, event_id)

    async def _identities(self, platform: Platform, user_id: int) -> tuple[BotIdentity, ...]:
        return await identities_for_local_user(self.users, platform, user_id)

    async def _registrations(
        self,
        event: Event,
        identities: Sequence[BotIdentity],
    ) -> list[Registration]:
        registrations = await asyncio.gather(
            *(
                self.catalog.get(
                    event,
                    self.key(identity.platform, identity.platform_user_id, event.event_id),
                )
                for identity in identities
            )
        )
        return [registration for registration in registrations if registration is not None]

    async def enqueue_profile_sync(
        self,
        platform: Platform,
        user_id: int,
        payload: ProfileSyncPayload,
        *,
        idempotency_key: str,
    ) -> OrderedRegistrationCommand:
        """Hand profile enrichment and projection fanout to the ordered worker."""

        command = OrderedRegistrationCommand(
            operation_id=str(uuid5(NAMESPACE_URL, f"larp-bot:{platform}:{user_id}:{idempotency_key}")),
            event_id=f"profile:{platform.value}:{user_id}",
            operation=Operation.SYNC_PROFILE,
            platform=platform,
            platform_user_id=user_id,
            payload=payload,
        )
        await self.publisher.publish(command)
        LOGGER.info(
            "ordered_command_enqueued",
            extra={
                "operation_id": command.operation_id,
                "event_id": command.event_id,
                "platform": command.platform.value,
                "platform_update_id": idempotency_key,
                "operation": command.operation.value,
            },
        )
        return command

    async def _event(self, event_id: str) -> Event:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        return event

    async def get_registration(self, event_id: str, platform: Platform, user_id: int) -> Registration | None:
        event = await self._event(event_id)
        identities = await self._identities(platform, user_id)
        return _effective_registration(await self._registrations(event, identities))

    async def get_registration_for_telegram_member(
        self,
        event_id: str,
        user_id: int,
        username: str | None,
    ) -> Registration | None:
        """Resolve a Telegram member by numeric/global identity, then by stored handle."""

        direct = await self.get_registration(event_id, Platform.TELEGRAM, user_id)
        handle = normalize_telegram_handle(username)
        if direct is not None:
            await self._remember_telegram_member(user_id, handle)
            return direct
        if handle is None:
            return None
        matches = [
            registration
            for registration in await self.catalog.registrations.find_by_telegram_profile(handle)
            if registration.event_id == event_id
        ]
        registration = _effective_registration(matches)
        if registration is not None:
            await self._remember_telegram_member(user_id, handle)
        return registration

    async def _remember_telegram_member(self, user_id: int, handle: str | None) -> None:
        if self.users is None or handle is None:
            return
        user = await self.users.get(Platform.TELEGRAM, user_id)
        if user is None:
            await self.users.save(TelegramUser(tg_id=user_id, telegram_handle=handle))
        elif isinstance(user, TelegramUser) and user.telegram_handle != handle:
            await self.users.update_telegram_handle(user_id, handle)

    async def registrations_on_date(
        self,
        platform: Platform,
        user_id: int,
        game_date: date,
        *,
        statuses: Collection[AttendanceStatus],
        exclude_event_id: str | None = None,
        conflicts_with: Event | None = None,
    ) -> list[tuple[Event, Registration]]:
        identities = await self._identities(platform, user_id)
        return await player_registrations_on_date(
            self.events,
            self.catalog,
            self.participant_secret,
            platform,
            user_id,
            game_date,
            statuses=statuses,
            exclude_event_id=exclude_event_id,
            identities=identities,
            conflicts_with=conflicts_with,
        )

    async def registered_games_page(
        self,
        platform: Platform,
        user_id: int,
        *,
        statuses: Collection[EventStatus] | None = None,
        archived: bool | None = None,
        registration_statuses: Collection[AttendanceStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        event_limit: int = 10,
        concurrency: int = 3,
    ) -> RegisteredGamesPage:
        accepted_registration_statuses = None if registration_statuses is None else set(registration_statuses)
        found: dict[str, Registration] = {}
        identities = await self._identities(platform, user_id)

        async def registered(event: Event) -> bool:
            registration = _effective_registration(await self._registrations(event, identities))
            if registration is None or (
                accepted_registration_statuses is not None
                and registration.attendance_status not in accepted_registration_statuses
            ):
                return False
            found[event.event_id] = registration
            return True

        page = await self._event_page(
            registered,
            statuses=statuses,
            archived=archived,
            after=after,
            before=before,
            event_limit=event_limit,
            concurrency=concurrency,
        )
        return RegisteredGamesPage(
            rows=[(event, found[event.event_id]) for event in page.events],
            first_event=page.events[0] if page.events else None,
            last_event=page.events[-1] if page.events else None,
            has_previous=page.has_previous,
            has_next=page.has_next,
        )

    async def participating_games_page(
        self,
        platform: Platform,
        user_id: int,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        event_limit: int = 10,
        concurrency: int = 3,
    ) -> EventPage:
        """Page the non-archived games the user plays in or leads as a master."""

        identities = await self._identities(platform, user_id)

        async def participates(event: Event) -> bool:
            return await self._participation(event, platform, user_id, identities=identities) is not None

        return await self._event_page(
            participates,
            archived=False,
            after=after,
            before=before,
            event_limit=event_limit,
            concurrency=concurrency,
        )

    async def game_overview(self, event: Event, platform: Platform, user_id: int) -> GameOverview | None:
        participation = await self._participation(event, platform, user_id)
        if participation is None:
            return None
        registration = participation.registration
        in_main_roster = False
        receives_chat_links = False
        if registration is not None:
            registrations = list(await self.catalog.registrations.list_for_event(event.event_id))
            main_roster = _active_main_roster(registrations, event.player_amount)
            in_main_roster = any(active.participant_key == registration.participant_key for active in main_roster)
            link_recipients = _chat_link_recipient_keys(registrations, event.player_amount)
            receives_chat_links = registration.participant_key in link_recipients
        # Stored chat links are shown only to the players they are sent to and to the game's masters.
        chat_links = (
            list(dict.fromkeys(text for text in event.confirmed_notifications if is_plain_chat_link(text)))
            if participation.is_leader or receives_chat_links
            else []
        )
        return GameOverview(participation=participation, in_main_roster=in_main_roster, chat_links=chat_links)

    async def _participation(
        self,
        event: Event,
        platform: Platform,
        user_id: int,
        *,
        identities: Sequence[BotIdentity] | None = None,
    ) -> GameParticipation | None:
        identities = identities or await self._identities(platform, user_id)
        registrations, leaders = await asyncio.gather(
            self._registrations(event, identities),
            self.events.list_leaders(event.event_id),
        )
        registration = _effective_registration(registrations)
        if registration is not None and registration.attendance_status is AttendanceStatus.CANCELLED:
            registration = None
        is_leader = any(identity in leaders for identity in identities)
        if registration is None and not is_leader:
            return None
        return GameParticipation(registration=registration, is_leader=is_leader)

    async def _event_page(
        self,
        keep: Callable[[Event], Awaitable[bool]],
        *,
        statuses: Collection[EventStatus] | None = None,
        archived: bool | None,
        after: tuple[datetime, str] | None,
        before: tuple[datetime, str] | None,
        event_limit: int,
        concurrency: int,
    ) -> EventPage:
        """Page the events that ``keep`` accepts, scanning at most ten events per repository query."""

        if event_limit < 1:
            raise ValueError("event page size must be positive")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        semaphore = asyncio.Semaphore(concurrency)

        async def check(event: Event) -> bool:
            async with semaphore:
                return await keep(event)

        async def scan(
            *,
            after_cursor: tuple[datetime, str] | None = None,
            before_cursor: tuple[datetime, str] | None = None,
            match_limit: int,
        ) -> list[Event]:
            matches: list[Event] = []
            scan_limit = min(event_limit, 10)
            while True:
                candidates = list(
                    await self.events.list_page(
                        statuses=statuses,
                        archived=archived,
                        after=after_cursor,
                        before=before_cursor,
                        limit=scan_limit,
                    )
                )
                if not candidates:
                    break
                kept = await asyncio.gather(*(check(event) for event in candidates))
                inspected = [event for event, accepted in zip(candidates, kept, strict=True) if accepted]
                if before_cursor is not None:
                    matches = inspected + matches
                    if len(matches) >= match_limit:
                        return matches[-match_limit:]
                    first = candidates[0]
                    before_cursor = (first.created_at, first.event_id)
                else:
                    matches.extend(inspected)
                    if len(matches) >= match_limit:
                        return matches[:match_limit]
                    last = candidates[-1]
                    after_cursor = (last.created_at, last.event_id)
                if len(candidates) < scan_limit:
                    break
            return matches

        matches = await scan(after_cursor=after, before_cursor=before, match_limit=event_limit)
        if not matches:
            return EventPage(events=[], has_previous=False, has_next=False)
        first, last = matches[0], matches[-1]
        previous, following = await asyncio.gather(
            scan(before_cursor=(first.created_at, first.event_id), match_limit=1),
            scan(after_cursor=(last.created_at, last.event_id), match_limit=1),
        )
        return EventPage(events=matches, has_previous=bool(previous), has_next=bool(following))

    async def enqueue_profile_updates(
        self,
        platform: Platform,
        user_id: int,
        *,
        idempotency_key: str,
        page_size: int = MAX_EVENT_PAGE_SIZE,
        concurrency: int = 10,
    ) -> int:
        """Queue a profile projection refresh for every unarchived game containing the user.

        The page size must stay within the event repository's cap. A larger page
        makes storage reject the very first scan, so no game table would ever
        receive the edited name, nickname or pass data.
        """

        identities = await self._identities(platform, user_id)
        queued = 0
        after: tuple[datetime, str] | None = None
        semaphore = asyncio.Semaphore(concurrency)
        while True:
            events = list(await self.events.list_page(after=after, archived=False, limit=page_size))
            if not events:
                return queued

            async def participation_for(event: Event) -> tuple[Event, list[BotIdentity]]:
                async with semaphore:
                    await self.catalog.ensure_migrated(event)
                    registrations, leaders = await asyncio.gather(
                        asyncio.gather(
                            *(
                                self.catalog.registrations.get(
                                    event.event_id,
                                    self.key(identity.platform, identity.platform_user_id, event.event_id),
                                )
                                for identity in identities
                            )
                        ),
                        self.events.list_leaders(event.event_id),
                    )
                    return event, [
                        identity
                        for identity, registration in zip(identities, registrations, strict=True)
                        if registration is not None or identity in leaders
                    ]

            participations = await asyncio.gather(*(participation_for(event) for event in events))
            for event, participating_identities in participations:
                for identity in participating_identities:
                    await self.enqueue(
                        operation=Operation.UPDATE_PROFILE,
                        event_id=event.event_id,
                        platform=identity.platform,
                        user_id=identity.platform_user_id,
                        payload=EmptyPayload(),
                        reply_context=ReplyContext(),
                        idempotency_key=f"{idempotency_key}:{Operation.UPDATE_PROFILE.value}:{event.event_id}",
                    )
                    queued += 1

            if len(events) < page_size:
                return queued
            last = events[-1]
            after = (last.created_at, last.event_id)

    async def enqueue(
        self,
        *,
        operation: Operation,
        event_id: str,
        platform: Platform,
        user_id: int,
        payload: EnlistPayload
        | CoPlayerWishPayload
        | CharacterWishPayload
        | ConfirmationDeadlinePayload
        | NotificationPayload
        | EventDetailsPayload
        | PlayerAmountPayload
        | EmptyPayload,
        reply_context: ReplyContext,
        idempotency_key: str | None = None,
        target_participant_key: str | None = None,
    ) -> OrderedRegistrationCommand:
        event = await self._event(event_id)
        if operation is Operation.ENLIST:
            if event.status is EventStatus.CLOSED:
                raise OperationNotAllowed("Регистрация на эту игру закрыта")
            if not event.registration_is_open():
                raise OperationNotAllowed("Регистрация на эту игру ещё не открыта")
        administrative_operations = {
            Operation.REFRESH_EVENT_TABLES,
            Operation.OPEN_REGISTRATION,
            Operation.OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.SEND_ALL_PLAYERS_NOTIFICATION,
            Operation.UPDATE_EVENT_DETAILS,
            Operation.UPDATE_PLAYER_AMOUNT,
            Operation.REMOVE_PARTICIPANT,
            Operation.REMOVE_WAITING_PARTICIPANTS,
            Operation.PROMOTE_RESERVE_PARTICIPANT,
            Operation.CLOSE_EVENT,
            Operation.ARCHIVE_EVENT,
            Operation.UNARCHIVE_EVENT,
            Operation.DELETE_EVENT,
        }
        identities: tuple[BotIdentity, ...] | None = None
        targeted_administrative_operations = {
            Operation.REMOVE_PARTICIPANT,
            Operation.PROMOTE_RESERVE_PARTICIPANT,
        }
        if operation in targeted_administrative_operations:
            if target_participant_key is None:
                raise ValueError(f"{operation} requires a target participant")
            participant = target_participant_key
        elif target_participant_key is not None:
            raise ValueError("a target participant is only valid for a targeted administrative operation")
        elif operation not in administrative_operations:
            if operation in {Operation.ENLIST, Operation.UPDATE_PROFILE}:
                participant = self.key(platform, user_id, event_id)
            else:
                identities = await self._identities(platform, user_id)
                existing = _effective_registration(await self._registrations(event, identities))
                participant = (
                    existing.participant_key if existing is not None else self.key(platform, user_id, event_id)
                )
        else:
            participant = None
        identity_group = None
        if operation is Operation.CONFIRM:
            identities = identities or await self._identities(platform, user_id)
            identity_group = ":".join(
                f"{identity.platform.value}:{identity.platform_user_id}"
                for identity in sorted(identities, key=lambda item: (item.platform.value, item.platform_user_id))
            )
        command = OrderedRegistrationCommand(
            operation_id=(
                str(uuid5(NAMESPACE_URL, f"larp-bot:{platform}:{user_id}:{idempotency_key}"))
                if idempotency_key
                else str(uuid4())
            ),
            event_id=event_id,
            operation=operation,
            platform=platform,
            platform_user_id=user_id,
            participant_key=participant,
            identity_group=identity_group,
            payload=payload,
            reply_context=reply_context,
        )
        await self.publisher.publish(command)
        LOGGER.info(
            "ordered_command_enqueued",
            extra={
                "operation_id": command.operation_id,
                "event_id": command.event_id,
                "platform": command.platform.value,
                "platform_update_id": idempotency_key,
                "operation": command.operation.value,
            },
        )
        return command
