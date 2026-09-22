from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from larp_bot.adapters.telegram import TelegramGroupUpdate
from larp_bot.domain.models import (
    MAX_EVENT_PAGE_SIZE,
    AttendanceStatus,
    Button,
    Event,
    event_name_with_date,
)

from .ports import EventRepository, TelegramChatGateway
from .services import RegistrationService

LOGGER = logging.getLogger("larp_bot.application.chat_management")

CHAT_PAGE_SIZE = 5
_TIE_TEXT = "Выберите игру, к которой нужно привязать этот чат:"
_EXPLICIT_ADMIN_RIGHTS = frozenset(
    {
        "can_change_info",
        "can_delete_messages",
        "can_manage_video_chats",
        "can_restrict_members",
        "can_promote_members",
        "can_invite_users",
        "can_post_stories",
        "can_edit_stories",
        "can_delete_stories",
        "can_post_messages",
        "can_edit_messages",
        "can_pin_messages",
        "can_manage_topics",
        "can_manage_direct_messages",
        "can_manage_tags",
        "can_send_welcome_messages",
    }
)


def telegram_administrator_title(display_name: str, *, limit: int = 16) -> str:
    """Fit ``surname name`` into Telegram's administrator-title limit."""

    parts = display_name.split()
    surname = parts[0] if parts else "Игрок"
    if len(parts) > 1:
        full = f"{surname} {parts[1]}"
        if len(full) <= limit:
            return full
        abbreviated = f"{surname} {parts[1][0]}."
        if len(abbreviated) <= limit:
            return abbreviated
    if len(surname) <= limit:
        return surname
    return surname[:limit]


def _event_token(event_id: str) -> str:
    return hashlib.blake2s(event_id.encode(), digest_size=12).hexdigest()


def _member_has_explicit_admin_right(member: dict[str, object] | None) -> bool:
    if member is None:
        return False
    if member.get("status") == "creator":
        return True
    if member.get("status") != "administrator":
        return False
    # can_manage_chat is implicit for title-only administrators. Requiring an
    # explicit privilege keeps player identity titles from authorizing /tie.
    return any(member.get(right) is True for right in _EXPLICIT_ADMIN_RIGHTS)


