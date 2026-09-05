from __future__ import annotations

from collections.abc import Collection, Sequence
from copy import deepcopy
from datetime import UTC, datetime

from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    Button,
    Event,
    EventStatus,
    OrderedRegistrationCommand,
    Platform,
    Registration,
    TelegramUser,
    User,
)


class MemoryUserRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[Platform, int], User] = {}

    async def get(self, platform: Platform, user_id: int) -> User | None:
        value = self.rows.get((platform, user_id))
        return None if value is None else deepcopy(value)

    async def find_telegram_by_handle(self, handle: str) -> TelegramUser | None:
        matches = [
            user
            for (platform, _), user in self.rows.items()
            if platform is Platform.TELEGRAM and getattr(user, "telegram_handle", None) == handle
        ]
        if len(matches) != 1:
            return None
        match = matches[0]
        assert isinstance(match, TelegramUser)
        return deepcopy(match)

    async def save(self, user: User) -> None:
        platform = Platform.TELEGRAM if hasattr(user, "tg_id") else Platform.VK
        user_id = user.tg_id if hasattr(user, "tg_id") else user.vk_id
        self.rows[(platform, user_id)] = deepcopy(user)

    async def grant_gamemaster(self, platform: Platform, user_id: int, operation_id: str) -> None:
        user = self.rows[(platform, user_id)]
        if not user.is_gamemaster:
            user.is_gamemaster = True
            user.gamemaster_grant_operation_id = operation_id

    async def list_all(self) -> Sequence[User]:
        return deepcopy(list(self.rows.values()))

    async def claim_delivery(
        self,
        platform: Platform,
        user_id: int,
        operation_id: str,
        buttons: Sequence[Button] = (),
    ) -> bool:
        user = self.rows.get((platform, user_id))
        if user is None:
            return True
        if user.last_delivery_operation_id == operation_id:
            return False
        user.last_delivery_operation_id = operation_id
        user.last_bot_buttons = list(buttons)
        return True


