from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from larp_bot.domain.models import (
    AttendanceStatus,
    CharacterWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    Registration,
    ReplyContext,
)
from larp_bot.domain.security import participant_key

from .ports import EventRepository, OrderedCommandPublisher, RegistrationTableRepository, UserRepository

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


class RegistrationService:
    def __init__(
        self,
        events: EventRepository,
        tables: RegistrationTableRepository,
        publisher: OrderedCommandPublisher,
        participant_secret: str,
    ) -> None:
        self.events = events
        self.tables = tables
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
        return await self.tables.find_registration(event, self.key(platform, user_id, event_id))

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
                found = await self.tables.find_registration(event, self.key(platform, user_id, event.event_id))
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
        payload: EnlistPayload | CharacterWishPayload | EmptyPayload,
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
    def __init__(self, events: EventRepository, tables: RegistrationTableRepository) -> None:
        self.events = events
        self.tables = tables

    async def create_event(self, name: str) -> Event:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Название игры не может быть пустым")
        event_id = str(uuid4())
        disk_path = f"disk:/larp-bot/events/{event_id}-{event_slug(clean_name)}.xlsx"
        public_url = await self.tables.create_event_workbook(disk_path)
        event = Event(
            event_id=event_id,
            name=clean_name,
            disk_resource_path=disk_path,
            public_registration_url=public_url,
        )
        try:
            await self.events.create(event)
        except Exception:
            await self.tables.delete_event_workbook(disk_path)
            raise
        return event


class OrderedMutationService:
    """Authoritative worker-time validation and workbook mutation."""

    def __init__(
        self,
        events: EventRepository,
        tables: RegistrationTableRepository,
        users: UserRepository | None = None,
    ) -> None:
        self.events = events
        self.tables = tables
        self.users = users

    async def apply(self, command: OrderedRegistrationCommand) -> str:
        event = await self.events.get(command.event_id)
        if event is None:
            if command.operation is Operation.DELETE_EVENT:
                return "Игра и таблица уже удалены"
            raise EventNotFound("Игра уже удалена или не существует")

        status_operations = {
            Operation.OPEN_REGISTRATION: (EventStatus.CREATED, "Установлен Статус «Регистрация»"),
            Operation.OPEN_CONFIRMATION: (EventStatus.CONFIRMATION_OPEN, "Установлен Статус «Подтверждение»"),
            Operation.CLOSE_EVENT: (EventStatus.CLOSED, "Установлен Статус «Закрытие регистрации»"),
        }
        if command.operation in status_operations:
            status, message = status_operations[command.operation]
            await self.events.set_status(event.event_id, status)
            return message
        if command.operation is Operation.DELETE_EVENT:
            await self.tables.delete_event_workbook(event.disk_resource_path)
            await self.events.delete(event.event_id)
            return "Игра и таблица удалены"

        assert command.participant_key is not None
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
            await self.tables.enlist(
                event,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                display_name=command.payload.display_name,
                wish_play=command.payload.wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
            )
            return "Заявка на игру записана"

        if command.operation is Operation.CONFIRM and event.status is not EventStatus.CONFIRMATION_OPEN:
            if event.status is EventStatus.CREATED:
                raise OperationNotAllowed("Подтверждение участия в этой игре ещё не открыто")
            raise OperationNotAllowed("Подтверждение участия в этой игре закрыто")

        registration = await self.tables.find_registration(event, command.participant_key)
        if registration is None:
            raise RegistrationNotFound("Сначала запишитесь на эту игру")
        if command.operation is Operation.CONFIRM:
            assert isinstance(command.payload, CharacterWishPayload)
            await self.tables.confirm(
                event,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                character_wish=command.payload.character_wish,
            )
            return "Участие подтверждено"
        if command.operation is Operation.UPDATE_CHARACTER_WISH:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова подтвердите участие")
            if registration.attendance_status is AttendanceStatus.WAITING and not registration.character_wish:
                raise OperationNotAllowed("Впервые укажите пожелания при подтверждении участия")
            assert isinstance(command.payload, CharacterWishPayload)
            await self.tables.update_character_wish(
                event,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                character_wish=command.payload.character_wish,
            )
            return "Пожелания по персонажу обновлены"
        if command.operation is Operation.CANCEL:
            await self.tables.cancel(
                event,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
            )
            return "Участие отменено"
        raise AssertionError(f"unhandled operation: {command.operation}")
