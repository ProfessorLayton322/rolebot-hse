from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from larp_bot.domain.models import (
    MAX_EVENT_PAGE_SIZE,
    AttendanceStatus,
    BotResponse,
    Button,
    CharacterWishPayload,
    CoPlayerWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    Platform,
    Registration,
    ReplyContext,
    TelegramUser,
    User,
    event_name_with_date,
    event_reference,
    normalize_telegram_handle,
    surname_and_name,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    BACK,
    CANCEL_DIALOG,
    CHARACTER_WISHES,
    CO_PLAYER_WISH_SKIP,
    CO_PLAYER_WISHES,
    ENLIST_SKIP,
    INACTIVE_GAME_RESPONSE,
    INVALID_INPUT_RESPONSE,
    KEEP,
    MAX_CHARACTER_WISH_LENGTH,
    MAX_CO_PLAYER_WISH_LENGTH,
    MY_GAMES,
    NO_CO_PLAYER_WISH,
    NO_WISHES,
    PAGE_SIZE,
    PROFILE,
    REGISTRATION_OPEN_STATUSES,
    _attendance_status_text,
    _event_buttons,
    _my_game_text,
    _no_games_for_action_text,
    _pagination_buttons,
    _player_game_picker_text,
    _signup_game_text,
    co_player_wish_prompt,
    confirmation_replacement_prompt,
    dialog_exit_button,
    is_profile_complete,
    user_id,
)
from .deadlines import format_confirmation_deadline
from .navigation import ADMIN_MENU, MAIN_MENU_LABEL, main_menu_button


