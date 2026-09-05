from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Protocol

from larp_bot.domain.models import (
    Button,
    Event,
    EventStatus,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    Registration,
    TelegramUser,
    User,
    VkUser,
)


class UserRepository(Protocol):
    async def get(self, platform: Platform, user_id: int) -> User | None: ...

    async def save(self, user: User) -> None: ...

    async def list_all(self) -> Sequence[User]: ...

    async def claim_delivery(self, platform: Platform, user_id: int, operation_id: str) -> bool: ...


class EventRepository(Protocol):
    async def get(self, event_id: str) -> Event | None: ...

    async def create(self, event: Event) -> None: ...

    async def set_status(self, event_id: str, status: EventStatus) -> bool: ...

    async def open_confirmation(self, event_id: str, deadline: datetime) -> bool: ...

    async def mark_registrations_migrated(self, event_id: str, migrated_at: datetime) -> None: ...

    async def set_public_table(self, event_id: str, resource_path: str, public_url: str) -> bool: ...

    async def set_pass_table(self, event_id: str, resource_path: str, public_url: str) -> bool: ...

    async def delete(self, event_id: str) -> bool: ...

    async def list_page(
        self,
        *,
        statuses: Collection[EventStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]: ...

    async def list_pass_tables_page(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Event]: ...


class RegistrationRepository(Protocol):
    async def get(self, event_id: str, participant_key: str) -> Registration | None: ...

    async def list_for_event(self, event_id: str) -> Sequence[Registration]: ...

    async def import_missing(self, registrations: Sequence[Registration]) -> None: ...

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
    ) -> bool: ...

    async def confirm(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool: ...

    async def update_character_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool: ...

    async def cancel(self, event_id: str, *, operation_id: str, participant_key: str) -> bool: ...

    async def delete_for_event(self, event_id: str) -> None: ...


class RegistrationShowcaseRepository(Protocol):
    async def read_legacy_registrations(self, event: Event) -> Sequence[Registration]: ...

    async def replace(self, event: Event, registrations: Sequence[Registration]) -> None: ...

    async def create_event_workbook(self, disk_path: str, registrations: Sequence[Registration] = ()) -> str: ...

    async def create_public_event_workbook(self, disk_path: str, registrations: Sequence[Registration] = ()) -> str: ...

    async def delete_event_workbook(self, disk_path: str) -> None: ...

    async def create_pass_table(self, disk_path: str, profiles: Sequence[PassDetails]) -> str: ...

    async def replace_pass_table(self, disk_path: str, profiles: Sequence[PassDetails]) -> None: ...

    async def delete_pass_table(self, disk_path: str) -> None: ...


class OrderedCommandPublisher(Protocol):
    async def publish(self, command: OrderedRegistrationCommand) -> None: ...


class OrderedCommandConsumer(Protocol):
    async def receive(self, *, max_messages: int, wait_seconds: int) -> Sequence[object]: ...

    async def delete(self, receipt_handle: str) -> None: ...


class AdminConfigProvider(Protocol):
    async def is_admin(self, platform: Platform, user_id: int) -> bool: ...

    async def is_gamemaster(self, platform: Platform, user_id: int) -> bool: ...

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
