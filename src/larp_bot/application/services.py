from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from larp_bot.domain.models import (
    MAX_PLAYER_AMOUNT,
    AttendanceStatus,
    BotIdentity,
    Button,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    CoPlayerWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
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
    event_reference,
    format_game_date,
    surname_and_name,
)
from larp_bot.domain.security import participant_key

from .deadlines import format_confirmation_deadline
from .navigation import confirmation_button, main_menu_button
from .ports import (
    AdminConfigProvider,
    DeferredTransport,
    EventRepository,
    OrderedCommandPublisher,
    RegistrationRepository,
    RegistrationShowcaseRepository,
    UserRepository,
)

LOGGER = logging.getLogger("larp_bot.application.services")

_CYRILLIC_TRANSLITERATION = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
_START_PARAMETER_ALPHABET = string.ascii_letters + string.digits
_MAX_START_PARAMETER_LENGTH = 64


class DomainError(RuntimeError):
    pass


class EventNotFound(DomainError):
    pass


class RegistrationNotFound(DomainError):
    pass


class OperationNotAllowed(DomainError):
    pass


def same_day_signup_warning(game_date: date) -> str:
    return (
        "Важно: вы уже записаны на другую игру, которая проводится "
        f"{format_game_date(game_date)}. Вы сможете подтвердить своё участие только "
        "в одной игре, когда решите, в какую именно хотите играть."
    )


@dataclass(frozen=True)
class MutationResult:
    text: str
    notifications_enabled: bool = True
    buttons: tuple[Button, ...] | None = None
    replaced_event_id: str | None = None


SIFT_PROMOTED_PREFIX = "promoted:"
SIFT_DEMOTED_PREFIX = "demoted:"


def _sift_snapshot_entry(prefix: str, participant_key: str) -> str:
    return f"{prefix}{participant_key}"


def _parse_sift_snapshot_entry(value: str) -> tuple[str, str] | None:
    for prefix in (SIFT_PROMOTED_PREFIX, SIFT_DEMOTED_PREFIX):
        if value.startswith(prefix):
            return prefix, value.removeprefix(prefix)
    return None


def event_slug(name: str) -> str:
    transliterated = name.casefold().replace("ё", "е")
    slug = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")
    return (slug or "game")[:40]


def event_table_paths(event_id: str, name: str) -> tuple[str, str]:
    """Build unique Disk paths while keeping the requested game name in each filename."""

    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", name.strip())
    safe_name = re.sub(r"\s+", " ", safe_name).strip(" .")[:120].rstrip(" .")
    if not safe_name:
        safe_name = event_slug(name)
    directory = f"disk:/larp-bot/events/{event_id}"
    return (
        f"{directory}/master_table_{safe_name}.xlsx",
        f"{directory}/public_table_{safe_name}.xlsx",
    )


def game_signup_token(name: str, game_date: date, *, random_suffix: str | None = None) -> str:
    """Build a readable token that is valid as a Telegram start parameter."""

    characters: list[str] = []
    for character in name.strip():
        replacement = _CYRILLIC_TRANSLITERATION.get(character.casefold())
        if replacement is not None:
            characters.append(replacement.capitalize() if character.isupper() else replacement)
        elif character.isascii() and character.isalnum():
            characters.append(character)
        else:
            characters.append("_")
    transliterated = "".join(characters)
    name_part = re.sub(r"_+", "_", transliterated).strip("_") or "game"
    suffix = (
        random_suffix
        if random_suffix is not None
        else "".join(secrets.choice(_START_PARAMETER_ALPHABET) for _ in range(3))
    )
    if len(suffix) != 3 or any(character not in _START_PARAMETER_ALPHABET for character in suffix):
        raise ValueError("signup token suffix must contain three alphanumeric characters")
    tail = f"_{game_date:%d_%m_%Y}_{suffix}"
    name_part = name_part[: _MAX_START_PARAMETER_LENGTH - len(tail)].rstrip("_") or "game"
    return f"{name_part}{tail}"


