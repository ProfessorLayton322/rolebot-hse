from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from larp_bot.domain.models import BotIdentity, Event, EventStatus, Platform, ScheduledLifecycleEvent, event_reference

from .navigation import main_menu_button
from .ports import AdminConfigProvider, DeferredTransport, EventRepository

LOGGER = logging.getLogger("larp_bot.application.registration_openings")


@dataclass(frozen=True)
class RegistrationOpeningNotificationResult:
    events_completed: int
    notifications_delivered: int


class RegistrationOpeningNotificationService:
    """Notify privileged operators after scheduled registration is already open."""

    def __init__(
        self,
        events: EventRepository,
        admins: AdminConfigProvider,
        transport: DeferredTransport,
    ) -> None:
        self.events = events
        self.admins = admins
        self.transport = transport

    @staticmethod
    def _recipient_key(identity: BotIdentity) -> str:
        return f"{identity.platform.value}:{identity.platform_user_id}"

    @staticmethod
    def _request_id(event: Event | ScheduledLifecycleEvent, identity: BotIdentity) -> str:
        return f"registration-open:{event.event_id}:{identity.platform.value}:{identity.platform_user_id}"

    async def _send(self, event: Event | ScheduledLifecycleEvent, identity: BotIdentity) -> str | None:
        recipient = self._recipient_key(identity)
        try:
            await self.transport.send(
                platform=identity.platform,
                user_id=identity.platform_user_id,
                request_id=self._request_id(event, identity),
                text=f"Регистрация на игру {event_reference(event.name, event.game_date)} открыта.",
                buttons=[main_menu_button()],
            )
        except Exception:
            LOGGER.exception(
                "registration_open_notification_failed",
                extra={
                    "event_id": event.event_id,
                    "platform": identity.platform.value,
                    "platform_user_id": identity.platform_user_id,
                },
            )
            return None
        return recipient

    async def notify_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        due_events: Sequence[ScheduledLifecycleEvent] | None = None,
    ) -> RegistrationOpeningNotificationResult:
        opened_by = datetime.now(UTC) if now is None else now.astimezone(UTC)
        if due_events is None:
            due: Sequence[Event | ScheduledLifecycleEvent] = await self.events.list_due_registration_notifications(
                opened_by,
                limit=limit,
            )
        else:
            due = [
                event
                for event in due_events
                if event.registration_open_at is not None
                and event.registration_open_at <= opened_by
                and event.registration_open_notification_completed_at is None
                and event.archived_at is None
                and event.status is not EventStatus.CLOSED
            ][:limit]
        if not due:
            return RegistrationOpeningNotificationResult(events_completed=0, notifications_delivered=0)

        admin_identities, moderator_identities = await asyncio.gather(
            self.admins.list_admins(),
            self.admins.list_gamemasters(),
        )
        identities = {
            self._recipient_key(identity): identity
            for identity in (*admin_identities, *moderator_identities)
            if identity.platform is not Platform.SYSTEM
        }
        ordered_identities = [identities[key] for key in sorted(identities)]

        delivered = 0
        completed = 0
        for event in due:
            notified = set(event.registration_open_notified_recipients)
            pending = [identity for identity in ordered_identities if self._recipient_key(identity) not in notified]
            results = await asyncio.gather(*(self._send(event, identity) for identity in pending))
            for recipient in results:
                if recipient is None:
                    continue
                await self.events.mark_registration_open_notification_delivered(event.event_id, recipient)
                notified.add(recipient)
                delivered += 1
            if all(self._recipient_key(identity) in notified for identity in ordered_identities):
                await self.events.complete_registration_open_notification(event.event_id, opened_by)
                completed += 1

        return RegistrationOpeningNotificationResult(
            events_completed=completed,
            notifications_delivered=delivered,
        )
