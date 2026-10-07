from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import date, datetime
from typing import Protocol

from larp_bot.domain.models import (
    DEFAULT_PLAYER_AMOUNT,
    AttendanceStatus,
    BotIdentity,
    Button,
    Event,
    EventStatus,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    PrivilegedRole,
    Registration,
    ScheduledLifecycleEvent,
    TelegramUser,
    User,
    VkUser,
)


class UserRepository(Protocol):
    async def get(self, platform: Platform, user_id: int) -> User | None: ...

    async def find_linked(self, user: User) -> User | None: ...

    async def find_telegram_by_handle(self, handle: str) -> TelegramUser | None: ...

    async def update_telegram_handle(self, user_id: int, handle: str) -> None: ...

    async def update_telegram_link(
        self,
        user_id: int,
        handle: str | None,
        linked_vk_id: int | None,
    ) -> None: ...

    async def save(self, user: User) -> None: ...

    async def set_larp_experience(self, platform: Platform, user_id: int, experienced: bool) -> None: ...

    async def grant_larp_experience(self, platform: Platform, user_id: int) -> None: ...

    async def set_privileged_role(
        self,
        platform: Platform,
        user_id: int,
        role: PrivilegedRole,
        operation_id: str,
    ) -> None: ...

    async def clear_privileged_role(self, platform: Platform, user_id: int, operation_id: str) -> None: ...

    async def list_all(self) -> Sequence[User]: ...

    async def claim_delivery(
        self,
        platform: Platform,
        user_id: int,
        operation_id: str,
        buttons: Sequence[Button] = (),
    ) -> bool: ...