class RegistrationCatalog:
    """YDB registration source of truth plus its derived public showcase."""

    def __init__(
        self,
        events: EventRepository,
        registrations: RegistrationRepository,
        showcase: RegistrationShowcaseRepository,
    ) -> None:
        self.events = events
        self.registrations = registrations
        self.showcase = showcase

    async def ensure_migrated(self, event: Event) -> Event:
        if event.registrations_migrated_at is not None:
            return event
        legacy = await self.showcase.read_legacy_registrations(event)
        await self.registrations.import_missing(legacy)
        # Publish the YDB projection before setting the marker. If Disk is
        # temporarily unavailable, the next request safely retries migration.
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
    ) -> Event:
        """Attach the contact-free workbook to an event created before the dual-table rollout."""

        if event.public_table_resource_path is not None:
            return event
        rows = registrations
        if rows is None:
            rows = list(await self.registrations.list_for_event(event.event_id))
        _, disk_path = event_table_paths(event.event_id, event.name)
        public_url = await self.showcase.create_public_event_workbook(
            disk_path,
            rows,
            player_amount=event.player_amount,
        )
        try:
            attached = await self.events.set_public_table(event.event_id, disk_path, public_url)
        except Exception:
            await self.showcase.delete_event_workbook(disk_path)
            raise
        if attached:
            return Event.model_validate(
                event.model_dump()
                | {
                    "public_table_resource_path": disk_path,
                    "public_table_public_url": public_url,
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
        rows = list(await self.registrations.list_for_event(event.event_id))
        event = await self.ensure_public_table(event, rows)
        await self.showcase.replace(event, rows)
        return event

    async def archive(self, event: Event) -> None:
        await self.events.archive(event.event_id, datetime.now(UTC))

    async def delete(self, event: Event) -> None:
        """Archive legacy delete requests without removing permanent game data."""
        await self.archive(event)


async def _event_pass_profiles(
    event: Event,
    events: EventRepository,
    registrations: RegistrationRepository,
    users: UserRepository,
    participant_secret: str,
) -> list[PassDetails]:
    registration_rows, user_rows, leader_rows = await asyncio.gather(
        registrations.list_for_event(event.event_id),
        users.list_all(),
        events.list_leaders(event.event_id),
    )
    confirmed_keys = {
        registration.participant_key
        for registration in registration_rows
        if registration.attendance_status is AttendanceStatus.CONFIRMED
    }
    leader_keys = {(leader.platform, leader.platform_user_id) for leader in leader_rows}
    profiles: list[PassDetails] = []
    for user in user_rows:
        if user.needs_pass is not True or user.pass_details is None:
            continue
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        uid = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
        key = participant_key(participant_secret, platform, uid, event.event_id)
        if key in confirmed_keys or (platform, uid) in leader_keys:
            profiles.append(user.pass_details)
    return profiles


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
) -> list[tuple[Event, Registration]]:
    """Find a player's active registrations on one game date.

    Events are paged because the YDB repository deliberately caps list queries
    at ten rows. Archived legacy games have no date and never enter this scan.
    """

    accepted = set(statuses)
    after: tuple[datetime, str] | None = None
    matches: list[tuple[Event, Registration]] = []
    while True:
        page = list(await events.list_page(after=after, archived=False, limit=10))
        if not page:
            return matches
        dated_events = [event for event in page if event.game_date == game_date and event.event_id != exclude_event_id]
        registrations = await asyncio.gather(
            *(
                catalog.get(
                    event,
                    participant_key(participant_secret, platform, user_id, event.event_id),
                )
                for event in dated_events
            )
        )
        matches.extend(
            (event, registration)
            for event, registration in zip(dated_events, registrations, strict=True)
            if registration is not None and registration.attendance_status in accepted
        )
        if len(page) < 10:
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
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.publisher = publisher
        self.participant_secret = participant_secret

    def key(self, platform: Platform, user_id: int, event_id: str) -> str:
        return participant_key(self.participant_secret, platform, user_id, event_id)

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
        return await self.catalog.get(event, self.key(platform, user_id, event_id))

    async def registrations_on_date(
        self,
        platform: Platform,
        user_id: int,
        game_date: date,
        *,
        statuses: Collection[AttendanceStatus],
        exclude_event_id: str | None = None,
    ) -> list[tuple[Event, Registration]]:
        return await player_registrations_on_date(
            self.events,
            self.catalog,
            self.participant_secret,
            platform,
            user_id,
            game_date,
            statuses=statuses,
            exclude_event_id=exclude_event_id,
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

        async def registered(event: Event) -> bool:
            registration = await self.catalog.get(event, self.key(platform, user_id, event.event_id))
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

        async def participates(event: Event) -> bool:
            return await self._participation(event, platform, user_id) is not None

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

    async def _participation(self, event: Event, platform: Platform, user_id: int) -> GameParticipation | None:
        registration, leaders = await asyncio.gather(
            self.catalog.get(event, self.key(platform, user_id, event.event_id)),
            self.events.list_leaders(event.event_id),
        )
        if registration is not None and registration.attendance_status is AttendanceStatus.CANCELLED:
            registration = None
        is_leader = BotIdentity(platform=platform, platform_user_id=user_id) in leaders
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
        page_size: int = 100,
        concurrency: int = 10,
    ) -> int:
        """Queue a profile projection refresh for every unarchived game containing the user."""

        queued = 0
        after: tuple[datetime, str] | None = None
        semaphore = asyncio.Semaphore(concurrency)
        while True:
            events = list(await self.events.list_page(after=after, archived=False, limit=page_size))
            if not events:
                return queued

            async def participation_for(event: Event) -> tuple[Event, Registration | None, bool]:
                async with semaphore:
                    key = self.key(platform, user_id, event.event_id)
                    registration, leaders = await asyncio.gather(
                        self.catalog.get(event, key),
                        self.events.list_leaders(event.event_id),
                    )
                    identity = BotIdentity(platform=platform, platform_user_id=user_id)
                    return event, registration, identity in leaders

            participations = await asyncio.gather(*(participation_for(event) for event in events))
            for event, registration, is_leader in participations:
                if registration is None and not is_leader:
                    continue
                await self.enqueue(
                    operation=Operation.UPDATE_PROFILE,
                    event_id=event.event_id,
                    platform=platform,
                    user_id=user_id,
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
            Operation.OPEN_REGISTRATION,
            Operation.OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.UPDATE_PLAYER_AMOUNT,
            Operation.REMOVE_PARTICIPANT,
            Operation.REMOVE_WAITING_PARTICIPANTS,
            Operation.CLOSE_EVENT,
            Operation.ARCHIVE_EVENT,
            Operation.UNARCHIVE_EVENT,
            Operation.DELETE_EVENT,
        }
        if operation is Operation.REMOVE_PARTICIPANT:
            if target_participant_key is None:
                raise ValueError("REMOVE_PARTICIPANT requires a target participant")
            participant = target_participant_key
        elif target_participant_key is not None:
            raise ValueError("a target participant is only valid for REMOVE_PARTICIPANT")
        elif operation not in administrative_operations:
            participant = self.key(platform, user_id, event_id)
        else:
            participant = None
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


@dataclass(frozen=True)
class PassTableExport:
    public_url: str
    created: bool
    row_count: int | None


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

    async def create_event(
        self,
        name: str,
        game_date: date,
        player_amount: int,
        leaders: Sequence[BotIdentity],
        registration_open_at: datetime | None = None,
    ) -> Event:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Название игры не может быть пустым")
        if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
            raise ValueError(f"Количество игроков должно быть от 1 до {MAX_PLAYER_AMOUNT}")
        if not leaders:
            raise ValueError("У игры должен быть хотя бы один мастер")
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
        created_event_paths: list[str] = []
        pass_table_created = False
        try:
            master_url = await self.showcase.create_event_workbook(master_path, player_amount=player_amount)
            created_event_paths.append(master_path)
            public_url = await self.showcase.create_public_event_workbook(public_path, player_amount=player_amount)
            created_event_paths.append(public_path)
            leader_keys = {(leader.platform, leader.platform_user_id) for leader in leaders}
            leader_pass_profiles = []
            for user in await self.users.list_all():
                identity = (
                    (Platform.TELEGRAM, user.tg_id) if isinstance(user, TelegramUser) else (Platform.VK, user.vk_id)
                )
                if identity in leader_keys and user.needs_pass is True and user.pass_details is not None:
                    leader_pass_profiles.append(user.pass_details)
            pass_url = await self.showcase.create_pass_table(pass_path, leader_pass_profiles)
            pass_table_created = True
            event = Event(
                event_id=event_id,
                name=clean_name,
                game_date=game_date,
                player_amount=player_amount,
                disk_resource_path=master_path,
                public_registration_url=master_url,
                public_table_resource_path=public_path,
                public_table_public_url=public_url,
                pass_table_resource_path=pass_path,
                pass_table_public_url=pass_url,
                registration_open_at=registration_open_at,
                signup_token=signup_token,
            )
            await self.events.create(event, leaders)
        except Exception:
            await asyncio.gather(
                *(self.showcase.delete_event_workbook(path) for path in created_event_paths),
                *([self.showcase.delete_pass_table(pass_path)] if pass_table_created else []),
                return_exceptions=True,
            )
            raise
        return event

    async def ensure_event_tables(self, event: Event) -> Event:
        event = await self.catalog.ensure_migrated(event)
        return await self.catalog.ensure_public_table(event)

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

    async def create_pass_table(self, event_id: str) -> PassTableExport:
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
        public_url = await self.showcase.create_pass_table(disk_path, profiles)
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
        if event.pass_table_resource_path is None:
            return
        await self.catalog.ensure_migrated(event)
        profiles = await _event_pass_profiles(
            event,
            self.events,
            self.catalog.registrations,
            self.users,
            self.participant_secret,
        )
        await self.showcase.replace_pass_table(event.pass_table_resource_path, profiles)


@dataclass(frozen=True)
class NotificationRecipient:
    platform: Platform
    user_id: int
    user: User


_TELEGRAM_CHAT_HOSTS = frozenset(
    {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog"}
)
_TELEGRAM_CHAT_PATH_RE = re.compile(r"^/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_]{3,31})/?$")
_VK_CHAT_PATH_RE = re.compile(r"^/join/[A-Za-z0-9_+=-]+(?:/[A-Za-z0-9_+=-]+)*/?$")


def is_plain_chat_link(value: str) -> bool:
    """Recognize a message consisting solely of a Telegram or VK chat link."""

    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return False
    try:
        parsed = urlsplit(candidate)
        has_credentials_or_port = parsed.username is not None or parsed.password is not None or parsed.port is not None
        host = (parsed.hostname or "").casefold()
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or has_credentials_or_port:
        return False
    if parsed.query or parsed.fragment:
        return False
    if host in _TELEGRAM_CHAT_HOSTS:
        return bool(_TELEGRAM_CHAT_PATH_RE.fullmatch(parsed.path))
    return host in {"vk.me", "www.vk.me"} and bool(_VK_CHAT_PATH_RE.fullmatch(parsed.path))


def confirmed_notification_text(event_name: str, message: str, game_date: date | None = None) -> str:
    clean_message = message.strip()
    reference = event_reference(event_name, game_date)
    if not is_plain_chat_link(clean_message):
        return f"Сообщение от мастеров игры {reference}:\n\n{clean_message}"
    return f"Вы в основном составе игры {reference}. Вступите в чат: {clean_message}"


def _active_main_roster(registrations: Sequence[Registration], player_amount: int) -> list[Registration]:
    active = [
        registration
        for registration in registrations
        if registration.attendance_status is not AttendanceStatus.CANCELLED
    ]
    active.sort(key=lambda registration: (registration.created_at, registration.participant_key))
    return active[:player_amount]


def _chat_link_recipient_keys(registrations: Sequence[Registration], player_amount: int) -> set[str]:
    """Stored chat links belong only to confirmed players inside the active main roster."""

    return {
        registration.participant_key
        for registration in _active_main_roster(registrations, player_amount)
        if registration.attendance_status is AttendanceStatus.CONFIRMED
    }


# Telegram rejects longer message texts and counts them in UTF-16 code units.
MAX_MESSAGE_LENGTH = 4096
MASTER_NO_SHOW_WARNING = (
    "Если вы не придете на игру, ничего нам не сообщив, к вам могут быть применены санкции Ролевого Клуба"
)


def _message_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def event_master_records(
    events: EventRepository,
    users: UserRepository | None,
    event_id: str,
) -> list[tuple[BotIdentity, User | None]]:
    """Load a game's masters with their bot profiles, ordered by name."""

    identities = list(await events.list_leaders(event_id))
    profiles: Sequence[User | None] = (
        await asyncio.gather(*(users.get(identity.platform, identity.platform_user_id) for identity in identities))
        if users is not None
        else [None] * len(identities)
    )
    records = list(zip(identities, profiles, strict=True))
    return sorted(
        records,
        key=lambda record: (
            ((record[1].full_name or "") if record[1] is not None else "").casefold(),
            record[0].platform.value,
            record[0].platform_user_id,
        ),
    )


def master_profile_url(identity: BotIdentity, profile: User | None) -> str | None:
    """Link the profile on the platform where the master uses the bot."""

    if identity.platform is Platform.VK:
        return f"https://vk.com/id{identity.platform_user_id}"
    if isinstance(profile, TelegramUser) and profile.telegram_handle is not None:
        return f"https://t.me/{profile.telegram_handle.removeprefix('@')}"
    # A Telegram account without a public username has no plain-text link.
    return None


def event_masters_text(masters: Sequence[tuple[BotIdentity, User | None]]) -> str:
    lines = []
    for identity, profile in masters:
        name = surname_and_name(profile.full_name) if profile is not None and profile.full_name else None
        parts = [part for part in (name, master_profile_url(identity, profile)) if part is not None]
        if parts:
            lines.append(" — ".join(parts))
    return "Мастера игры:\n" + "\n".join(lines) if lines else ""


def master_contacts_section(masters: Sequence[tuple[BotIdentity, User | None]]) -> str:
    """Name the masters and ask players to warn them before skipping the game."""

    profiles = [url for identity, profile in masters if (url := master_profile_url(identity, profile)) is not None]
    contacts = f": {' '.join(profiles)} ." if profiles else "."
    disclaimer = (
        f"Если вы решите не участвовать в игре, пожалуйста сообщите об этом мастерам{contacts} {MASTER_NO_SHOW_WARNING}"
    )
    masters_text = event_masters_text(masters)
    return f"{masters_text}\n\n{disclaimer}" if masters_text else disclaimer


def confirmation_success_text(
    event: Event,
    character_wish: str,
    *,
    in_main_roster: bool,
    masters: Sequence[tuple[BotIdentity, User | None]],
) -> str:
    link_text = "Мастера скоро отправят вам ссылку на чат игры!\n\n" if in_main_roster else ""

    def compose(wish: str) -> str:
        return (
            f"✅ Участие в игре {event_reference(event.name, event.game_date)} подтверждено.\n\n"
            f"{link_text}"
            f"Пожелания по персонажу:\n{wish}\n\n"
            "Позже их можно изменить через «🎭 Пожелания по игре».\n\n"
            f"{master_contacts_section(masters)}"
        )

    text = compose(character_wish)
    excess = _message_length(text) - MAX_MESSAGE_LENGTH
    if excess <= 0:
        return text
    # Only the echo is shortened; the stored wish stays complete and the master contacts still fit.
    budget = _message_length(character_wish) - excess - 1
    kept: list[str] = []
    for character in character_wish:
        budget -= _message_length(character)
        if budget < 0:
            break
        kept.append(character)
    return compose("".join(kept).rstrip() + "…")


def enlistment_success_text(
    event: Event,
    queue_position: int,
    *,
    masters: Sequence[tuple[BotIdentity, User | None]],
    same_day_warning: bool = False,
) -> str:
    if event.public_table_public_url is None:
        raise RuntimeError("public game table is not initialized")
    if queue_position <= event.player_amount:
        position_text = (
            f"Вы записались на игру {event_reference(event.name, event.game_date)}!\n\n"
            "Сейчас вы в основном составе.\n\n"
            "Актуальный список записавшихся и состав можно посмотреть здесь: "
            f"{event.public_table_public_url}"
        )
    else:
        cancellations_needed = queue_position - event.player_amount
        position_text = (
            f"Вы записались на игру {event_reference(event.name, event.game_date)}!\n\n"
            f"Сейчас вы в запасе. Перед вами: {cancellations_needed}.\n\n"
            "Если вы перейдёте в основной состав, бот сообщит об этом отдельным сообщением.\n\n"
            "Актуальный список записавшихся и состав можно посмотреть здесь: "
            f"{event.public_table_public_url}"
        )
    if event.status is EventStatus.CONFIRMATION_OPEN:
        deadline_text = (
            f" до {format_confirmation_deadline(event.confirmation_deadline)} (мск)"
            if event.confirmation_deadline is not None
            else ""
        )
        confirmation_text = f"Когда будете уверены, что сможете прийти на игру, подтвердите участие{deadline_text}."
    else:
        confirmation_text = (
            "Подтверждение участия пока не открыто. Мы обязательно пришлём вам уведомление, "
            "когда можно будет подтвердиться."
        )
    text = (
        f"{position_text}\n\n{confirmation_text}\n\n"
        "Пожелания по соигрокам всегда можно изменить позже через «🎭 Пожелания по игре»."
    )
    if same_day_warning and event.game_date is not None:
        text = f"{text}\n\n{same_day_signup_warning(event.game_date)}"
    return f"{text}\n\n{master_contacts_section(masters)}"


class ConfirmationNotificationService:
    """Delivers ordered, admin-triggered notifications to event participants."""

    NOTIFICATION_OPERATIONS = frozenset(
        {
            Operation.OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
        }
    )

    def __init__(
        self,
        events: EventRepository,
        catalog: RegistrationCatalog,
        users: UserRepository,
        transport: DeferredTransport,
        participant_secret: str,
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.users = users
        self.transport = transport
        self.participant_secret = participant_secret

    @staticmethod
    def _identity(user: User) -> tuple[Platform, int]:
        if isinstance(user, TelegramUser):
            return Platform.TELEGRAM, user.tg_id
        return Platform.VK, user.vk_id

    async def _recipients(
        self,
        event: Event,
        attendance_status: AttendanceStatus,
        *,
        main_roster_only: bool = False,
    ) -> list[NotificationRecipient]:
        await self.catalog.ensure_migrated(event)
        all_registrations = list(await self.catalog.registrations.list_for_event(event.event_id))
        main_roster_keys = (
            {
                registration.participant_key
                for registration in _active_main_roster(all_registrations, event.player_amount)
            }
            if main_roster_only
            else None
        )
        registrations = [
            registration
            for registration in all_registrations
            if registration.attendance_status is attendance_status
            and (main_roster_keys is None or registration.participant_key in main_roster_keys)
        ]
        registrations.sort(key=lambda registration: (registration.created_at, registration.participant_key))
        participant_keys = {registration.participant_key for registration in registrations}
        recipients: list[NotificationRecipient] = []
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            if key in participant_keys:
                recipients.append(NotificationRecipient(platform, uid, user))
        return recipients

    async def notify(self, command: OrderedRegistrationCommand) -> int:
        if command.operation not in self.NOTIFICATION_OPERATIONS:
            raise ValueError("command does not send a participant notification")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if command.operation is Operation.SEND_CONFIRMED_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            text = confirmed_notification_text(event.name, command.payload.text, event.game_date)
            attendance_status = AttendanceStatus.CONFIRMED
            main_roster_only = is_plain_chat_link(command.payload.text)
            buttons = [main_menu_button()]
        else:
            if event.confirmation_deadline is None:
                raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
            deadline = format_confirmation_deadline(event.confirmation_deadline)
            reference = event_reference(event.name, event.game_date)
            if command.operation is Operation.OPEN_CONFIRMATION:
                text = f"Подтверждение участия в игре {reference} открыто! Подтвердите участие до {deadline} (мск)."
            else:
                text = f"Напоминаем: подтвердите участие в игре {reference} до {deadline} (мск)."
            attendance_status = AttendanceStatus.WAITING
            main_roster_only = False
            buttons = [confirmation_button(event.event_id)]

        delivered = 0
        for recipient in await self._recipients(
            event,
            attendance_status,
            main_roster_only=main_roster_only,
        ):
            request_id = (
                f"{command.operation_id}:confirmation-notification:{recipient.platform.value}:{recipient.user_id}"
            )
            if recipient.user.last_delivery_operation_id == request_id:
                continue
            await self.transport.send(
                platform=recipient.platform,
                user_id=recipient.user_id,
                request_id=request_id,
                text=text,
                buttons=buttons,
            )
            await self.users.claim_delivery(recipient.platform, recipient.user_id, request_id, buttons)
            delivered += 1
        return delivered

    async def notify_confirming_participant(
        self,
        command: OrderedRegistrationCommand,
        confirmation_text: str,
        confirmation_buttons: Sequence[Button],
    ) -> int:
        """Deliver confirmation success and every earlier master notification as one retry unit."""

        if command.operation is not Operation.CONFIRM:
            raise ValueError("command does not confirm a participant")
        user = await self.users.get(command.platform, command.platform_user_id)
        if user is not None and user.last_delivery_operation_id == command.operation_id:
            return 0
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")

        await self.transport.send(
            platform=command.platform,
            user_id=command.platform_user_id,
            request_id=f"{command.operation_id}:confirmation-result",
            text=confirmation_text,
            buttons=confirmation_buttons,
        )
        registrations = list(await self.catalog.registrations.list_for_event(event.event_id))
        link_recipients = _chat_link_recipient_keys(registrations, event.player_amount)
        delivered_notifications = [
            (index, stored_text)
            for index, stored_text in enumerate(event.confirmed_notifications)
            if not is_plain_chat_link(stored_text) or command.participant_key in link_recipients
        ]
        for index, stored_text in delivered_notifications:
            await self.transport.send(
                platform=command.platform,
                user_id=command.platform_user_id,
                request_id=f"{command.operation_id}:past-confirmed-notification:{index}",
                text=confirmed_notification_text(event.name, stored_text, event.game_date),
                buttons=[main_menu_button()],
            )
        # One command-level marker is written only after the complete bundle is
        # accepted. No per-notification/per-recipient delivery state is stored.
        delivered_buttons = [main_menu_button()] if delivered_notifications else confirmation_buttons
        await self.users.claim_delivery(
            command.platform,
            command.platform_user_id,
            command.operation_id,
            delivered_buttons,
        )
        return len(delivered_notifications)

    async def notify_reserve_promotion(
        self,
        command: OrderedRegistrationCommand,
        *,
        event_id: str | None = None,
    ) -> int:
        """Notify the one reserve player promoted by this roster mutation."""

        if command.operation not in {Operation.CANCEL, Operation.CONFIRM, Operation.REMOVE_PARTICIPANT}:
            raise ValueError("command does not promote a reserve participant")
        event = await self.events.get(event_id or command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_reserve_promotion_operation_id != command.operation_id:
            return 0
        if event.last_reserve_promotion_delivered_operation_id == command.operation_id:
            return 0
        promoted_key = event.last_reserve_promotion_participant_key
        if promoted_key is None:
            return 0

        recipient: NotificationRecipient | None = None
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            if participant_key(self.participant_secret, platform, uid, event.event_id) == promoted_key:
                recipient = NotificationRecipient(platform, uid, user)
                break
        if recipient is None:
            LOGGER.warning(
                "reserve_promotion_recipient_missing",
                extra={"event_id": event.event_id, "operation_id": command.operation_id},
            )
            return 0

        request_id = f"{command.operation_id}:reserve-promotion"
        if recipient.user.last_delivery_operation_id == request_id:
            return 0
        await self.transport.send(
            platform=recipient.platform,
            user_id=recipient.user_id,
            request_id=request_id,
            text=(
                "Место освободилось: вы перешли из запаса в основной состав игры "
                f"{event_reference(event.name, event.game_date)}.\n\n"
                "После подтверждения участия мастера отправят вам ссылку на чат игры отдельным сообщением.\n\n"
                "Посмотрите актуальный состав игроков: "
                f"{event.public_table_public_url}"
            ),
            buttons=[main_menu_button()],
        )
        await self.events.mark_reserve_promotion_delivered(event.event_id, command.operation_id)
        await self.users.claim_delivery(
            recipient.platform,
            recipient.user_id,
            request_id,
            [main_menu_button()],
        )
        return 1

    async def notify_confirmed_link_promotion(
        self,
        command: OrderedRegistrationCommand,
        *,
        event_id: str | None = None,
    ) -> int:
        """Replay chat links when a confirmed reserve player enters the confirmed-player limit."""

        if command.operation not in {Operation.CANCEL, Operation.CONFIRM, Operation.REMOVE_PARTICIPANT}:
            raise ValueError("command does not promote a confirmed link recipient")
        event = await self.events.get(event_id or command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_confirmed_link_promotion_operation_id != command.operation_id:
            return 0
        if event.last_confirmed_link_promotion_delivered_operation_id == command.operation_id:
            return 0
        promoted_key = event.last_confirmed_link_promotion_participant_key
        if promoted_key is None:
            return 0

        recipient: NotificationRecipient | None = None
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            if participant_key(self.participant_secret, platform, uid, event.event_id) == promoted_key:
                recipient = NotificationRecipient(platform, uid, user)
                break
        if recipient is None:
            LOGGER.warning(
                "confirmed_link_promotion_recipient_missing",
                extra={"event_id": event.event_id, "operation_id": command.operation_id},
            )
            return 0

        links = [text for text in event.confirmed_notifications if is_plain_chat_link(text)]
        last_request_id: str | None = None
        for index, link in enumerate(links):
            last_request_id = f"{command.operation_id}:confirmed-link-promotion:{index}"
            await self.transport.send(
                platform=recipient.platform,
                user_id=recipient.user_id,
                request_id=last_request_id,
                text=confirmed_notification_text(event.name, link, event.game_date),
                buttons=[main_menu_button()],
            )
        await self.events.mark_confirmed_link_promotion_delivered(event.event_id, command.operation_id)
        if last_request_id is not None:
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                last_request_id,
                [main_menu_button()],
            )
        return len(links)

    async def notify_removed_participants(self, command: OrderedRegistrationCommand) -> int:
        if command.operation is not Operation.REMOVE_PARTICIPANT:
            raise ValueError("command does not remove participants")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_participant_removal_operation_id != command.operation_id:
            return 0

        recipients: dict[str, NotificationRecipient] = {}
        participant_keys = set(event.participant_removal_keys)
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            if key in participant_keys:
                recipients[key] = NotificationRecipient(platform, uid, user)

        text = f"Ваша запись на игру {event_reference(event.name, event.game_date)} удалена."

        delivered = 0
        notified_keys = set(event.participant_removal_notified_keys)
        for key in event.participant_removal_keys:
            if key in notified_keys:
                continue
            recipient = recipients.get(key)
            if recipient is None:
                LOGGER.warning(
                    "participant_removal_recipient_missing",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            request_id = f"{command.operation_id}:participant-removal:{recipient.platform.value}:{recipient.user_id}"
            await self.transport.send(
                platform=recipient.platform,
                user_id=recipient.user_id,
                request_id=request_id,
                text=text,
                buttons=[main_menu_button()],
            )
            await self.events.mark_participant_removal_delivered(
                event.event_id,
                command.operation_id,
                key,
            )
            notified_keys.add(key)
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                request_id,
                [main_menu_button()],
            )
            delivered += 1
        return delivered

    async def notify_sifted_participants(self, command: OrderedRegistrationCommand) -> int:
        if command.operation is not Operation.REMOVE_WAITING_PARTICIPANTS:
            raise ValueError("command does not sift participants")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_participant_removal_operation_id != command.operation_id:
            return 0

        users_by_key: dict[str, NotificationRecipient] = {}
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            users_by_key[key] = NotificationRecipient(platform, uid, user)

        delivered = 0
        notified_entries = set(event.participant_removal_notified_keys)
        chat_links = [text for text in event.confirmed_notifications if is_plain_chat_link(text)]
        for snapshot_entry in event.participant_removal_keys:
            if snapshot_entry in notified_entries:
                continue
            parsed = _parse_sift_snapshot_entry(snapshot_entry)
            if parsed is None:
                LOGGER.warning(
                    "confirmation_sift_snapshot_invalid",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            transition, key = parsed
            recipient = users_by_key.get(key)
            if recipient is None:
                LOGGER.warning(
                    "confirmation_sift_recipient_missing",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            if transition == SIFT_PROMOTED_PREFIX:
                messages = [
                    (
                        "Вы перешли из запаса в основной состав игры "
                        f"{event_reference(event.name, event.game_date)}.\n\n"
                        "Посмотрите актуальный состав игроков: "
                        f"{event.public_table_public_url}"
                    ),
                    *(confirmed_notification_text(event.name, link, event.game_date) for link in chat_links),
                ]
                transition_name = "promotion"
            else:
                messages = [
                    (
                        "Вы не усепели подтвердить или отменить ваше участие в игре "
                        f"«{event.name}». Мы были вынуждены переместить вашу заявку в запас"
                    )
                ]
                transition_name = "demotion"
            last_request_id = ""
            for index, text in enumerate(messages):
                last_request_id = (
                    f"{command.operation_id}:confirmation-sift:{transition_name}:"
                    f"{recipient.platform.value}:{recipient.user_id}:{index}"
                )
                await self.transport.send(
                    platform=recipient.platform,
                    user_id=recipient.user_id,
                    request_id=last_request_id,
                    text=text,
                    buttons=[main_menu_button()],
                )
            await self.events.mark_participant_removal_delivered(
                event.event_id,
                command.operation_id,
                snapshot_entry,
            )
            notified_entries.add(snapshot_entry)
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                last_request_id,
                [main_menu_button()],
            )
            delivered += 1
        return delivered


class OrderedMutationService:
    """Authoritative worker-time validation, YDB mutation, and showcase refresh."""

    def __init__(
        self,
        events: EventRepository,
        catalog: RegistrationCatalog,
        users: UserRepository | None = None,
        participant_secret: str | None = None,
        admins: AdminConfigProvider | None = None,
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.users = users
        self.participant_secret = participant_secret
        self.admins = admins

    async def _require_event_leader(self, command: OrderedRegistrationCommand) -> None:
        identity = BotIdentity(platform=command.platform, platform_user_id=command.platform_user_id)
        if identity in await self.events.list_leaders(command.event_id):
            return
        if self.admins is not None and await self.admins.is_admin(command.platform, command.platform_user_id):
            return
        raise OperationNotAllowed("Только мастера этой игры и администраторы бота могут изменять её данные")

    async def _require_admin(self, command: OrderedRegistrationCommand) -> None:
        if self.admins is not None and await self.admins.is_admin(command.platform, command.platform_user_id):
            return
        raise OperationNotAllowed("Только администраторы бота могут разархивировать игры")

    async def _refresh_pass_table(self, event: Event) -> None:
        if event.pass_table_resource_path is None:
            return
        if self.users is None or self.participant_secret is None:
            raise RuntimeError("pass table refresh requires users and participant secret")
        profiles = await _event_pass_profiles(
            event,
            self.events,
            self.catalog.registrations,
            self.users,
            self.participant_secret,
        )
        await self.catalog.showcase.replace_pass_table(event.pass_table_resource_path, profiles)

    async def _record_reserve_promotion(
        self,
        event: Event,
        command: OrderedRegistrationCommand,
        registration: Registration | None,
    ) -> None:
        if event.last_reserve_promotion_operation_id == command.operation_id:
            return
        promoted_key: str | None = None
        if registration is not None and registration.attendance_status is not AttendanceStatus.CANCELLED:
            active = [
                row
                for row in await self.catalog.registrations.list_for_event(event.event_id)
                if row.attendance_status is not AttendanceStatus.CANCELLED
            ]
            active.sort(key=lambda row: (row.created_at, row.participant_key))
            target_index = next(
                (index for index, row in enumerate(active) if row.participant_key == registration.participant_key),
                None,
            )
            if target_index is not None and target_index < event.player_amount < len(active):
                promoted_key = active[event.player_amount].participant_key
        await self.events.set_reserve_promotion(event.event_id, command.operation_id, promoted_key)
        event.last_reserve_promotion_operation_id = command.operation_id
        event.last_reserve_promotion_participant_key = promoted_key

    async def _record_confirmed_link_promotion(
        self,
        event: Event,
        command: OrderedRegistrationCommand,
        registration: Registration | None,
    ) -> None:
        if event.last_confirmed_link_promotion_operation_id == command.operation_id:
            return
        promoted_key: str | None = None
        if registration is not None and registration.attendance_status is not AttendanceStatus.CANCELLED:
            active = [
                row
                for row in await self.catalog.registrations.list_for_event(event.event_id)
                if row.attendance_status is not AttendanceStatus.CANCELLED
            ]
            active.sort(key=lambda row: (row.created_at, row.participant_key))
            target_index = next(
                (index for index, row in enumerate(active) if row.participant_key == registration.participant_key),
                None,
            )
            if target_index is not None and target_index < event.player_amount < len(active):
                promoted = active[event.player_amount]
                if promoted.attendance_status is AttendanceStatus.CONFIRMED:
                    promoted_key = promoted.participant_key
        await self.events.set_confirmed_link_promotion(event.event_id, command.operation_id, promoted_key)
        event.last_confirmed_link_promotion_operation_id = command.operation_id
        event.last_confirmed_link_promotion_participant_key = promoted_key

    async def apply(self, command: OrderedRegistrationCommand) -> MutationResult:
        event = await self.events.get(command.event_id)
        if event is None:
            if command.operation in {Operation.ARCHIVE_EVENT, Operation.DELETE_EVENT}:
                return MutationResult("Игра уже отсутствует в постоянном списке")
            raise EventNotFound("Игра уже удалена или не существует")

        if command.operation is Operation.UNARCHIVE_EVENT:
            await self._require_admin(command)
            if event.archived_at is None:
                return MutationResult("Игра уже разархивирована")
            await self.events.unarchive(event.event_id, datetime.now(UTC))
            return MutationResult("Игра разархивирована; регистрация остаётся закрытой")

        if event.archived_at is not None:
            if command.operation in {Operation.ARCHIVE_EVENT, Operation.DELETE_EVENT}:
                return MutationResult("Игра уже архивирована")
            if command.operation in {
                Operation.ENLIST,
                Operation.CONFIRM,
                Operation.UPDATE_CHARACTER_WISH,
                Operation.UPDATE_CO_PLAYER_WISH,
                Operation.CANCEL,
            }:
                raise OperationNotAllowed("Игра больше не активна; запись и изменение данных недоступны.")
            raise OperationNotAllowed("Игра уже архивирована")

        administrative_operations = {
            Operation.OPEN_REGISTRATION,
            Operation.OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.UPDATE_PLAYER_AMOUNT,
            Operation.REMOVE_PARTICIPANT,
            Operation.REMOVE_WAITING_PARTICIPANTS,
            Operation.CLOSE_EVENT,
            Operation.ARCHIVE_EVENT,
            Operation.DELETE_EVENT,
        }
        if command.operation in administrative_operations:
            await self._require_event_leader(command)

        status_operations = {
            Operation.OPEN_REGISTRATION: (EventStatus.CREATED, "Установлен Статус «Регистрация»"),
            Operation.CLOSE_EVENT: (EventStatus.CLOSED, "Установлен Статус «Закрытие регистрации»"),
        }
        if command.operation is Operation.OPEN_CONFIRMATION:
            assert isinstance(command.payload, ConfirmationDeadlinePayload)
            status_changed = event.status is not EventStatus.CONFIRMATION_OPEN
            await self.events.open_confirmation(event.event_id, command.payload.deadline)
            return MutationResult(
                "Установлен Статус «Подтверждение»",
                notifications_enabled=status_changed,
            )
        if command.operation is Operation.SEND_CONFIRMATION_REMINDER:
            if event.status is not EventStatus.CONFIRMATION_OPEN:
                raise OperationNotAllowed("Подтверждение участия для этой игры не открыто")
            if event.confirmation_deadline is None:
                raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
            return MutationResult("Напоминание о подтверждении отправлено")
        if command.operation is Operation.SEND_CONFIRMED_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            await self.events.append_confirmed_notification(
                event.event_id,
                command.payload.text,
                command.operation_id,
            )
            return MutationResult("Уведомление подтвердившим участие отправлено")
        if command.operation is Operation.UPDATE_PLAYER_AMOUNT:
            assert isinstance(command.payload, PlayerAmountPayload)
            event = await self.catalog.ensure_migrated(event)
            await self.events.set_player_amount(event.event_id, command.payload.player_amount)
            event.player_amount = command.payload.player_amount
            await self.catalog.refresh(event)
            return MutationResult(f"Количество игроков изменено: {event.player_amount}")
        if command.operation in status_operations:
            status, message = status_operations[command.operation]
            status_changed = await self.events.set_status(event.event_id, status)
            return MutationResult(message, notifications_enabled=status_changed)
        if command.operation in {Operation.ARCHIVE_EVENT, Operation.DELETE_EVENT}:
            # DELETE_EVENT can still be present in FIFO or in a stale admin
            # keyboard from an older deployment. Treat it as archival so a
            # published workbook, its registrations, and its permanent game
            # entry can never be removed by an old command.
            await self.catalog.archive(event)
            return MutationResult("Игра архивирована; таблицы и записи сохранены")

        event = await self.catalog.ensure_migrated(event)
        if command.operation is Operation.REMOVE_WAITING_PARTICIPANTS:
            if event.last_participant_removal_operation_id == command.operation_id:
                snapshot = event.participant_removal_keys
            else:
                registrations = [
                    registration
                    for registration in await self.catalog.registrations.list_for_event(event.event_id)
                    if registration.attendance_status is not AttendanceStatus.CANCELLED
                ]
                registrations.sort(key=lambda row: (row.created_at, row.participant_key))
                before_main = {row.participant_key for row in registrations[: event.player_amount]}
                reordered = sorted(
                    registrations,
                    key=lambda row: (
                        row.attendance_status is not AttendanceStatus.CONFIRMED,
                        row.created_at,
                        row.participant_key,
                    ),
                )
                after_main = {row.participant_key for row in reordered[: event.player_amount]}
                promoted_keys = [
                    row.participant_key for row in reordered if row.participant_key in after_main - before_main
                ]
                demoted_keys = [
                    row.participant_key for row in registrations if row.participant_key in before_main - after_main
                ]
                snapshot = [
                    *(_sift_snapshot_entry(SIFT_PROMOTED_PREFIX, key) for key in promoted_keys),
                    *(_sift_snapshot_entry(SIFT_DEMOTED_PREFIX, key) for key in demoted_keys),
                ]
                await self.events.set_participant_removal(
                    event.event_id,
                    command.operation_id,
                    snapshot,
                )
            active_registrations = [
                registration
                for registration in await self.catalog.registrations.list_for_event(event.event_id)
                if registration.attendance_status is not AttendanceStatus.CANCELLED
            ]
            reordered = sorted(
                active_registrations,
                key=lambda row: (
                    row.attendance_status is not AttendanceStatus.CONFIRMED,
                    row.created_at,
                    row.participant_key,
                ),
            )
            if [row.participant_key for row in active_registrations] != [row.participant_key for row in reordered]:
                await self.catalog.registrations.sift_waiting_to_end(event.event_id)
            await self.catalog.refresh(event)
            promoted_count = sum(entry.startswith(SIFT_PROMOTED_PREFIX) for entry in snapshot)
            demoted_count = sum(entry.startswith(SIFT_DEMOTED_PREFIX) for entry in snapshot)
            return MutationResult(
                f"Заявки пересортированы: в основной состав перемещено {promoted_count}, в запас — {demoted_count}"
            )

        assert command.participant_key is not None
        if command.operation is Operation.UPDATE_PROFILE:
            if self.users is None or self.participant_secret is None:
                raise RuntimeError("profile update requires users and participant secret")
            expected_key = participant_key(
                self.participant_secret,
                command.platform,
                command.platform_user_id,
                event.event_id,
            )
            if command.participant_key != expected_key:
                raise OperationNotAllowed("Некорректная запись участника")
            user = await self.users.get(command.platform, command.platform_user_id)
            if user is None or not user.profile_complete or user.full_name is None:
                raise OperationNotAllowed("Сначала полностью заполните профиль")
            registration, leaders = await asyncio.gather(
                self.catalog.registrations.get(event.event_id, command.participant_key),
                self.events.list_leaders(event.event_id),
            )
            identity = BotIdentity(
                platform=command.platform,
                platform_user_id=command.platform_user_id,
            )
            if registration is None and identity not in leaders:
                return MutationResult("Запись игрока уже отсутствует", notifications_enabled=False)
            if isinstance(user, TelegramUser):
                vk_profile = user.vk_url or ""
                telegram_handle = user.telegram_handle
            else:
                vk_profile = f"https://vk.com/id{user.vk_id}"
                telegram_handle = user.telegram_handle
            telegram_profile = None if telegram_handle is None else f"https://t.me/{telegram_handle.removeprefix('@')}"
            if registration is not None:
                await self.catalog.registrations.update_profile(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_key=command.participant_key,
                    display_name=surname_and_name(user.full_name),
                    nickname=user.nickname,
                    larp_experience=bool(user.larp_experience),
                    crossplay=bool(user.crossplay),
                    vk_profile=vk_profile,
                    telegram_profile=telegram_profile,
                )
                await self.catalog.refresh(event)
            await self._refresh_pass_table(event)
            return MutationResult("Профиль игрока обновлён", notifications_enabled=False)
        if command.operation is Operation.ENLIST:
            if event.status is EventStatus.CLOSED:
                raise OperationNotAllowed("Регистрация на эту игру закрыта")
            if not event.registration_is_open():
                raise OperationNotAllowed("Регистрация на эту игру ещё не открыта")
            assert isinstance(command.payload, EnlistPayload)
            existing = await self.catalog.registrations.get(event.event_id, command.participant_key)
            if (
                existing is not None
                and existing.attendance_status is not AttendanceStatus.CANCELLED
                and existing.last_operation_id != command.operation_id
            ):
                raise OperationNotAllowed(f"Вы уже записаны на игру {event_reference(event.name, event.game_date)}")
            larp_experience = command.payload.larp_experience
            crossplay = command.payload.crossplay
            nickname = command.payload.nickname
            vk_profile = command.payload.vk_profile
            telegram_profile = command.payload.telegram_profile
            if self.users is not None:
                user = await self.users.get(command.platform, command.platform_user_id)
                if user is None or not user.profile_complete:
                    raise OperationNotAllowed("Сначала полностью заполните профиль")
                nickname = user.nickname
                if larp_experience is None or crossplay is None or not vk_profile:
                    larp_experience = user.larp_experience
                    crossplay = user.crossplay
                    if isinstance(user, TelegramUser):
                        vk_profile = user.vk_url or ""
                    else:
                        vk_profile = f"https://vk.com/id{user.vk_id}"
                        if telegram_profile is None and user.telegram_handle is not None:
                            telegram_profile = f"https://t.me/{user.telegram_handle.removeprefix('@')}"
            await self.catalog.registrations.enlist(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                display_name=surname_and_name(command.payload.display_name),
                nickname=nickname,
                wish_play=command.payload.wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            )
            event = await self.catalog.refresh(event)
            active = [
                row
                for row in await self.catalog.registrations.list_for_event(event.event_id)
                if row.attendance_status is not AttendanceStatus.CANCELLED
            ]
            active.sort(key=lambda row: (row.created_at, row.participant_key))
            queue_position = next(
                index for index, row in enumerate(active, start=1) if row.participant_key == command.participant_key
            )
            same_day_warning = False
            if event.game_date is not None and not command.payload.same_day_warning_shown:
                if self.participant_secret is None:
                    raise RuntimeError("same-day signup validation requires participant secret")
                same_day_warning = bool(
                    await player_registrations_on_date(
                        self.events,
                        self.catalog,
                        self.participant_secret,
                        command.platform,
                        command.platform_user_id,
                        event.game_date,
                        statuses={AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED},
                        exclude_event_id=event.event_id,
                    )
                )
            buttons = (confirmation_button(event.event_id),) if event.status is EventStatus.CONFIRMATION_OPEN else None
            return MutationResult(
                enlistment_success_text(
                    event,
                    queue_position,
                    masters=await event_master_records(self.events, self.users, event.event_id),
                    same_day_warning=same_day_warning,
                ),
                buttons=buttons,
            )

        if command.operation is Operation.CONFIRM and event.status is not EventStatus.CONFIRMATION_OPEN:
            if event.status is EventStatus.CREATED:
                raise OperationNotAllowed("Подтверждение участия в этой игре ещё не открыто")
            raise OperationNotAllowed("Подтверждение участия в этой игре закрыто")

        registration = await self.catalog.registrations.get(event.event_id, command.participant_key)
        if command.operation is Operation.REMOVE_PARTICIPANT:
            if registration is not None and registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Запись этого игрока уже отменена")
            if event.last_participant_removal_operation_id != command.operation_id:
                await self.events.set_participant_removal(
                    event.event_id,
                    command.operation_id,
                    [command.participant_key] if registration is not None else [],
                )
            await self._record_reserve_promotion(event, command, registration)
            await self._record_confirmed_link_promotion(event, command, registration)
            if registration is not None:
                await self.catalog.registrations.remove(
                    event.event_id,
                    participant_key=command.participant_key,
                )
            # Refresh even when the row is already absent. The first attempt may
            # have deleted it before a transient workbook or pass-table failure.
            await self.catalog.refresh(event)
            await self._refresh_pass_table(event)
            return MutationResult("Игрок удалён" if registration is not None else "Игрок уже удалён")
        if registration is None:
            raise RegistrationNotFound("Сначала запишитесь на эту игру")
        if command.operation is Operation.CONFIRM:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова запишитесь на эту игру")
            assert isinstance(command.payload, CharacterWishPayload)
            replaced_event: Event | None = None
            replaced_registration: Registration | None = None
            replaced_participant_key: str | None = None
            if event.game_date is not None:
                if self.participant_secret is None:
                    raise RuntimeError("same-day confirmation validation requires participant secret")
                conflicts = await player_registrations_on_date(
                    self.events,
                    self.catalog,
                    self.participant_secret,
                    command.platform,
                    command.platform_user_id,
                    event.game_date,
                    statuses={AttendanceStatus.CONFIRMED},
                    exclude_event_id=event.event_id,
                )
                conflict_ids = {conflict.event_id for conflict, _ in conflicts}
                if conflict_ids and conflict_ids != {command.payload.replaced_event_id}:
                    raise OperationNotAllowed(
                        "Вы уже подтвердили участие в другой игре этого дня. "
                        "Начните подтверждение заново и подтвердите замену."
                    )
                if command.payload.replaced_event_id is not None:
                    replaced_event = await self.events.get(command.payload.replaced_event_id)
                    if replaced_event is None or replaced_event.event_id == event.event_id:
                        raise OperationNotAllowed("Игра, участие в которой нужно заменить, не найдена")
                    if replaced_event.game_date != event.game_date:
                        raise OperationNotAllowed("Игры проводятся в разные дни")
                    replaced_participant_key = participant_key(
                        self.participant_secret,
                        command.platform,
                        command.platform_user_id,
                        replaced_event.event_id,
                    )
                    replaced_registration = await self.catalog.get(
                        replaced_event,
                        replaced_participant_key,
                    )
                    already_applied = (
                        registration.last_operation_id == command.operation_id
                        and registration.attendance_status is AttendanceStatus.CONFIRMED
                    )
                    if (
                        replaced_registration is None
                        or replaced_registration.attendance_status is not AttendanceStatus.CONFIRMED
                    ) and not already_applied:
                        replaced_event = None
                        replaced_participant_key = None
            elif command.payload.replaced_event_id is not None:
                raise OperationNotAllowed("Для этой игры не задана дата")
            if replaced_event is not None:
                await self._record_reserve_promotion(replaced_event, command, replaced_registration)
                await self._record_confirmed_link_promotion(replaced_event, command, replaced_registration)
            await self.catalog.registrations.confirm(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                character_wish=command.payload.character_wish,
                replaced_event_id=replaced_event.event_id if replaced_event is not None else None,
                replaced_participant_key=replaced_participant_key,
            )
            if replaced_event is not None:
                await self.catalog.refresh(replaced_event)
                await self._refresh_pass_table(replaced_event)
            event = await self.catalog.refresh(event)
            await self._refresh_pass_table(event)
            main_roster_keys = {
                row.participant_key
                for row in _active_main_roster(
                    list(await self.catalog.registrations.list_for_event(event.event_id)),
                    event.player_amount,
                )
            }
            return MutationResult(
                confirmation_success_text(
                    event,
                    command.payload.character_wish,
                    in_main_roster=command.participant_key in main_roster_keys,
                    masters=await event_master_records(self.events, self.users, event.event_id),
                ),
                replaced_event_id=replaced_event.event_id if replaced_event is not None else None,
            )
        if command.operation is Operation.UPDATE_CHARACTER_WISH:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова запишитесь на эту игру")
            if registration.attendance_status is AttendanceStatus.WAITING and not registration.character_wish:
                raise OperationNotAllowed("Впервые укажите пожелания при подтверждении участия")
            assert isinstance(command.payload, CharacterWishPayload)
            await self.catalog.registrations.update_character_wish(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                character_wish=command.payload.character_wish,
            )
            await self.catalog.refresh(event)
            return MutationResult("Пожелания по персонажу обновлены")
        if command.operation is Operation.UPDATE_CO_PLAYER_WISH:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова запишитесь на эту игру")
            assert isinstance(command.payload, CoPlayerWishPayload)
            await self.catalog.registrations.update_co_player_wish(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                wish_play=command.payload.wish_play,
            )
            await self.catalog.refresh(event)
            return MutationResult("Пожелания по соигрокам обновлены")
        if command.operation is Operation.CANCEL:
            await self._record_reserve_promotion(event, command, registration)
            await self._record_confirmed_link_promotion(event, command, registration)
            await self.catalog.registrations.cancel(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
            )
            await self.catalog.refresh(event)
            await self._refresh_pass_table(event)
            return MutationResult("Участие отменено")
        raise AssertionError(f"unhandled operation: {command.operation}")