class PlayerConversationMixin(ConversationContext):
    """Player registration, confirmation, wishes, and cancellation flows."""

    async def _show_events(
        self,
        flow: str,
        statuses: frozenset[EventStatus] | None,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        empty_text: str = "Подходящих игр сейчас нет.",
    ) -> BotResponse:
        if flow == "enlist":
            events: list[Event] = []
            cursor: tuple[datetime, str] | None = None
            while True:
                page = list(
                    await self.events.list_page(
                        statuses=statuses,
                        after=cursor,
                        archived=False,
                        registration_opened=True,
                        limit=MAX_EVENT_PAGE_SIZE,
                    )
                )
                events.extend(page)
                if len(page) < MAX_EVENT_PAGE_SIZE:
                    break
                last = page[-1]
                cursor = (last.created_at, last.event_id)
        else:
            events = list(
                await self.events.list_page(
                    statuses=statuses,
                    after=after,
                    before=before,
                    archived=False,
                    limit=PAGE_SIZE,
                )
            )
        cancel = ADMIN_MENU if flow.startswith("admin-") else CANCEL_DIALOG
        if not events:
            return BotResponse(
                text=empty_text,
                buttons=_pagination_buttons(previous=None, following=None, cancel=cancel),
            )
        if flow == "enlist":
            return BotResponse(
                text=_player_game_picker_text(flow),
                buttons=[
                    *_event_buttons(events, flow),
                    *_pagination_buttons(previous=None, following=None, cancel=cancel),
                ],
            )
        first, last = events[0], events[-1]
        previous, following = await asyncio.gather(
            self.events.list_page(
                statuses=statuses,
                before=(first.created_at, first.event_id),
                archived=False,
                registration_opened=True if flow == "enlist" else None,
                limit=1,
            ),
            self.events.list_page(
                statuses=statuses,
                after=(last.created_at, last.event_id),
                archived=False,
                registration_opened=True if flow == "enlist" else None,
                limit=1,
            ),
        )
        buttons = _event_buttons(events, flow)
        buttons.extend(
            _pagination_buttons(
                previous=self._event_cursor("e", "p", flow, first) if previous else None,
                following=self._event_cursor("e", "n", flow, last) if following else None,
                cancel=cancel,
            )
        )
        text = _player_game_picker_text(flow) if flow == "enlist" else "🎮 Выберите игру:"
        return BotResponse(text=text, buttons=buttons)

    async def _show_open_events(self, flow: str) -> BotResponse:
        return await self._show_events(
            flow,
            REGISTRATION_OPEN_STATUSES,
            empty_text=_no_games_for_action_text(flow),
        )

    @staticmethod
    def _event_cursor(kind: str, direction: str, flow: str, event: Event) -> str:
        flow_codes = {
            "enlist": "e",
            "confirm": "c",
            "character": "w",
            "wishes": "g",
            "cancel": "x",
            "mine": "m",
            "admin-status": "s",
            "admin-archive": "a",
            "admin-delete": "d",
            "admin-reminder": "r",
            "admin-notification": "n",
            "admin-pass-create": "p",
            "admin-leader-add": "l",
        }
        micros = int(event.created_at.timestamp() * 1_000_000)
        return f"page:{kind}:{direction}:{flow_codes[flow]}:{micros}:{event.event_id}"

    async def _page(self, user: User, value: str) -> BotResponse:
        if value.startswith(("page:a:", "page:r:")):
            parts = value.split(":", 4)
            if len(parts) != 5 or parts[2] not in {"n", "p"} or not parts[3].isdigit():
                return BotResponse(text="Некорректная страница.")
            if not await self._has_admin_access(user):
                return BotResponse(text="Недостаточно прав.")
            archived = parts[1] == "r"
            if archived and not await self._is_admin(user):
                return BotResponse(text="Недостаточно прав.")
            cursor = (datetime.fromtimestamp(int(parts[3]) / 1_000_000, UTC), parts[4])
            if parts[2] == "p":
                return await self._show_admin_games(user, before=cursor, archived=archived)
            return await self._show_admin_games(user, after=cursor, archived=archived)
        parts = value.split(":", 5)
        if len(parts) == 6 and parts[1] in {"e", "u"}:
            flow_codes = {
                "e": "enlist",
                "c": "confirm",
                "w": "character",
                "g": "wishes",
                "x": "cancel",
                "m": "mine",
                "s": "admin-status",
                "a": "admin-archive",
                "d": "admin-delete",
                "r": "admin-reminder",
                "n": "admin-notification",
                "p": "admin-pass-create",
                "l": "admin-leader-add",
            }
            _, kind, direction, flow_code, micros, event_id = parts
            flow = flow_codes.get(flow_code)
            if direction not in {"n", "p"} or flow is None or not micros.isdigit():
                return BotResponse(text="Некорректная страница.")
            if flow.startswith("admin-") and not await self._has_admin_access(user):
                return BotResponse(text="Недостаточно прав.")
            cursor = (datetime.fromtimestamp(int(micros) / 1_000_000, UTC), event_id)
            if kind == "u" and flow == "mine":
                if direction == "p":
                    return await self._show_my_games(user, before=cursor)
                return await self._show_my_games(user, after=cursor)
            if kind == "u":
                if direction == "p":
                    return await self._show_registered(user, flow, before=cursor)
                return await self._show_registered(user, flow, after=cursor)
            event_flow_statuses = {
                "enlist": REGISTRATION_OPEN_STATUSES,
                "admin-status": None,
                "admin-archive": None,
                "admin-delete": None,
                "admin-reminder": frozenset({EventStatus.CONFIRMATION_OPEN}),
                "admin-notification": None,
                "admin-pass-create": None,
                "admin-leader-add": None,
            }
            if flow not in event_flow_statuses:
                return BotResponse(text="Некорректная страница.")
            if direction == "p":
                return await self._show_events(flow, event_flow_statuses[flow], before=cursor)
            return await self._show_events(flow, event_flow_statuses[flow], after=cursor)
        parts = value.split(":", 3)
        if len(parts) != 4 or not parts[2].isdigit():
            return BotResponse(text="Некорректная страница.")
        _, flow, micros, event_id = parts
        if flow.startswith("admin-") and not await self._has_admin_access(user):
            return BotResponse(text="Недостаточно прав.")
        after = (datetime.fromtimestamp(int(micros) / 1_000_000, UTC), event_id)
        event_flow_statuses = {
            "enlist": REGISTRATION_OPEN_STATUSES,
            "admin-status": None,
            "admin-archive": None,
            "admin-delete": None,
            "admin-reminder": frozenset({EventStatus.CONFIRMATION_OPEN}),
            "admin-notification": None,
            "admin-pass-create": None,
            "admin-leader-add": None,
        }
        if flow in event_flow_statuses:
            return await self._show_events(flow, event_flow_statuses[flow], after=after)
        if flow == "admin-list":
            return await self._show_admin_games(user)
        if flow == "admin-pass-list":
            return await self._show_admin_games(user)
        return await self._show_registered(user, flow, after=after)

    @staticmethod
    def _game_wishes_menu(event: Event) -> BotResponse:
        return BotResponse(
            text=f"Пожелания по игре {event_reference(event.name, event.game_date)}",
            buttons=[
                Button(label=CHARACTER_WISHES, value=f"wishes:character:{event.event_id}"),
                Button(label=CO_PLAYER_WISHES, value=f"wishes:co-player:{event.event_id}"),
            ],
        )

    @staticmethod
    def _begin_confirmation_wish(
        user: User,
        event: Event,
        registration: Registration,
        *,
        replaced_event_id: str | None = None,
    ) -> BotResponse:
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "existing_character_wish": registration.character_wish,
        }
        if replaced_event_id is not None:
            user.dialog_context["replaced_event_id"] = replaced_event_id
        user.dialog_state = "CONFIRM_WISH"
        current = registration.character_wish or "ещё не указаны"
        buttons = [Button(label=NO_WISHES, value=NO_WISHES), Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)]
        if registration.character_wish:
            buttons.insert(0, Button(label=KEEP, value=KEEP))
        return BotResponse(
            text=(
                f"Игра: {event_reference(event.name, event.game_date)}\n"
                f"Статус: {_attendance_status_text(registration.attendance_status)}\n\n"
                f"Текущие пожелания: {current}\n\n"
                "Отправьте пожелания по персонажу для этой игры.\n\n"
                "После отправки ответа ваше участие будет подтверждено."
            ),
            buttons=[
                *buttons[:-1],
                dialog_exit_button(label="Не подтверждать сейчас"),
            ],
        )

    async def _show_my_games(
        self,
        user: User,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> BotResponse:
        identity = self._identity(user)
        page = await self.registrations.participating_games_page(
            identity.platform,
            identity.platform_user_id,
            after=after,
            before=before,
            event_limit=PAGE_SIZE,
        )
        if not page.events:
            return BotResponse(text=_no_games_for_action_text("mine"))
        if len(page.events) == 1 and not page.has_previous and not page.has_next:
            return await self._my_game_page(user, page.events[0], from_list=False)
        buttons = _event_buttons(page.events, "mine")
        buttons.extend(
            _pagination_buttons(
                previous=self._event_cursor("u", "p", "mine", page.events[0]) if page.has_previous else None,
                following=self._event_cursor("u", "n", "mine", page.events[-1]) if page.has_next else None,
                cancel=CANCEL_DIALOG,
            )
        )
        return BotResponse(text=_player_game_picker_text("mine"), buttons=buttons)

    async def _my_game_page(self, user: User, event: Event, *, from_list: bool) -> BotResponse:
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        if (
            self.administration.mirrors_google_tables
            and event.public_table_public_url is not None
            and event.public_table_google_url is None
        ):
            event = await self.administration.ensure_event_tables(event)
        identity = self._identity(user)
        overview, masters = await asyncio.gather(
            self.registrations.game_overview(event, identity.platform, identity.platform_user_id),
            self._event_master_records(event.event_id),
        )
        if overview is None:
            return BotResponse(text="Вы больше не участвуете в этой игре.")
        # A lone game skips the list, so a way back to it would only reopen this page.
        buttons = [Button(label=BACK, value=MY_GAMES)] if from_list else []
        return BotResponse(text=_my_game_text(event, overview, masters), buttons=[*buttons, main_menu_button()])

    async def _show_registered(
        self,
        user: User,
        flow: str,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> BotResponse:
        if not is_profile_complete(user):
            return BotResponse(
                text="Сначала заполните профиль.",
                buttons=[Button(label=PROFILE, value=PROFILE)],
            )
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        event_statuses: frozenset[EventStatus] | None
        archived: bool | None
        registration_statuses: frozenset[AttendanceStatus] | None
        if flow == "confirm":
            event_statuses = frozenset({EventStatus.CONFIRMATION_OPEN})
            archived = False
            registration_statuses = frozenset({AttendanceStatus.WAITING})
        else:
            event_statuses = None
            archived = False if flow in {"cancel", "wishes"} else None
            registration_statuses = (
                frozenset({AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED})
                if flow in {"cancel", "wishes"}
                else None
            )
        page = await self.registrations.registered_games_page(
            platform,
            user_id(user),
            statuses=event_statuses,
            archived=archived,
            registration_statuses=registration_statuses,
            after=after,
            before=before,
            event_limit=PAGE_SIZE,
        )
        matches = page.rows
        actionable_matches = matches
        if flow == "character":
            actionable_matches = [
                item
                for item in matches
                if item[0].archived_at is None and item[1].attendance_status is not AttendanceStatus.CANCELLED
            ]
        if flow == "wishes" and len(matches) == 1 and not page.has_previous and not page.has_next:
            return self._game_wishes_menu(matches[0][0])
        buttons = [
            Button(
                label=event_name_with_date(event.name, event.game_date),
                value=f"select:{flow}:{event.event_id}",
            )
            for event, _ in actionable_matches
        ]
        buttons.extend(
            _pagination_buttons(
                previous=(
                    self._event_cursor("u", "p", flow, page.first_event)
                    if page.has_previous and page.first_event is not None
                    else None
                ),
                following=(
                    self._event_cursor("u", "n", flow, page.last_event)
                    if page.has_next and page.last_event is not None
                    else None
                ),
                cancel=CANCEL_DIALOG,
            )
        )
        if not matches and not page.has_previous and not page.has_next:
            empty_text = _no_games_for_action_text(flow)
            if flow == "confirm":
                cancelled_page = await self.registrations.registered_games_page(
                    platform,
                    user_id(user),
                    statuses=frozenset({EventStatus.CONFIRMATION_OPEN}),
                    archived=False,
                    registration_statuses=frozenset({AttendanceStatus.CANCELLED}),
                    event_limit=1,
                )
                if cancelled_page.rows:
                    empty_text += " Если вы отменили участие, сначала запишитесь на игру снова."
            return BotResponse(text=empty_text, buttons=buttons)
        if flow in {"cancel", "character", "wishes"} and not actionable_matches:
            return BotResponse(text=_no_games_for_action_text(flow), buttons=buttons)
        return BotResponse(text=_player_game_picker_text(flow), buttons=buttons)

    async def _select_event(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        try:
            _, flow, event_id = value.split(":", 2)
        except ValueError:
            return BotResponse(text="Некорректный выбор.")
        event = await self.events.get(event_id)
        if event is None:
            return BotResponse(text="Игра не найдена.")
        platform = message.identity.platform
        uid = message.identity.platform_user_id
        if flow == "enlist":
            if not is_profile_complete(user):
                return BotResponse(
                    text="Сначала полностью заполните профиль.",
                    buttons=[Button(label=PROFILE, value=PROFILE)],
                )
            if event.archived_at is not None:
                return BotResponse(text=INACTIVE_GAME_RESPONSE)
            if not event.registration_is_open():
                assert event.registration_open_at is not None
                return BotResponse(
                    text=(
                        "Регистрация на эту игру откроется "
                        f"{format_confirmation_deadline(event.registration_open_at)} (МСК)."
                    )
                )
            if event.status is EventStatus.CLOSED:
                return BotResponse(text="Регистрация на эту игру уже закрыта.")
            registration = await self.registrations.get_registration(event_id, platform, uid)
            if registration is not None and registration.attendance_status is not AttendanceStatus.CANCELLED:
                return BotResponse(text=f"Вы уже записаны на игру {event_reference(event.name, event.game_date)}.")
            same_day_registrations = (
                await self.registrations.registrations_on_date(
                    platform,
                    uid,
                    event.game_date,
                    statuses={AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED},
                    exclude_event_id=event.event_id,
                    conflicts_with=event,
                )
                if event.game_date is not None
                else []
            )
            user.dialog_context = {
                "event_id": event_id,
                "event_name": event.name,
                "same_day_warning_shown": bool(same_day_registrations),
            }
            user.dialog_state = "ENLIST_WISH_PLAY"
            response = co_player_wish_prompt(
                skip_value="enlist:wish-play:skip",
                skip_label=ENLIST_SKIP,
                same_day_warning_date=event.game_date if same_day_registrations else None,
            )
            response.text = f"{_signup_game_text(event, platform)}\n\n{response.text}"
            return response
        if flow in {
            "admin-status",
            "admin-archive",
            "admin-delete",
            "admin-reminder",
            "admin-notification",
            "admin-pass-create",
            "admin-leader-add",
        }:
            return await self._admin_select(user, message, event, flow)
        if flow == "mine":
            return await self._my_game_page(user, event, from_list=True)
        registration = await self.registrations.get_registration(event_id, platform, uid)
        if registration is None:
            return BotResponse(text="Сначала запишитесь на эту игру.")
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        if registration.attendance_status is AttendanceStatus.CANCELLED and flow in {
            "confirm",
            "character",
            "wishes",
        }:
            return BotResponse(text="Сначала снова запишитесь на эту игру.")
        if flow == "confirm" and event.status is not EventStatus.CONFIRMATION_OPEN:
            if event.status is EventStatus.CREATED:
                return BotResponse(
                    text=(
                        f"Вы записаны на игру {event_reference(event.name, event.game_date)}, "
                        "но подтверждение участия ещё не открыто. "
                        "Организаторы откроют его позже."
                    )
                )
            return BotResponse(
                text=(
                    f"Регистрация и подтверждение участия в игре "
                    f"{event_reference(event.name, event.game_date)} закрыты."
                )
            )
        if flow == "wishes":
            return self._game_wishes_menu(event)
        if flow == "confirm":
            confirmed_same_day = (
                await self.registrations.registrations_on_date(
                    platform,
                    uid,
                    event.game_date,
                    statuses={AttendanceStatus.CONFIRMED},
                    exclude_event_id=event.event_id,
                    conflicts_with=event,
                )
                if event.game_date is not None
                else []
            )
            if confirmed_same_day:
                replaced_event, _ = confirmed_same_day[0]
                user.dialog_state = "CONFIRM_REPLACE"
                user.dialog_context = {
                    "event_id": event.event_id,
                    "replaced_event_id": replaced_event.event_id,
                }
                return confirmation_replacement_prompt(event, replaced_event)
            return self._begin_confirmation_wish(user, event, registration)
        user.dialog_context = {
            "event_id": event_id,
            "event_name": event.name,
            "existing_character_wish": registration.character_wish,
        }
        if flow == "character":
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                await self._clear(user)
                return BotResponse(text="Сначала снова запишитесь на эту игру.")
            if registration.attendance_status is AttendanceStatus.WAITING and not registration.character_wish:
                await self._clear(user)
                return BotResponse(
                    text=("Пожелания ещё не вводились. Впервые укажите их через «✅ Подтвердить участие».")
                )
            user.dialog_state = "CHARACTER_EDIT"
            return BotResponse(
                text=(
                    f"Игра: {event_reference(event.name, event.game_date)}\n"
                    f"Статус: {_attendance_status_text(registration.attendance_status)}\n\n"
                    f"Текущие пожелания:\n{registration.character_wish}\n\n"
                    "Отправьте новый вариант."
                ),
                buttons=[Button(label=NO_WISHES, value=NO_WISHES), dialog_exit_button()],
            )
        if flow == "cancel":
            user.dialog_state = "CANCEL_CONFIRM"
            return BotResponse(
                text=(
                    "Вы уверены, что хотите отменить участие в игре "
                    f"{event_reference(event.name, event.game_date)}? "
                    "Если вы отмените участие, то сможете записаться снова, но попадёте "
                    "в конец очереди на основной состав."
                ),
                buttons=[
                    Button(label="Да, отменить", value="cancel:yes"),
                    dialog_exit_button(label="Нет, оставить участие"),
                ],
            )
        return BotResponse(text="Некорректное действие.")

    async def _confirm_replacement_step(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse:
        event_id = str(user.dialog_context["event_id"])
        replaced_event_id = str(user.dialog_context["replaced_event_id"])
        event, replaced_event = await asyncio.gather(
            self.events.get(event_id),
            self.events.get(replaced_event_id),
        )
        if event is None or replaced_event is None:
            await self._clear(user)
            return BotResponse(text="Одна из игр больше не найдена.")
        if value != "confirm:replace:yes":
            return confirmation_replacement_prompt(event, replaced_event)
        if event.archived_at is not None:
            await self._clear(user)
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        if event.status is not EventStatus.CONFIRMATION_OPEN:
            await self._clear(user)
            return BotResponse(text="Подтверждение участия в этой игре уже закрыто.")
        registration = await self.registrations.get_registration(
            event.event_id,
            message.identity.platform,
            message.identity.platform_user_id,
        )
        if registration is None or registration.attendance_status is AttendanceStatus.CANCELLED:
            await self._clear(user)
            return BotResponse(text="Сначала снова запишитесь на эту игру.")
        return self._begin_confirmation_wish(
            user,
            event,
            registration,
            replaced_event_id=replaced_event.event_id,
        )

    async def _wishes_action(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        try:
            _, action, event_id = value.split(":", 2)
        except ValueError:
            return BotResponse(text="Некорректный выбор.")
        if action == "character":
            return await self._select_event(user, message, f"select:character:{event_id}")
        if action != "co-player":
            return BotResponse(text="Некорректный выбор.")
        event = await self.events.get(event_id)
        if event is None:
            return BotResponse(text="Игра не найдена.")
        registration = await self.registrations.get_registration(
            event_id,
            message.identity.platform,
            message.identity.platform_user_id,
        )
        if registration is None:
            return BotResponse(text="Сначала запишитесь на эту игру.")
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        if registration.attendance_status is AttendanceStatus.CANCELLED:
            return BotResponse(text="Сначала снова запишитесь на эту игру.")
        user.dialog_context = {"event_id": event_id, "event_name": event.name}
        user.dialog_state = "CO_PLAYER_EDIT"
        return co_player_wish_prompt(skip_value=CO_PLAYER_WISH_SKIP)

    async def _enlist_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not is_profile_complete(user):
            await self._clear(user)
            return BotResponse(
                text="Сначала полностью заполните профиль.",
                buttons=[Button(label=PROFILE, value=PROFILE)],
            )
        if not value:
            return BotResponse(text="Введите ответ текстом.")
        if user.dialog_state == "ENLIST_WISH_PLAY":
            if message.callback is not None and value != "enlist:wish-play:skip":
                return BotResponse(
                    text=(
                        f"Используйте текущую клавиатуру: нажмите «{ENLIST_SKIP}» или "
                        "отправьте сообщением, с кем вы хотите играть."
                    ),
                    buttons=[
                        Button(label=ENLIST_SKIP, value="enlist:wish-play:skip"),
                        dialog_exit_button(),
                    ],
                )
            if value != "enlist:wish-play:skip" and len(value) > MAX_CO_PLAYER_WISH_LENGTH:
                return BotResponse(
                    text=INVALID_INPUT_RESPONSE,
                    buttons=[Button(label=ENLIST_SKIP, value="enlist:wish-play:skip"), dialog_exit_button()],
                )
            user.dialog_context["wish_play"] = NO_CO_PLAYER_WISH if value == "enlist:wish-play:skip" else value
            return await self._enqueue_enlist(user, message)
        if user.dialog_state == "ENLIST_CONFIRM":
            # Complete signups started before the confirmation step was removed.
            if value != "enlist:confirm":
                return BotResponse(text="Нажмите «Подтвердить» или «В главное меню».")
            return await self._enqueue_enlist(user, message)
        raise AssertionError("unknown enlist state")

    async def _enqueue_enlist(self, user: User, message: InboundMessage) -> BotResponse:
        context = user.dialog_context.copy()
        event = await self.events.get(str(context["event_id"]))
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.")
        if event.archived_at is not None:
            await self._clear(user)
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        registration = await self.registrations.get_registration(
            event.event_id,
            message.identity.platform,
            message.identity.platform_user_id,
        )
        if registration is not None and registration.attendance_status is not AttendanceStatus.CANCELLED:
            await self._clear(user)
            return BotResponse(text=f"Вы уже записаны на игру {event_reference(event.name, event.game_date)}.")
        await self._clear(user)
        if isinstance(user, TelegramUser):
            vk_profile = user.vk_url or ""
            telegram_handle = normalize_telegram_handle(message.telegram_username)
        else:
            vk_profile = f"https://vk.com/id{user.vk_id}"
            telegram_handle = user.telegram_handle
        telegram_profile = None if telegram_handle is None else f"https://t.me/{telegram_handle.removeprefix('@')}"
        await self.registrations.enqueue(
            operation=Operation.ENLIST,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EnlistPayload(
                display_name=surname_and_name(user.full_name or ""),
                nickname=user.nickname,
                wish_play=str(context["wish_play"]),
                larp_experience=user.larp_experience,
                crossplay=user.crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
                same_day_warning_shown=bool(context.get("same_day_warning_shown")),
            ),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                buttons=[main_menu_button()],
            ),
            idempotency_key=f"{message.update_id}:ENLIST",
        )
        return BotResponse(text="⏳ Запись принята в обработку.", deferred=True, command_enqueued=True)

    async def _character_command(
        self,
        user: User,
        message: InboundMessage,
        value: str,
        operation: Operation,
    ) -> BotResponse:
        context = user.dialog_context.copy()
        if value == KEEP:
            existing = str(context.get("existing_character_wish", ""))
            if not existing:
                return BotResponse(text="Сохранённых пожеланий пока нет. Введите текст или выберите «Без пожеланий».")
            wish = existing
        elif value == NO_WISHES:
            wish = NO_WISHES
        else:
            wish = value.strip()
            if not wish:
                return BotResponse(text="Введите текст или выберите «Без пожеланий».")
        if len(wish) > MAX_CHARACTER_WISH_LENGTH:
            return BotResponse(
                text=INVALID_INPUT_RESPONSE,
                buttons=(
                    [
                        Button(label=KEEP, value=KEEP),
                        Button(label=NO_WISHES, value=NO_WISHES),
                        dialog_exit_button(label="Не подтверждать сейчас"),
                    ]
                    if operation is Operation.CONFIRM and context.get("existing_character_wish")
                    else [
                        Button(label=NO_WISHES, value=NO_WISHES),
                        dialog_exit_button(
                            label="Не подтверждать сейчас" if operation is Operation.CONFIRM else MAIN_MENU_LABEL
                        ),
                    ]
                ),
            )
        await self._clear(user)
        event = await self.events.get(str(context["event_id"]))
        if event is None:
            return BotResponse(text="Игра не найдена.")
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        await self.registrations.enqueue(
            operation=operation,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=CharacterWishPayload(
                character_wish=wish,
                replaced_event_id=(
                    str(context["replaced_event_id"])
                    if operation is Operation.CONFIRM and context.get("replaced_event_id") is not None
                    else None
                ),
            ),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=(
                    ""
                    if operation is Operation.CONFIRM
                    else f"🎭 Пожелания для игры {event_reference(event.name, event.game_date)} обновлены."
                ),
                buttons=[main_menu_button()],
            ),
            idempotency_key=f"{message.update_id}:{operation.value}",
        )
        return BotResponse(text="⏳ Изменение принято в обработку.", deferred=True, command_enqueued=True)

    async def _co_player_wish_command(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse:
        buttons = co_player_wish_prompt(skip_value=CO_PLAYER_WISH_SKIP).buttons
        if message.callback is not None and value == CO_PLAYER_WISH_SKIP:
            wish = NO_CO_PLAYER_WISH
        else:
            wish = value.strip()
            if not wish:
                return BotResponse(text="Введите текст или выберите «Пропустить».", buttons=buttons)
        if len(wish) > MAX_CO_PLAYER_WISH_LENGTH:
            return BotResponse(text=INVALID_INPUT_RESPONSE, buttons=buttons)
        context = user.dialog_context.copy()
        await self._clear(user)
        event = await self.events.get(str(context["event_id"]))
        if event is None:
            return BotResponse(text="Игра не найдена.")
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        registration = await self.registrations.get_registration(
            event.event_id,
            message.identity.platform,
            message.identity.platform_user_id,
        )
        if registration is None:
            return BotResponse(text="Сначала запишитесь на эту игру.")
        if registration.attendance_status is AttendanceStatus.CANCELLED:
            return BotResponse(text="Сначала снова запишитесь на эту игру.")
        await self.registrations.enqueue(
            operation=Operation.UPDATE_CO_PLAYER_WISH,
            event_id=event.event_id,
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=CoPlayerWishPayload(wish_play=wish),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=(
                    f"🎭 Пожелания по соигрокам для игры {event_reference(event.name, event.game_date)} обновлены."
                ),
                buttons=[main_menu_button()],
            ),
            idempotency_key=f"{message.update_id}:{Operation.UPDATE_CO_PLAYER_WISH.value}",
        )
        return BotResponse(text="⏳ Изменение принято в обработку.", deferred=True, command_enqueued=True)

    async def _cancel_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if value != "cancel:yes":
            return BotResponse(text="Нажмите «Да, отменить» или «Нет, оставить участие».")
        context = user.dialog_context.copy()
        await self._clear(user)
        event = await self.events.get(str(context["event_id"]))
        if event is None:
            return BotResponse(text="Игра не найдена.")
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        await self.registrations.enqueue(
            operation=Operation.CANCEL,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EmptyPayload(),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=f"❌ Участие в игре {event_reference(event.name, event.game_date)} отменено.",
                buttons=[main_menu_button()],
            ),
            idempotency_key=f"{message.update_id}:CANCEL",
        )
        return BotResponse(text="⏳ Отмена принята в обработку.", deferred=True, command_enqueued=True)