class TelegramChatManagementService:
    def __init__(
        self,
        events: EventRepository,
        registrations: RegistrationService,
        telegram: TelegramChatGateway,
    ) -> None:
        self.events = events
        self.registrations = registrations
        self.telegram = telegram

    async def _unarchived_events(self) -> list[Event]:
        events: list[Event] = []
        after: tuple[datetime, str] | None = None
        while True:
            page = list(
                await self.events.list_page(
                    after=after,
                    archived=False,
                    limit=MAX_EVENT_PAGE_SIZE,
                )
            )
            events.extend(page)
            if len(page) < MAX_EVENT_PAGE_SIZE:
                return events
            last = page[-1]
            after = (last.created_at, last.event_id)

    @staticmethod
    def _page_buttons(events: list[Event], page_number: int) -> list[Button]:
        offset = page_number * CHAT_PAGE_SIZE
        page = events[offset : offset + CHAT_PAGE_SIZE]
        buttons = [
            Button(
                label=event_name_with_date(event.name, event.game_date),
                value=f"tie:e:{_event_token(event.event_id)}",
            )
            for event in page
        ]
        if page_number > 0:
            buttons.append(Button(label="Предыдущая страница", value=f"tie:p:{page_number - 1}"))
        if offset + CHAT_PAGE_SIZE < len(events):
            buttons.append(Button(label="Вперёд", value=f"tie:p:{page_number + 1}"))
        return buttons

    async def _show_picker(
        self,
        update: TelegramGroupUpdate,
        *,
        page_number: int = 0,
        edit: bool = False,
    ) -> None:
        events = await self._unarchived_events()
        if not events:
            text = "Нет неархивированных игр, к которым можно привязать чат."
            buttons: list[Button] = []
        else:
            last_page = (len(events) - 1) // CHAT_PAGE_SIZE
            page_number = min(max(page_number, 0), last_page)
            text = _TIE_TEXT
            buttons = self._page_buttons(events, page_number)
        request_id = f"telegram-chat:{update.update_id}:picker:{page_number}"
        if edit and update.message_id is not None:
            await self.telegram.edit_message(
                chat_id=update.chat_id,
                message_id=update.message_id,
                request_id=request_id,
                text=text,
                buttons=buttons,
            )
            return
        await self.telegram.send_message(
            chat_id=update.chat_id,
            request_id=request_id,
            text=text,
            buttons=buttons,
        )

    async def _actor_can_tie(self, update: TelegramGroupUpdate) -> bool:
        if update.actor_user_id is None:
            return False
        member = await self.telegram.get_chat_member(
            chat_id=update.chat_id,
            user_id=update.actor_user_id,
            request_id=f"telegram-chat:{update.update_id}:actor",
        )
        return _member_has_explicit_admin_right(member)

    async def _event_for_token(self, token: str) -> Event | None:
        matches = [event for event in await self._unarchived_events() if _event_token(event.event_id) == token]
        if len(matches) > 1:
            raise RuntimeError("Telegram tie callback token collision")
        return matches[0] if matches else None

    async def _tie(self, update: TelegramGroupUpdate, token: str) -> None:
        event = await self._event_for_token(token)
        if event is None:
            await self.telegram.answer_callback(
                callback_query_id=update.callback_query_id or "",
                request_id=f"telegram-chat:{update.update_id}:missing",
                text="Игра больше недоступна.",
            )
            await self._show_picker(update, edit=True)
            return
        if not await self.events.bind_telegram_chat(event.event_id, update.chat_id):
            await self.telegram.answer_callback(
                callback_query_id=update.callback_query_id or "",
                request_id=f"telegram-chat:{update.update_id}:conflict",
                text="Этот чат или игра уже привязаны.",
            )
            return

        bound_event = await self.events.get(event.event_id)
        if bound_event is None or bound_event.telegram_chat_id != update.chat_id:
            raise RuntimeError("Telegram chat binding was not persisted")
        if bound_event.telegram_chat_invite_link is None:
            invite_link = await self.telegram.ensure_primary_invite_link(
                chat_id=update.chat_id,
                request_id=f"telegram-chat:{update.update_id}:invite",
            )
            await self.events.set_telegram_chat_invite_link(event.event_id, update.chat_id, invite_link)
            bound_event = await self.events.get(event.event_id)
            if bound_event is None or bound_event.telegram_chat_invite_link != invite_link:
                raise RuntimeError("Telegram chat invite link was not persisted")

        await self.telegram.answer_callback(
            callback_query_id=update.callback_query_id or "",
            request_id=f"telegram-chat:{update.update_id}:answered",
        )
        if update.message_id is not None:
            await self.telegram.delete_message(
                chat_id=update.chat_id,
                message_id=update.message_id,
                request_id=f"telegram-chat:{update.update_id}:delete-picker",
            )
        await self.telegram.send_message(
            chat_id=update.chat_id,
            request_id=f"telegram-chat:{update.update_id}:confirmation",
            text=f"Чат был привязан к игре {event.name}",
        )

    async def _handle_callback(self, update: TelegramGroupUpdate) -> None:
        if not await self._actor_can_tie(update):
            await self.telegram.answer_callback(
                callback_query_id=update.callback_query_id or "",
                request_id=f"telegram-chat:{update.update_id}:denied",
                text="Недостаточно прав.",
            )
            return
        value = update.callback_data or ""
        if value.startswith("tie:p:"):
            try:
                page_number = int(value.removeprefix("tie:p:"))
            except ValueError:
                page_number = 0
            await self.telegram.answer_callback(
                callback_query_id=update.callback_query_id or "",
                request_id=f"telegram-chat:{update.update_id}:page-answered",
            )
            await self._show_picker(update, page_number=page_number, edit=True)
            return
        if value.startswith("tie:e:"):
            await self._tie(update, value.removeprefix("tie:e:"))

    async def _handle_join(self, update: TelegramGroupUpdate) -> None:
        if update.member_is_bot or update.member_user_id is None:
            return
        event = await self.events.get_by_telegram_chat_id(update.chat_id)
        if event is None or event.archived_at is not None:
            return
        registration = await self.registrations.get_registration_for_telegram_member(
            event.event_id,
            update.member_user_id,
            update.member_username,
        )
        if registration is None or registration.attendance_status is AttendanceStatus.CANCELLED:
            return
        title = telegram_administrator_title(registration.display_name)
        await self.telegram.promote_title_only_admin(
            chat_id=update.chat_id,
            user_id=update.member_user_id,
            request_id=f"telegram-chat:{update.update_id}:promote",
        )
        await self.telegram.set_administrator_title(
            chat_id=update.chat_id,
            user_id=update.member_user_id,
            title=title,
            request_id=f"telegram-chat:{update.update_id}:title",
        )

    async def handle(self, update: TelegramGroupUpdate) -> None:
        if update.action == "bot_admin_granted":
            await self._show_picker(update)
        elif update.action == "tie_command":
            if await self._actor_can_tie(update):
                await self._show_picker(update)
            else:
                await self.telegram.send_message(
                    chat_id=update.chat_id,
                    request_id=f"telegram-chat:{update.update_id}:denied",
                    text="Недостаточно прав.",
                )
        elif update.action == "tie_callback":
            await self._handle_callback(update)
        elif update.action == "member_joined":
            await self._handle_join(update)
