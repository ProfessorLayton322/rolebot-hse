from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from larp_bot.domain.models import (
    AttendanceStatus,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    Registration,
    ReplyContext,
    TelegramUser,
    User,
)
from larp_bot.domain.security import participant_key

from .deadlines import format_confirmation_deadline
from .ports import (
    DeferredTransport,
    EventRepository,
    OrderedCommandPublisher,
    RegistrationRepository,
    RegistrationShowcaseRepository,
    UserRepository,
)

LOGGER = logging.getLogger("larp_bot.application.services")


class DomainError(RuntimeError):
    pass


class EventNotFound(DomainError):
    pass


class RegistrationNotFound(DomainError):
    pass


class OperationNotAllowed(DomainError):
    pass


def event_slug(name: str) -> str:
    transliterated = name.casefold().replace("ё", "е")
    slug = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")
    return (slug or "game")[:40]


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
        await self.refresh(event)
        migrated_at = datetime.now(UTC)
        await self.events.mark_registrations_migrated(event.event_id, migrated_at)
        event.registrations_migrated_at = migrated_at
        return event

    async def get(self, event: Event, participant_key: str) -> Registration | None:
        await self.ensure_migrated(event)
        return await self.registrations.get(event.event_id, participant_key)

    async def refresh(self, event: Event) -> None:
        rows = await self.registrations.list_for_event(event.event_id)
        await self.showcase.replace(event, rows)

    async def archive(self, event: Event) -> None:
        await self.events.set_status(event.event_id, EventStatus.CLOSED)

    async def delete(self, event: Event) -> None:
        """Archive legacy delete requests without removing permanent game data."""
        await self.archive(event)


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

    async def _event(self, event_id: str) -> Event:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        return event

    async def get_registration(self, event_id: str, platform: Platform, user_id: int) -> Registration | None:
        event = await self._event(event_id)
        return await self.catalog.get(event, self.key(platform, user_id, event_id))

    async def registered_games_page(
        self,
        platform: Platform,
        user_id: int,
        *,
        after: tuple[datetime, str] | None = None,
        event_limit: int = 10,
        concurrency: int = 3,
    ) -> tuple[list[tuple[Event, Registration]], tuple[datetime, str] | None]:
        candidates = list(await self.events.list_page(after=after, limit=event_limit))
        semaphore = asyncio.Semaphore(concurrency)

        async def inspect(event: Event) -> tuple[Event, Registration] | None:
            async with semaphore:
                found = await self.catalog.get(event, self.key(platform, user_id, event.event_id))
                return (event, found) if found is not None else None

        matches = [item for item in await asyncio.gather(*(inspect(e) for e in candidates)) if item]
        cursor = None
        if len(candidates) == event_limit:
            last = candidates[-1]
            cursor = (last.created_at, last.event_id)
        return matches, cursor

    async def enqueue(
        self,
        *,
        operation: Operation,
        event_id: str,
        platform: Platform,
        user_id: int,
        payload: EnlistPayload
        | CharacterWishPayload
        | ConfirmationDeadlinePayload
        | NotificationPayload
        | EmptyPayload,
        reply_context: ReplyContext,
        idempotency_key: str | None = None,
    ) -> OrderedRegistrationCommand:
        event = await self._event(event_id)
        if operation is Operation.ENLIST and event.status is EventStatus.CLOSED:
            raise OperationNotAllowed("Регистрация на эту игру закрыта")
        participant = None
        if operation not in {
            Operation.OPEN_REGISTRATION,
            Operation.OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.CLOSE_EVENT,
            Operation.DELETE_EVENT,
        }:
            participant = self.key(platform, user_id, event_id)
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

    async def create_event(self, name: str) -> Event:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Название игры не может быть пустым")
        event_id = str(uuid4())
        disk_path = f"disk:/larp-bot/events/{event_id}-{event_slug(clean_name)}.xlsx"
        public_url = await self.showcase.create_event_workbook(disk_path)
        event = Event(
            event_id=event_id,
            name=clean_name,
            disk_resource_path=disk_path,
            public_registration_url=public_url,
        )
        try:
            await self.events.create(event)
        except Exception:
            await self.showcase.delete_event_workbook(disk_path)
            raise
        return event

    async def create_pass_table(self, event_id: str) -> PassTableExport:
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.pass_table_public_url is not None:
            return PassTableExport(event.pass_table_public_url, created=False, row_count=None)

        await self.catalog.ensure_migrated(event)
        confirmed = [
            registration
            for registration in await self.catalog.registrations.list_for_event(event.event_id)
            if registration.attendance_status is AttendanceStatus.CONFIRMED
        ]
        pass_profiles: dict[str, PassDetails] = {}
        for user in await self.users.list_all():
            if not user.profile_complete or user.needs_pass is not True or user.pass_details is None:
                continue
            platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
            uid = user.tg_id if isinstance(user, TelegramUser) else user.vk_id
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            pass_profiles[key] = user.pass_details
        profiles = [pass_profiles[row.participant_key] for row in confirmed if row.participant_key in pass_profiles]

        disk_path = f"disk:/larp-bot/passes/{event.event_id}.xlsx"
        public_url = await self.showcase.create_pass_table(disk_path, profiles)
        try:
            if not await self.events.set_pass_table(event.event_id, disk_path, public_url):
                raise EventNotFound("Игра была удалена во время создания таблицы")
        except Exception:
            await self.showcase.delete_pass_table(disk_path)
            raise
        return PassTableExport(public_url, created=True, row_count=len(profiles))


@dataclass(frozen=True)
class NotificationRecipient:
    platform: Platform
    user_id: int
    user: User