class EventRepository(Protocol):
    async def get(self, event_id: str) -> Event | None: ...

    async def get_by_signup_token(self, signup_token: str) -> Event | None: ...

    async def get_by_telegram_chat_id(self, chat_id: int) -> Event | None: ...

    async def create(self, event: Event, leaders: Sequence[BotIdentity] = ()) -> None: ...

    async def add_leader(self, event_id: str, leader: BotIdentity) -> bool: ...

    async def remove_leader(self, event_id: str, leader: BotIdentity) -> bool: ...

    async def list_leaders(self, event_id: str) -> Sequence[BotIdentity]: ...

    async def set_status(self, event_id: str, status: EventStatus) -> bool: ...

    async def set_registration_open_at(self, event_id: str, opens_at: datetime | None) -> bool: ...

    async def list_due_lifecycle_events(
        self,
        due_by: datetime,
        *,
        limit: int = 300,
    ) -> Sequence[ScheduledLifecycleEvent]: ...

    async def list_due_registration_notifications(
        self,
        opened_by: datetime,
        *,
        limit: int = 100,
    ) -> Sequence[Event]: ...

    async def mark_registration_open_notification_delivered(
        self,
        event_id: str,
        recipient: str,
    ) -> None: ...

    async def complete_registration_open_notification(
        self,
        event_id: str,
        completed_at: datetime,
    ) -> None: ...

    async def set_confirmation_schedule(
        self,
        event_id: str,
        opens_at: datetime | None,
        deadline: datetime | None,
    ) -> bool: ...

    async def list_due_confirmation_openings(
        self,
        opened_by: datetime,
        *,
        limit: int = 100,
    ) -> Sequence[Event]: ...

    async def open_scheduled_confirmation(
        self,
        event_id: str,
        opens_at: datetime,
        deadline: datetime,
    ) -> bool: ...

    async def set_scheduled_close_at(self, event_id: str, closes_at: datetime | None) -> bool: ...

    async def list_due_scheduled_closings(
        self,
        closed_by: datetime,
        *,
        limit: int = 100,
    ) -> Sequence[Event]: ...

    async def start_scheduled_close(
        self,
        event_id: str,
        operation_id: str,
        started_at: datetime,
    ) -> bool: ...

    async def complete_scheduled_close_actions(
        self,
        event_id: str,
        operation_id: str,
        completed_at: datetime,
    ) -> bool: ...

    async def mark_scheduled_close_notification_delivered(
        self,
        event_id: str,
        operation_id: str,
        recipient: str,
    ) -> None: ...

    async def complete_scheduled_close_notifications(
        self,
        event_id: str,
        operation_id: str,
        completed_at: datetime,
    ) -> None: ...

    async def set_player_amount(self, event_id: str, player_amount: int) -> bool: ...

    async def set_details(self, event_id: str, name: str, game_date: date) -> bool: ...

    async def set_post_links(self, event_id: str, telegram_post_url: str, vk_post_url: str) -> bool: ...

    async def bind_telegram_chat(self, event_id: str, chat_id: int) -> bool: ...

    async def set_telegram_chat_invite_link(self, event_id: str, chat_id: int, invite_link: str) -> bool: ...

    async def archive(self, event_id: str, archived_at: datetime) -> bool: ...

    async def unarchive(self, event_id: str, updated_at: datetime) -> bool: ...

    async def open_confirmation(self, event_id: str, deadline: datetime) -> bool: ...

    async def mark_registrations_migrated(self, event_id: str, migrated_at: datetime) -> None: ...

    async def set_public_table(self, event_id: str, resource_path: str, public_url: str) -> bool: ...

    async def set_pass_table(self, event_id: str, resource_path: str, public_url: str) -> bool: ...

    async def set_achievement(self, event_id: str, resource_path: str, public_url: str) -> bool: ...

    async def append_confirmed_notification(self, event_id: str, text: str, operation_id: str) -> bool: ...

    async def set_reserve_promotion(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str | None,
    ) -> None: ...

    async def mark_reserve_promotion_delivered(self, event_id: str, operation_id: str) -> None: ...

    async def set_confirmed_link_promotion(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str | None,
    ) -> None: ...

    async def mark_confirmed_link_promotion_delivered(self, event_id: str, operation_id: str) -> None: ...

    async def set_participant_removal(
        self,
        event_id: str,
        operation_id: str,
        participant_keys: Sequence[str],
    ) -> None: ...

    async def add_participant_removal_keys(
        self,
        event_id: str,
        operation_id: str,
        participant_keys: Sequence[str],
    ) -> None: ...

    async def mark_participant_removal_delivered(
        self,
        event_id: str,
        operation_id: str,
        participant_key: str,
    ) -> None: ...

    async def delete(self, event_id: str) -> bool: ...

    async def list_page(
        self,
        *,
        statuses: Collection[EventStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        archived: bool | None = None,
        registration_opened: bool | None = None,
        newest_first: bool = False,
        limit: int = 10,
    ) -> Sequence[Event]: ...


class RegistrationRepository(Protocol):
    async def get(self, event_id: str, participant_key: str) -> Registration | None: ...

    async def find_by_telegram_profile(self, profile: str) -> Sequence[Registration]: ...

    async def list_for_event(self, event_id: str) -> Sequence[Registration]: ...

    async def list_page_for_event(
        self,
        event_id: str,
        *,
        statuses: Collection[AttendanceStatus] | None = None,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 10,
    ) -> Sequence[Registration]: ...

    async def import_missing(self, registrations: Sequence[Registration]) -> None: ...

    async def enlist(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        nickname: str | None = None,
        wish_play: str,
        larp_experience: bool | None = None,
        crossplay: bool | None = None,
        vk_profile: str = "",
        telegram_profile: str | None = None,
    ) -> bool: ...

    async def update_profile(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        display_name: str,
        nickname: str | None,
        larp_experience: bool,
        crossplay: bool,
        vk_profile: str,
        telegram_profile: str | None,
    ) -> bool: ...

    async def confirm(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
        replaced_event_id: str | None = None,
        replaced_participant_key: str | None = None,
    ) -> bool: ...

    async def confirm_linked(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
        character_wish: str,
        replaced_event_id: str | None = None,
        replaced_participant_keys: Sequence[str] = (),
    ) -> bool: ...

    async def update_character_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        character_wish: str,
    ) -> bool: ...

    async def update_linked_character_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
        character_wish: str,
    ) -> bool: ...

    async def update_co_player_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_key: str,
        wish_play: str,
    ) -> bool: ...

    async def update_linked_co_player_wish(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
        wish_play: str,
    ) -> bool: ...

    async def cancel(self, event_id: str, *, operation_id: str, participant_key: str) -> bool: ...

    async def cancel_linked(
        self,
        event_id: str,
        *,
        operation_id: str,
        participant_keys: Sequence[str],
    ) -> bool: ...

    async def sift_waiting_to_end(self, event_id: str) -> None: ...

    async def swap_queue_positions(
        self,
        event_id: str,
        *,
        operation_id: str,
        promoted_participant_key: str,
        promoted_created_at: datetime,
        demoted_participant_key: str,
        demoted_created_at: datetime,
    ) -> bool: ...

    async def remove(self, event_id: str, *, participant_key: str) -> bool: ...

    async def remove_many(self, event_id: str, *, participant_keys: Sequence[str]) -> None: ...

    async def delete_for_event(self, event_id: str) -> None: ...


