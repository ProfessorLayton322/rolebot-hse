from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from larp_bot.domain.models import (
    AttendanceStatus,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    Operation,
    OrderedRegistrationCommand,
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

    async def delete(self, event: Event) -> None:
        await self.showcase.delete_event_workbook(event.disk_resource_path)
        await self.registrations.delete_for_event(event.event_id)
        await self.events.delete(event.event_id)


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
        payload: EnlistPayload | CharacterWishPayload | ConfirmationDeadlinePayload | EmptyPayload,
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


class EventAdministrationService:
    def __init__(self, events: EventRepository, showcase: RegistrationShowcaseRepository) -> None:
        self.events = events
        self.showcase = showcase

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


@dataclass(frozen=True)
class NotificationRecipient:
    platform: Platform
    user_id: int
    user: User


class ConfirmationNotificationService:
    """Delivers explicit admin-triggered notifications to waiting players."""

    NOTIFICATION_OPERATIONS = frozenset({Operation.OPEN_CONFIRMATION, Operation.SEND_CONFIRMATION_REMINDER})

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

    async def _recipients(self, event: Event) -> list[NotificationRecipient]:
        await self.catalog.ensure_migrated(event)
        waiting_keys = {
            registration.participant_key
            for registration in await self.catalog.registrations.list_for_event(event.event_id)
            if registration.attendance_status is AttendanceStatus.WAITING
        }
        recipients: list[NotificationRecipient] = []
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            if key in waiting_keys:
                recipients.append(NotificationRecipient(platform, uid, user))
        return recipients

    async def notify_waiting(self, command: OrderedRegistrationCommand) -> int:
        if command.operation not in self.NOTIFICATION_OPERATIONS:
            raise ValueError("command does not send a confirmation notification")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.confirmation_deadline is None:
            raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
        deadline = format_confirmation_deadline(event.confirmation_deadline)
        if command.operation is Operation.OPEN_CONFIRMATION:
            text = f"Подтверждение на игру {event.name} открыто! Дедлайн подтверждения - {deadline}"
        else:
            text = f"Напоминаем о необходимости подтвердить или отменить участие в игре {event.name} до {deadline}!"

        delivered = 0
        for recipient in await self._recipients(event):
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
                return "Игра и таблица уже удалены"
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
        if command.operation in status_operations:
            status, message = status_operations[command.operation]
            await self.events.set_status(event.event_id, status)
            return message
        if command.operation is Operation.DELETE_EVENT:
            await self.catalog.delete(event)
            return "Игра и таблица удалены"

        assert command.participant_key is not None
        await self.catalog.ensure_migrated(event)
        if command.operation is Operation.ENLIST:
            if event.status is EventStatus.CLOSED:
                raise OperationNotAllowed("Регистрация на эту игру закрыта")
            assert isinstance(command.payload, EnlistPayload)
            larp_experience = command.payload.larp_experience
            crossplay = command.payload.crossplay
            if (larp_experience is None or crossplay is None) and self.users is not None:
                user = await self.users.get(command.platform, command.platform_user_id)
                if user is not None:
                    larp_experience = user.larp_experience
                    crossplay = user.crossplay
            await self.catalog.registrations.enlist(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                display_name=command.payload.display_name,
                wish_play=command.payload.wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
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
