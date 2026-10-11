from __future__ import annotations

import re
from datetime import UTC, datetime

from larp_bot.domain.models import (
    BotResponse,
    Button,
    Event,
    InboundMessage,
    Platform,
    TelegramUser,
    User,
    event_reference,
    normalize_telegram_handle,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    ADMIN,
    ADMIN_ACTIONS,
    BACK,
    CANCEL,
    CANCEL_DIALOG,
    CHARACTER,
    CONFIRM,
    ENLIST,
    FREE_TEXT_DIALOG_STATES,
    GAME_WISHES,
    LEGACY_MANAGE_MODERATORS,
    LEGACY_STATUS_ACTIONS,
    LOGGER,
    MANAGE_MODERATORS,
    MY_GAMES,
    PROFILE,
    PROFILE_EDITED,
    UNSUPPORTED_CONTENT_RESPONSE,
    WELCOME_TEXT,
    _start_command,
    is_profile_complete,
    main_buttons,
)
from .deadlines import format_confirmation_deadline
from .navigation import ADMIN_MENU, MAIN_MENU, admin_menu_button, main_menu_button
from .ports import AdminConfigProvider, DeferredTransport, EventRepository, UserRepository, VkUserIdResolver, new_user
from .services import EventAdministrationService, RegistrationService