class MemoryEventRepository:
    def __init__(self, events: Sequence[Event] = ()) -> None:
        self.rows = {event.event_id: deepcopy(event) for event in events}
        self.leaders: dict[str, set[tuple[Platform, int]]] = {event.event_id: set() for event in events}

    async def get(self, event_id: str) -> Event | None:
        event = self.rows.get(event_id)
        return None if event is None else deepcopy(event)

    async def create(self, event: Event, leaders: Sequence[BotIdentity] = ()) -> None:
        if event.event_id in self.rows:
            raise ValueError("event exists")
        self.rows[event.event_id] = deepcopy(event)
        self.leaders[event.event_id] = {(leader.platform, leader.platform_user_id) for leader in leaders}

    async def add_leader(self, event_id: str, leader: BotIdentity) -> bool:
        if event_id not in self.rows:
            return False
        identity = (leader.platform, leader.platform_user_id)
        event_leaders = self.leaders.setdefault(event_id, set())
        if identity in event_leaders:
            return False
        event_leaders.add(identity)
        return True

    async def list_leaders(self, event_id: str) -> Sequence[BotIdentity]:
        return [
            BotIdentity(platform=platform, platform_user_id=user_id)
            for platform, user_id in sorted(
                self.leaders.get(event_id, set()), key=lambda item: (item[0].value, item[1])
            )
        ]

    async def set_status(self, event_id: str, status: EventStatus) -> bool:
        event = self.rows.get(event_id)
        if event is None:
            return False
        changed = event.status is not status
        event.status = status
        event.updated_at = datetime.now(event.updated_at.tzinfo)
        return changed

    async def open_confirmation(self, event_id: str, deadline: datetime) -> bool:
        event = self.rows.get(event_id)
        if event is None:
            return False
        changed = event.status is not EventStatus.CONFIRMATION_OPEN or event.confirmation_deadline != deadline
        event.status = EventStatus.CONFIRMATION_OPEN
        event.confirmation_deadline = deadline
        event.updated_at = datetime.now(event.updated_at.tzinfo)
        return changed

    async def mark_registrations_migrated(self, event_id: str, migrated_at: datetime) -> None:
        event = self.rows[event_id]
        event.registrations_migrated_at = migrated_at
        event.updated_at = migrated_at

    async def set_public_table(self, event_id: str, resource_path: str, public_url: str) -> bool:
        event = self.rows.get(event_id)
        if event is None or event.public_table_public_url is not None:
            return False
        self.rows[event_id] = Event.model_validate(
            event.model_dump()
            | {
                "public_table_resource_path": resource_path,
                "public_table_public_url": public_url,
                "updated_at": datetime.now(event.updated_at.tzinfo),
            }
        )
        return True

    async def archive(self, event_id: str, archived_at: datetime) -> bool:
        event = self.rows.get(event_id)
        if event is None or event.archived_at is not None:
            return False
        self.rows[event_id] = Event.model_validate(
            event.model_dump()
            | {
                "status": EventStatus.CLOSED,
                "archived_at": archived_at,
                "updated_at": archived_at,
            }
        )
        return True

    async def set_pass_table(self, event_id: str, resource_path: str, public_url: str) -> bool:
        event = self.rows.get(event_id)
        if event is None or event.pass_table_public_url is not None:
            return False
        self.rows[event_id] = Event.model_validate(
            event.model_dump()
            | {
                "pass_table_resource_path": resource_path,
                "pass_table_public_url": public_url,
                "updated_at": datetime.now(event.updated_at.tzinfo),
            }
        )
        return True

    async def append_confirmed_notification(self, event_id: str, text: str, operation_id: str) -> bool:
        event = self.rows.get(event_id)
        if event is None:
            return False
        if event.last_confirmed_notification_operation_id == operation_id:
            return False
        event.confirmed_notifications.append(text)
        event.last_confirmed_notification_operation_id = operation_id
        event.updated_at = datetime.now(event.updated_at.tzinfo)
        return True

    async def set_reserve_promotion(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str | None,
    ) -> None:
        event = self.rows[event_id]
        event.last_reserve_promotion_operation_id = operation_id
        event.last_reserve_promotion_participant_key = participant_key
        event.updated_at = datetime.now(event.updated_at.tzinfo)

    async def mark_reserve_promotion_delivered(self, event_id: str, operation_id: str) -> None:
        event = self.rows[event_id]
        event.last_reserve_promotion_delivered_operation_id = operation_id
        event.updated_at = datetime.now(event.updated_at.tzinfo)

    async def delete(self, event_id: str) -> bool:
        self.leaders.pop(event_id, None)
        return self.rows.pop(event_id, None) is not None

    async def list_page(
        self,
        *,
        statuses: Collection[EventStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        archived: bool | None = None,
        limit: int = 10,
    ) -> Sequence[Event]:
        if after is not None and before is not None:
            raise ValueError("only one event-page cursor may be supplied")
        values = sorted(self.rows.values(), key=lambda event: (event.created_at, event.event_id))
        if statuses is not None:
            accepted = set(statuses)
            values = [event for event in values if event.status in accepted]
        if archived is not None:
            values = [event for event in values if (event.archived_at is not None) is archived]
        if after is not None:
            values = [event for event in values if (event.created_at, event.event_id) > after]
        if before is not None:
            values = [event for event in values if (event.created_at, event.event_id) < before]
            values = values[-limit:]
        return deepcopy(values[:limit])


class MemoryRegistrationRepository:
    def __init__(self, registrations: Sequence[Registration] = ()) -> None:
        self.rows = {
            (registration.event_id, registration.participant_key): deepcopy(registration)
            for registration in registrations
        }

    async def get(self, event_id: str, participant_key: str) -> Registration | None:
        value = self.rows.get((event_id, participant_key))
        return None if value is None else deepcopy(value)

    async def list_for_event(self, event_id: str) -> Sequence[Registration]:
        values = [value for (stored_event_id, _), value in self.rows.items() if stored_event_id == event_id]
        return deepcopy(sorted(values, key=lambda item: (item.created_at, item.participant_key)))

    async def list_page_for_event(
        self,
        event_id: str,
        *,
        statuses: Collection[AttendanceStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Registration]:
        if after is not None and before is not None:
            raise ValueError("only one registration-page cursor may be supplied")
        values = [value for (stored_event_id, _), value in self.rows.items() if stored_event_id == event_id]
        if statuses is not None:
            accepted = set(statuses)
            values = [registration for registration in values if registration.attendance_status in accepted]
        values.sort(key=lambda registration: (registration.created_at, registration.participant_key))
        if after is not None:
            values = [registration for registration in values if self._registration_cursor(registration) > after]
        if before is not None:
            values = [registration for registration in values if self._registration_cursor(registration) < before]
            values = values[-limit:]
        return deepcopy(values[:limit])

    @staticmethod
    def _registration_cursor(registration: Registration) -> tuple[datetime, str]:
        return registration.created_at, registration.participant_key

    async def import_missing(self, registrations: Sequence[Registration]) -> None:
        for registration in registrations:
            self.rows.setdefault(
                (registration.event_id, registration.participant_key),
                deepcopy(registration),
            )

    async def enlist(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        wish_play: str,
        larp_experience: bool | None = None,
        crossplay: bool | None = None,
        vk_profile: str = "",
        telegram_profile: str | None = None,
    ) -> bool:
        key = (event_id, participant_key)
        registration = self.rows.get(key)
        if registration is not None and registration.last_operation_id == operation_id:
            return False
        if registration is None:
            registration = Registration(
                event_id=event_id,
                participant_key=participant_key,
                display_name=display_name,
                wish_play=wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            )
            self.rows[key] = registration
        else:
            registration.display_name = display_name
            registration.wish_play = wish_play
            registration.larp_experience = larp_experience
            registration.crossplay = crossplay
            registration.vk_profile = vk_profile
            registration.telegram_profile = telegram_profile
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                registration.attendance_status = AttendanceStatus.WAITING
                registration.created_at = datetime.now(UTC)
        registration.last_operation_id = operation_id
        registration.updated_at = datetime.now(UTC)
        return True

    async def confirm(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool:
        registration = self.rows[(event_id, participant_key)]
        if registration.last_operation_id == operation_id:
            return False
        registration.character_wish = character_wish
        registration.attendance_status = AttendanceStatus.CONFIRMED
        registration.last_operation_id = operation_id
        registration.updated_at = datetime.now(registration.updated_at.tzinfo)
        return True

    async def update_character_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool:
        registration = self.rows[(event_id, participant_key)]
        if registration.last_operation_id == operation_id:
            return False
        registration.character_wish = character_wish
        registration.last_operation_id = operation_id
        registration.updated_at = datetime.now(registration.updated_at.tzinfo)
        return True

    async def cancel(self, event_id: str, *, operation_id: str, participant_key: str) -> bool:
        registration = self.rows[(event_id, participant_key)]
        if registration.last_operation_id == operation_id:
            return False
        registration.attendance_status = AttendanceStatus.CANCELLED
        registration.last_operation_id = operation_id
        registration.updated_at = datetime.now(registration.updated_at.tzinfo)
        return True

    async def remove(self, event_id: str, *, participant_key: str) -> bool:
        return self.rows.pop((event_id, participant_key), None) is not None

    async def delete_for_event(self, event_id: str) -> None:
        for key in [key for key in self.rows if key[0] == event_id]:
            del self.rows[key]


class MemoryCommandPublisher:
    def __init__(self) -> None:
        self.commands: list[OrderedRegistrationCommand] = []

    async def publish(self, command: OrderedRegistrationCommand) -> None:
        if all(existing.operation_id != command.operation_id for existing in self.commands):
            self.commands.append(deepcopy(command))


class StaticAdminProvider:
    def __init__(
        self,
        tg_ids: set[int] | None = None,
        vk_ids: set[int] | None = None,
        tg_gamemaster_ids: set[int] | None = None,
        vk_gamemaster_ids: set[int] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.tg_ids = tg_ids or set()
        self.vk_ids = vk_ids or set()
        self.tg_gamemaster_ids = tg_gamemaster_ids or set()
        self.vk_gamemaster_ids = vk_gamemaster_ids or set()
        self.secrets = secrets or {}

    async def list_admins(self) -> Sequence[BotIdentity]:
        return [
            *(BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user_id) for user_id in sorted(self.tg_ids)),
            *(BotIdentity(platform=Platform.VK, platform_user_id=user_id) for user_id in sorted(self.vk_ids)),
        ]

    async def is_admin(self, platform: Platform, user_id: int) -> bool:
        return user_id in (self.tg_ids if platform is Platform.TELEGRAM else self.vk_ids)

    async def is_gamemaster(self, platform: Platform, user_id: int) -> bool:
        ids = self.tg_gamemaster_ids if platform is Platform.TELEGRAM else self.vk_gamemaster_ids
        return user_id in ids

    async def get_secret(self, key: str) -> str:
        return self.secrets[key]


class MemoryDeferredTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[Platform, int, str, str, tuple[Button, ...]]] = []

    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        self.sent.append((platform, user_id, request_id, text, tuple(buttons)))


class MemoryVkUserIdResolver:
    def __init__(self, ids: dict[str, int] | None = None) -> None:
        self.ids = ids or {}

    async def resolve_user_id(self, profile: str) -> int | None:
        return self.ids.get(profile)