_TELEGRAM_CHAT_HOSTS = frozenset(
    {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog"}
)
_TELEGRAM_CHAT_PATH_RE = re.compile(r"^/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_]{3,31})/?$")
_VK_CHAT_PATH_RE = re.compile(r"^/join/[A-Za-z0-9_=-]+/?$")


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


def confirmed_notification_text(event_name: str, message: str) -> str:
    clean_message = message.strip()
    if not is_plain_chat_link(clean_message):
        return clean_message
    return (
        f"Вы подтвердили своё участие в игре {event_name}! "
        f"Пожалуйста, добавьтесь в чат игры, мы вас очень ждём: {clean_message}"
    )


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
    ) -> list[NotificationRecipient]:
        await self.catalog.ensure_migrated(event)
        participant_keys = {
            registration.participant_key
            for registration in await self.catalog.registrations.list_for_event(event.event_id)
            if registration.attendance_status is attendance_status
        }
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
            text = confirmed_notification_text(event.name, command.payload.text)
            attendance_status = AttendanceStatus.CONFIRMED
        else:
            if event.confirmation_deadline is None:
                raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
            deadline = format_confirmation_deadline(event.confirmation_deadline)
            if command.operation is Operation.OPEN_CONFIRMATION:
                text = f"Подтверждение на игру {event.name} открыто! Дедлайн подтверждения - {deadline}"
            else:
                text = f"Напоминаем о необходимости подтвердить или отменить участие в игре {event.name} до {deadline}!"
            attendance_status = AttendanceStatus.WAITING

        delivered = 0
        for recipient in await self._recipients(event, attendance_status):
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
            )
            await self.users.claim_delivery(recipient.platform, recipient.user_id, request_id)
            delivered += 1
        return delivered


class OrderedMutationService:
    """Authoritative worker-time validation, YDB mutation, and showcase refresh."""

    def __init__(
        self,
        events: EventRepository,
        catalog: RegistrationCatalog,
        users: UserRepository | None = None,
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.users = users

    async def apply(self, command: OrderedRegistrationCommand) -> str:
        event = await self.events.get(command.event_id)
        if event is None:
            if command.operation is Operation.DELETE_EVENT:
                return "Игра уже отсутствует в постоянном списке"
            raise EventNotFound("Игра уже удалена или не существует")

        status_operations = {
            Operation.OPEN_REGISTRATION: (EventStatus.CREATED, "Установлен Статус «Регистрация»"),
            Operation.CLOSE_EVENT: (EventStatus.CLOSED, "Установлен Статус «Закрытие регистрации»"),
        }
        if command.operation is Operation.OPEN_CONFIRMATION:
            assert isinstance(command.payload, ConfirmationDeadlinePayload)
            await self.events.open_confirmation(event.event_id, command.payload.deadline)
            return "Установлен Статус «Подтверждение»"
        if command.operation is Operation.SEND_CONFIRMATION_REMINDER:
            if event.status is not EventStatus.CONFIRMATION_OPEN:
                raise OperationNotAllowed("Подтверждение участия для этой игры не открыто")
            if event.confirmation_deadline is None:
                raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
            return "Напоминание о подтверждении отправлено"
        if command.operation is Operation.SEND_CONFIRMED_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            return "Уведомление подтвердившим участие отправлено"
        if command.operation in status_operations:
            status, message = status_operations[command.operation]
            await self.events.set_status(event.event_id, status)
            return message
        if command.operation is Operation.DELETE_EVENT:
            # DELETE_EVENT can still be present in FIFO or in a stale admin
            # keyboard from an older deployment. Treat it as archival so a
            # published workbook, its registrations, and its permanent game
            # entry can never be removed by an old command.
            await self.catalog.archive(event)
            return "Игра архивирована; таблицы и записи сохранены"

        assert command.participant_key is not None
        await self.catalog.ensure_migrated(event)
        if command.operation is Operation.ENLIST:
            if event.status is EventStatus.CLOSED:
                raise OperationNotAllowed("Регистрация на эту игру закрыта")
            assert isinstance(command.payload, EnlistPayload)
            larp_experience = command.payload.larp_experience
            crossplay = command.payload.crossplay
            vk_profile = command.payload.vk_profile
            telegram_profile = command.payload.telegram_profile
            if self.users is not None:
                user = await self.users.get(command.platform, command.platform_user_id)
                if user is None or not user.profile_complete:
                    raise OperationNotAllowed("Сначала полностью заполните профиль")
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
                display_name=command.payload.display_name,
                wish_play=command.payload.wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            )
            await self.catalog.refresh(event)
            return "Заявка на игру записана"

        if command.operation is Operation.CONFIRM and event.status is not EventStatus.CONFIRMATION_OPEN:
            if event.status is EventStatus.CREATED:
                raise OperationNotAllowed("Подтверждение участия в этой игре ещё не открыто")
            raise OperationNotAllowed("Подтверждение участия в этой игре закрыто")

        registration = await self.catalog.registrations.get(event.event_id, command.participant_key)
        if registration is None:
            raise RegistrationNotFound("Сначала запишитесь на эту игру")
        if command.operation is Operation.CONFIRM:
            assert isinstance(command.payload, CharacterWishPayload)
            await self.catalog.registrations.confirm(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                character_wish=command.payload.character_wish,
            )
            await self.catalog.refresh(event)
            return "Участие подтверждено"
        if command.operation is Operation.UPDATE_CHARACTER_WISH:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова подтвердите участие")
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
            return "Пожелания по персонажу обновлены"
        if command.operation is Operation.CANCEL:
            await self.catalog.registrations.cancel(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
            )
            await self.catalog.refresh(event)
            return "Участие отменено"
        raise AssertionError(f"unhandled operation: {command.operation}")