class RegistrationShowcaseRepository(Protocol):
    deferred_uploads: bool

    async def read_legacy_registrations(self, event: Event) -> Sequence[Registration]: ...

    async def replace(self, event: Event, registrations: Sequence[Registration]) -> None: ...

    async def create_event_workbook(
        self,
        disk_path: str,
        registrations: Sequence[Registration] = (),
        *,
        player_amount: int = DEFAULT_PLAYER_AMOUNT,
        callback: dict[str, object] | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None: ...

    async def create_public_event_workbook(
        self,
        disk_path: str,
        registrations: Sequence[Registration] = (),
        *,
        player_amount: int = DEFAULT_PLAYER_AMOUNT,
        callback: dict[str, object] | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None: ...

    async def create_event_bundle(
        self,
        master_path: str,
        public_path: str,
        pass_path: str,
        pass_profiles: Sequence[PassDetails],
        *,
        player_amount: int,
        callback: dict[str, object],
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, str] | None: ...

    async def delete_event_workbook(self, disk_path: str) -> None: ...

    async def create_pass_table(
        self,
        disk_path: str,
        profiles: Sequence[PassDetails],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None: ...

    async def upsert_published_pass_table(
        self,
        disk_path: str,
        profiles: Sequence[PassDetails],
        *,
        callback: dict[str, object],
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None: ...

    async def pass_table_exists(self, disk_path: str) -> bool: ...

    async def replace_pass_table(self, disk_path: str, profiles: Sequence[PassDetails]) -> None: ...

    async def delete_pass_table(self, disk_path: str) -> None: ...

    async def create_achievement_presentation(
        self,
        disk_path: str,
        game_name: str,
        *,
        overwrite: bool = False,
        callback: dict[str, object] | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None: ...

    async def delete_achievement_presentation(self, disk_path: str) -> None: ...


class OrderedCommandPublisher(Protocol):
    async def publish(self, command: OrderedRegistrationCommand) -> None: ...


class OrderedCommandConsumer(Protocol):
    async def receive(self, *, max_messages: int, wait_seconds: int) -> Sequence[object]: ...

    async def delete(self, receipt_handle: str) -> None: ...

    async def release(self, receipt_handle: str) -> None: ...


class AdminConfigProvider(Protocol):
    async def list_admins(self) -> Sequence[BotIdentity]: ...

    async def list_gamemasters(self) -> Sequence[BotIdentity]: ...

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


class TelegramChatGateway(Protocol):
    async def send_message(
        self,
        *,
        chat_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> int: ...

    async def edit_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None: ...

    async def delete_message(self, *, chat_id: int, message_id: int, request_id: str) -> None: ...

    async def answer_callback(self, *, callback_query_id: str, request_id: str, text: str = "") -> None: ...

    async def get_chat_member(self, *, chat_id: int, user_id: int, request_id: str) -> dict[str, object] | None: ...

    async def ensure_primary_invite_link(self, *, chat_id: int, request_id: str) -> str: ...

    async def promote_title_only_admin(self, *, chat_id: int, user_id: int, request_id: str) -> None: ...

    async def set_administrator_title(
        self,
        *,
        chat_id: int,
        user_id: int,
        title: str,
        request_id: str,
    ) -> None: ...

    async def kick_member(self, *, chat_id: int, user_id: int, request_id: str) -> bool: ...


class VkUserIdResolver(Protocol):
    async def resolve_user_id(self, profile: str) -> int | None: ...


def new_user(platform: Platform, user_id: int) -> TelegramUser | VkUser:
    if platform is Platform.TELEGRAM:
        return TelegramUser(tg_id=user_id)
    if platform is Platform.VK:
        return VkUser(vk_id=user_id)
    raise ValueError("system identities do not have user profiles")