class ConversationCoreMixin(ConversationContext):
    """Core state-machine entry points and persistence."""

    def __init__(
        self,
        users: UserRepository,
        events: EventRepository,
        registrations: RegistrationService,
        administration: EventAdministrationService,
        admins: AdminConfigProvider,
        transport: DeferredTransport | None = None,
        vk_user_ids: VkUserIdResolver | None = None,
    ) -> None:
        self.users = users
        self.events = events
        self.registrations = registrations
        self.administration = administration
        self.admins = admins
        self.transport = transport
        self.vk_user_ids = vk_user_ids

    async def _main(self, user: User, text: str = "Выберите действие:") -> BotResponse:
        return BotResponse(text=text, buttons=main_buttons(await self._has_admin_access(user)))

    async def _save(self, user: User, message: InboundMessage) -> None:
        user.last_update_id = message.update_id
        user.last_update_at = datetime.now(UTC)
        user.updated_at = datetime.now(UTC)
        await self.users.save(user)

    async def _clear(self, user: User) -> None:
        user.dialog_state = "IDLE"
        user.dialog_context = {}

    async def _refresh_telegram_link(self, user: TelegramUser, *, force_vk_resolution: bool = False) -> None:
        linked_vk_id = user.linked_vk_id
        if force_vk_resolution or linked_vk_id is None:
            linked_vk_id = None
            if user.vk_url is not None:
                numeric = re.fullmatch(r"https://vk\.com/id([1-9][0-9]*)", user.vk_url)
                if numeric is not None:
                    linked_vk_id = int(numeric.group(1))
                elif self.vk_user_ids is not None:
                    try:
                        linked_vk_id = await self.vk_user_ids.resolve_user_id(user.vk_url)
                    except Exception:
                        LOGGER.exception("vk_profile_link_resolution_failed", extra={"tg_id": user.tg_id})
        user.linked_vk_id = linked_vk_id
        await self.users.update_telegram_link(user.tg_id, user.telegram_handle, linked_vk_id)

    async def handle(self, message: InboundMessage) -> BotResponse:
        platform = message.identity.platform
        uid = message.identity.platform_user_id
        user = await self.users.get(platform, uid) or new_user(platform, uid)
        if user.last_update_id == message.update_id:
            return BotResponse(text="", silent=True, deferred=platform is Platform.TELEGRAM)
        if isinstance(user, TelegramUser):
            previous_handle = user.telegram_handle
            user.telegram_handle = normalize_telegram_handle(message.telegram_username)
            if previous_handle != user.telegram_handle or (user.vk_url is not None and user.linked_vk_id is None):
                await self._refresh_telegram_link(user)
        value = (message.callback or message.text).strip()
        is_start, start_parameter = _start_command(value, message)
        admin_branch = (
            user.dialog_state.startswith("ADMIN_")
            or value in {ADMIN, ADMIN_MENU}
            or value in ADMIN_ACTIONS
            or value in LEGACY_STATUS_ACTIONS
            or value.startswith(
                ("ag:", "mod:", "master:", "stats:", "select:admin-", "page:admin-", "page:a:", "page:r:")
            )
        )
        current_button_values = {button.value for button in user.last_bot_buttons}
        if is_start:
            await self._clear(user)
            signup_event = (
                await self.events.get_by_signup_token(start_parameter) if start_parameter is not None else None
            )
            if signup_event is not None and signup_event.archived_at is not None:
                signup_event = None
            if signup_event is not None and is_profile_complete(user):
                response = await self._start_game_signup(user, message, signup_event)
            elif not is_profile_complete(user):
                response = await self._begin_profile(
                    user,
                    signup_event_id=None if signup_event is None else signup_event.event_id,
                )
                response.text = f"{WELCOME_TEXT}\n\n{response.text}"
            else:
                response = await self._main(user, WELCOME_TEXT)
        elif message.has_unsupported_content:
            await self._save(user, message)
            return BotResponse(
                text=UNSUPPORTED_CONTENT_RESPONSE,
                buttons=user.last_bot_buttons.copy(),
            )
        elif (
            user.dialog_state in FREE_TEXT_DIALOG_STATES
            and message.callback is not None
            and value not in current_button_values
            and value not in {MAIN_MENU, ADMIN_MENU}
        ):
            response = BotResponse(
                text=(
                    "Эта кнопка устарела. Используйте текущую клавиатуру из последнего сообщения бота "
                    "или отправьте ответ текстом. Если вы увидели это сообщение, "
                    "попробуйте написать боту /start"
                ),
                buttons=user.last_bot_buttons,
            )
        elif value in {"/menu", MAIN_MENU, BACK}:
            await self._clear(user)
            response = await self._main(user, WELCOME_TEXT)
        elif value == CANCEL_DIALOG:
            return_to_profile = user.dialog_state.startswith("PROFILE_") and user.dialog_context.get(
                "profile_flow"
            ) in {"field", "reset"}
            await self._clear(user)
            response = (
                await self._profile_response(user, prefix=PROFILE_EDITED)
                if return_to_profile
                else await self._main(user, "Действие отменено.")
            )
        elif value in {ADMIN_MENU, MANAGE_MODERATORS, LEGACY_MANAGE_MODERATORS}:
            await self._clear(user)
            response = (
                await self._moderator_management_menu(user)
                if value in {MANAGE_MODERATORS, LEGACY_MANAGE_MODERATORS}
                else await self._admin_menu(user)
            )
        elif value.startswith(
            (
                "ag:manage:",
                "ag:edit:",
                "ag:files:",
                "ag:masters:",
                "ag:players:",
                "ag:notifications:",
                "ag:schedule",
            )
        ):
            await self._clear(user)
            response = await self._admin_game_action(user, message, value)
        elif user.dialog_state != "IDLE":
            response = await self._continue(user, message, value)
        else:
            response = await self._start(user, message, value)
        if user.dialog_state == "IDLE" and not response.silent and not response.buttons:
            if admin_branch and await self._has_admin_access(user):
                response.buttons = [admin_menu_button()]
            else:
                response.buttons = [main_menu_button()]
        user.last_bot_buttons = response.buttons.copy()
        await self._save(user, message)
        return response

    async def _start_game_signup(
        self,
        user: User,
        message: InboundMessage,
        event: Event,
    ) -> BotResponse:
        if not event.registration_is_open():
            assert event.registration_open_at is not None
            return await self._main(
                user,
                (
                    f"Регистрация на игру {event_reference(event.name, event.game_date)} ещё не открыта. "
                    f"Она откроется {format_confirmation_deadline(event.registration_open_at)} (МСК)."
                ),
            )
        return await self._select_event(user, message, f"select:enlist:{event.event_id}")

    async def _start(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if value == PROFILE:
            if is_profile_complete(user):
                return await self._profile_response(user)
            return await self._begin_profile(user)
        if value == "profile:menu":
            if not is_profile_complete(user):
                return await self._begin_profile(user)
            return self._profile_edit_menu(user)
        if value == "profile:reset":
            return await self._begin_profile(user, editing_existing=is_profile_complete(user))
        if value == "profile:edit":
            # Keep keyboards sent before the edit-menu rollout working.
            return await self._begin_profile(user, editing_existing=is_profile_complete(user))
        if value.startswith("profile:field:"):
            if not is_profile_complete(user):
                return await self._begin_profile(user)
            return self._begin_profile_field(user, value.removeprefix("profile:field:"))
        if value == ENLIST:
            if not is_profile_complete(user):
                return BotResponse(
                    text="Сначала заполните профиль.",
                    buttons=[Button(label=PROFILE, value=PROFILE)],
                )
            return await self._show_open_events("enlist")
        if value == CONFIRM:
            return await self._show_registered(user, "confirm")
        if value in {GAME_WISHES, CHARACTER}:
            return await self._show_registered(user, "wishes")
        if value == CANCEL:
            return await self._show_registered(user, "cancel")
        if value == MY_GAMES:
            return await self._show_my_games(user)
        if value in {ADMIN, ADMIN_MENU}:
            return await self._admin_menu(user)
        if value.startswith("select:"):
            return await self._select_event(user, message, value)
        if value.startswith("page:"):
            return await self._page(user, value)
        if value.startswith("wishes:"):
            return await self._wishes_action(user, message, value)
        if value.startswith("stats:"):
            return await self._statistics_action(user, message, value)
        if value.startswith("ag:"):
            return await self._admin_game_action(user, message, value)
        if value.startswith(("mod:", "master:")):
            admin_response = await self._admin_callback(user, message, value)
            if admin_response is not None:
                return admin_response
        admin_response = await self.dispatch_idle_admin(user, message, value)
        if admin_response is not None:
            return admin_response
        return await self._main(user, "Не удалось распознать команду. Выберите действие кнопкой.")
