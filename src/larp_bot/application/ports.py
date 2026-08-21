from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from larp_bot.domain.models import (
    Button,
    Event,
    EventStatus,
    OrderedRegistrationCommand,
    Platform,
    Registration,
    TelegramUser,
    User,
    VkUser,
)


class UserRepository(Protocol):
    async def get(self, platform: Platform, user_id: int) -> User | None: ...

    async def save(self, user: User) -> None: ...

    async def claim_delivery(self, platform: Platform, user_id: int, operation_id: str) -> bool: ...


class EventRepository(Protocol):
    async def get(self, event_id: str) -> Event | None: ...

    async def create(self, event: Event) -> None: ...

    async def set_status(self, event_id: str, status: EventStatus) -> bool: ...

    async def delete(self, event_id: str) -> bool: ...

    async def list_page(
        self,
        *,
        status: EventStatus | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]: ...


class RegistrationTableRepository(Protocol):
    async def find_registration(self, event: Event, participant_key: str) -> Registration | None: ...

    async def enlist(
        self,
        event: Event,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        wish_play: str,
        larp_experience: bool | None = None,
        crossplay: bool | None = None,
    ) -> bool: ...

    async def confirm(
        self,
        event: Event,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool: ...

    async def update_character_wish(
        self,
        event: Event,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool: ...

    async def cancel(self, event: Event, *, operation_id: str, participant_key: str) -> bool: ...

    async def create_event_workbook(self, disk_path: str) -> str: ...

    async def delete_event_workbook(self, disk_path: str) -> None: ...


class OrderedCommandPublisher(Protocol):
    async def publish(self, command: OrderedRegistrationCommand) -> None: ...


class OrderedCommandConsumer(Protocol):
    async def receive(self, *, max_messages: int, wait_seconds: int) -> Sequence[object]: ...

    async def delete(self, receipt_handle: str) -> None: ...


class AdminConfigProvider(Protocol):
    async def is_admin(self, platform: Platform, user_id: int) -> bool: ...

    async def get_secret(self, key: str) -> str: ...


class DeferredTransport(Protocol):
    async def send(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None: ...


def new_user(platform: Platform, user_id: int) -> TelegramUser | VkUser:
    if platform is Platform.TELEGRAM:
        return TelegramUser(tg_id=user_id)
    if platform is Platform.VK:
        return VkUser(vk_id=user_id)
    raise ValueError("system identities do not have user profiles")
