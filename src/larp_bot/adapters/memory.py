from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime

from larp_bot.domain.models import (
    Button,
    Event,
    EventStatus,
    OrderedRegistrationCommand,
    Platform,
    User,
)


class MemoryUserRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[Platform, int], User] = {}

    async def get(self, platform: Platform, user_id: int) -> User | None:
        value = self.rows.get((platform, user_id))
        return None if value is None else deepcopy(value)

    async def save(self, user: User) -> None:
        platform = Platform.TELEGRAM if hasattr(user, "tg_id") else Platform.VK
        user_id = user.tg_id if hasattr(user, "tg_id") else user.vk_id
        self.rows[(platform, user_id)] = deepcopy(user)

    async def claim_delivery(self, platform: Platform, user_id: int, operation_id: str) -> bool:
        user = self.rows.get((platform, user_id))
        if user is None:
            return True
        if user.last_delivery_operation_id == operation_id:
            return False
        user.last_delivery_operation_id = operation_id
        return True


class MemoryEventRepository:
    def __init__(self, events: Sequence[Event] = ()) -> None:
        self.rows = {event.event_id: deepcopy(event) for event in events}

    async def get(self, event_id: str) -> Event | None:
        event = self.rows.get(event_id)
        return None if event is None else deepcopy(event)

    async def create(self, event: Event) -> None:
        if event.event_id in self.rows:
            raise ValueError("event exists")
        self.rows[event.event_id] = deepcopy(event)

    async def set_status(self, event_id: str, status: EventStatus) -> bool:
        event = self.rows.get(event_id)
        if event is None:
            return False
        changed = event.status is not status
        event.status = status
        event.updated_at = datetime.now(event.updated_at.tzinfo)
        return changed

    async def delete(self, event_id: str) -> bool:
        return self.rows.pop(event_id, None) is not None

    async def list_page(
        self,
        *,
        status: EventStatus | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]:
        values = sorted(self.rows.values(), key=lambda event: (event.created_at, event.event_id))
        if status is not None:
            values = [event for event in values if event.status is status]
        if after is not None:
            values = [event for event in values if (event.created_at, event.event_id) > after]
        return deepcopy(values[:limit])


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
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.tg_ids = tg_ids or set()
        self.vk_ids = vk_ids or set()
        self.secrets = secrets or {}

    async def is_admin(self, platform: Platform, user_id: int) -> bool:
        return user_id in (self.tg_ids if platform is Platform.TELEGRAM else self.vk_ids)

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
