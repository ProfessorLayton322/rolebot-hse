from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from secrets import token_urlsafe

from pydantic import ValidationError

from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    BotResponse,
    Button,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    NotificationPayload,
    Operation,
    PassDetails,
    Platform,
    ReplyContext,
    TelegramUser,
    User,
    normalize_mobile_phone,
    normalize_telegram_handle,
    normalize_telegram_profile,
    normalize_vk_url,
    validate_cyrillic_name,
    validate_latin_name,
    validate_pass_email,
)

from .deadlines import (
    NEAREST_THURSDAY,
    closest_thursday_19,
    format_confirmation_deadline,
    parse_confirmation_deadline,
)
from .navigation import ADMIN_MENU, MAIN_MENU, admin_menu_button, main_menu_button
from .ports import (
    AdminConfigProvider,
    DeferredTransport,
    EventRepository,
    UserRepository,
    VkUserIdResolver,
    new_user,
)
from .services import EventAdministrationService, RegistrationService

PROFILE = "📝 Профиль"
ENLIST = "🎮 Записаться на игру"
CONFIRM = "✅ Подтвердить участие"
CHARACTER = "🎭 Пожелания по персонажу"
CANCEL = "❌ Отменить участие"
ADMIN = "🛠 Администрирование"
BACK = "⬅️ Назад"
CANCEL_DIALOG = "Отмена"
NO_WISHES = "Без пожеланий"
KEEP = "Оставить без изменений"
SKIP = "Пропустить"
NO_CO_PLAYER_WISH = "Без пожеланий"
CHANGE_STATUS = "🔄 Изменить статус"
SEND_CONFIRMATION_REMINDER = "🔔 Напомнить о подтверждении"
SEND_CONFIRMED_NOTIFICATION = "📣 Уведомить подтвердивших"
REMOVE_PLAYER = "🗑 Удалить игрока"
CREATE_PASS_TABLE = "🎫 Создать таблицу пропусков"
LIST_PASS_TABLES = "🔗 Ссылки на таблицы пропусков"
MANAGE_GAMES = "Управление играми"
EVENT_TABLES = "Таблицы участников и пропусков"
GRANT_GAMEMASTER = "🎖 Назначить гейммастера"
ADD_EVENT_LEADER = "👑 Добавить ведущего игры"
ARCHIVE_GAME = "📦 Архивировать игру"
LEGACY_DELETE_GAME = "🗑 Удалить игру"
STATUS_REGISTRATION = "Регистрация"
STATUS_CONFIRMATION = "Подтверждение"
STATUS_CLOSED = "Закрытие регистрации"
GAMEMASTER_NOTIFICATION = "Вам было присуждено звание гейммастера! 🎉🎉🎉"

REGISTRATION_OPEN_STATUSES = frozenset({EventStatus.CREATED, EventStatus.CONFIRMATION_OPEN})
EVENT_STATUS_LABELS = {
    EventStatus.CREATED: STATUS_REGISTRATION,
    EventStatus.CONFIRMATION_OPEN: STATUS_CONFIRMATION,
    EventStatus.CLOSED: STATUS_CLOSED,
}
ADMIN_STATUS_CHOICES = {
    "admin:status:registration": (STATUS_REGISTRATION, Operation.OPEN_REGISTRATION),
    "admin:status:confirmation": (STATUS_CONFIRMATION, Operation.OPEN_CONFIRMATION),
    "admin:status:closed": (STATUS_CLOSED, Operation.CLOSE_EVENT),
}
MASTER_TABLE_DISCLAIMER = "⚠️ Только для ведущих этой игры. Не пересылайте эту ссылку игрокам!"
# Keep buttons sent immediately before this rollout useful; they now open the
# unrestricted status picker instead of applying their former transition.
LEGACY_STATUS_ACTIONS = frozenset({"🔓 Открыть подтверждения", "🔒 Закрыть подтверждения", "🔒 Закрыть регистрацию"})
ADMIN_ACTIONS = frozenset(
    {
        "➕ Создать игру",
        MANAGE_GAMES,
        EVENT_TABLES,
        CHANGE_STATUS,
        SEND_CONFIRMATION_REMINDER,
        SEND_CONFIRMED_NOTIFICATION,
        REMOVE_PLAYER,
        CREATE_PASS_TABLE,
        LIST_PASS_TABLES,
        GRANT_GAMEMASTER,
        ADD_EVENT_LEADER,
        ARCHIVE_GAME,
        LEGACY_DELETE_GAME,
        "📋 Список игр",
    }
)
FREE_TEXT_DIALOG_STATES = frozenset(
    {
        "PROFILE_SURNAME_CYRILLIC",
        "PROFILE_NAME_CYRILLIC",
        "PROFILE_CONTACT",
        "PROFILE_PASS_PATRONYM_CYRILLIC",
        "PROFILE_PASS_SURNAME_LATIN",
        "PROFILE_PASS_NAME_LATIN",
        "PROFILE_PASS_PATRONYM_LATIN",
        "PROFILE_PASS_PHONE",
        "PROFILE_PASS_EMAIL_ADDRESS",
        "ENLIST_WISH_PLAY",
        "CONFIRM_WISH",
        "CHARACTER_EDIT",
        "ADMIN_CREATE_NAME",
        "ADMIN_CONFIRMATION_DEADLINE",
        "ADMIN_NOTIFICATION_TEXT",
        "ADMIN_GAMEMASTER_PROFILE",
        "ADMIN_EVENT_LEADER_PROFILE",
        "ADMIN_ARCHIVE_NAME",
        "ADMIN_DELETE_NAME",
        "ADMIN_REMOVE_PICK",
        "ADMIN_REMOVE_CONFIRM",
    }
)


def yes_no_buttons() -> list[Button]:
    return [Button(label="Да", value="Да"), Button(label="Нет", value="Нет")]


def main_buttons(has_admin_access: bool) -> list[Button]:
    labels = [PROFILE, ENLIST, CONFIRM, CHARACTER, CANCEL]
    if has_admin_access:
        labels.append(ADMIN)
    return [Button(label=label, value=label) for label in labels]


def event_table_links(event: Event) -> str:
    if event.public_table_public_url is None:
        raise RuntimeError("public game table is not initialized")
    if event.pass_table_public_url is None:
        raise RuntimeError("pass table is not initialized")
    return (
        f"Публичная таблица (без контактов Telegram и VK):\n{event.public_table_public_url}\n\n"
        f"Административная таблица:\n{MASTER_TABLE_DISCLAIMER}\n{event.master_table_public_url}\n\n"
        f"Таблица пропусков:\n{event.pass_table_public_url}"
    )


def user_id(user: User) -> int:
    return user.tg_id if isinstance(user, TelegramUser) else user.vk_id


def is_profile_complete(user: User) -> bool:
    return user.profile_complete


def _bool_text(value: bool | None) -> str:
    return "Да" if value else "Нет"


def _event_buttons(events: list[Event], flow: str) -> list[Button]:
    return [Button(label=event.name, value=f"select:{flow}:{event.event_id}") for event in events]


def game_management_button(event_id: str) -> Button:
    return Button(label=BACK, value=f"ag:manage:{event_id}")


def _admin_status_response(event_name: str, current_status: EventStatus, event_id: str) -> BotResponse:
    return BotResponse(
        text=(
            f"Игра: «{event_name}»\n"
            f"Текущий Статус: {EVENT_STATUS_LABELS[current_status]}\n\n"
            "Выберите новый Статус:\n\n"
            f"{STATUS_REGISTRATION} — игроки могут записываться, но ещё не могут подтверждать участие.\n\n"
            f"{STATUS_CONFIRMATION} — игроки могут записываться и подтверждать участие.\n\n"
            f"{STATUS_CLOSED} — игроки не могут ни записываться, ни подтверждать участие."
        ),
        buttons=[Button(label=label, value=value) for value, (label, _) in ADMIN_STATUS_CHOICES.items()]
        + [game_management_button(event_id)],
    )


def _confirmation_deadline_response(event_id: str, *, invalid: bool = False) -> BotResponse:
    prefix = "Некорректная дата и время. Попробуйте ещё раз.\n\n" if invalid else ""
    return BotResponse(
        text=(
            f"{prefix}Введите дедлайн подтверждения строго в формате DD.MM.YY HH:MM или нажмите «{NEAREST_THURSDAY}»."
        ),
        buttons=[
            Button(label=NEAREST_THURSDAY, value="admin:deadline:nearest-thursday"),
            game_management_button(event_id),
        ],
    )


def _confirmed_notification_prompt(event_name: str, event_id: str, *, invalid: str = "") -> BotResponse:
    prefix = f"{invalid}\n\n" if invalid else ""
    return BotResponse(
        text=(
            f"{prefix}Отправьте текст уведомления для подтвердивших участие в игре «{event_name}».\n\n"
            "Можно просто вставить ссылку-приглашение в чат Telegram или VK: бот сам добавит её "
            "и название игры в уведомление."
        ),
        buttons=[game_management_button(event_id)],
    )


class ConversationEngine:
    """One platform-neutral state machine rendered by Telegram and VK adapters."""

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

    async def handle(self, message: InboundMessage) -> BotResponse:
        platform = message.identity.platform
        uid = message.identity.platform_user_id
        user = await self.users.get(platform, uid) or new_user(platform, uid)
        if user.last_update_id == message.update_id:
            return BotResponse(text="", silent=True, deferred=platform is Platform.TELEGRAM)
        if isinstance(user, TelegramUser):
            user.telegram_handle = normalize_telegram_handle(message.telegram_username)

        value = (message.callback or message.text).strip()
        admin_branch = (
            user.dialog_state.startswith("ADMIN_")
            or value in {ADMIN, ADMIN_MENU}
            or value in ADMIN_ACTIONS
            or value in LEGACY_STATUS_ACTIONS
            or value.startswith(("ag:", "select:admin-", "page:admin-", "page:a:"))
        )
        current_button_values = {button.value for button in user.last_bot_buttons}
        if (
            user.dialog_state in FREE_TEXT_DIALOG_STATES
            and message.callback is not None
            and value not in current_button_values
            and value not in {MAIN_MENU, ADMIN_MENU}
        ):
            response = BotResponse(
                text=(
                    "Эта кнопка устарела. Используйте текущую клавиатуру из последнего сообщения бота "
                    "или отправьте ответ текстом."
                ),
                buttons=user.last_bot_buttons,
            )
        elif value in {"/start", "/menu", MAIN_MENU, BACK}:
            await self._clear(user)
            response = await self._main(user, "Привет! Этот бот поможет зарегистрироваться на LARP-игры.")
        elif value == CANCEL_DIALOG:
            await self._clear(user)
            response = await self._main(user, "Действие отменено.")
        elif value == ADMIN_MENU:
            await self._clear(user)
            response = await self._admin_menu(user)
        elif value.startswith("ag:manage:"):
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

    async def _start(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if value == PROFILE:
            if is_profile_complete(user):
                contact = user.vk_url if isinstance(user, TelegramUser) else user.telegram_handle
                contact_label = "VK" if isinstance(user, TelegramUser) else "Telegram"
                return BotResponse(
                    text=(
                        f"Профиль\n\nИмя: {user.full_name}\n{contact_label}: {contact or 'не указан'}\n"
                        f"Кроссплей: {_bool_text(user.crossplay)}\n"
                        f"Опыт LARP: {_bool_text(user.larp_experience)}\n"
                        f"Нужен пропуск: {_bool_text(user.needs_pass)}"
                    ),
                    buttons=[
                        Button(label="Изменить профиль", value="profile:edit"),
                        Button(label=BACK, value=BACK),
                    ],
                )
            return await self._begin_profile(user)
        if value == "profile:edit":
            return await self._begin_profile(user)
        if value == ENLIST:
            if not is_profile_complete(user):
                return BotResponse(
                    text="Сначала зарегистрируйте профиль.",
                    buttons=[Button(label=PROFILE, value=PROFILE)],
                )
            return await self._show_open_events("enlist")
        if value == CONFIRM:
            return await self._show_registered(user, "confirm")
        if value == CHARACTER:
            return await self._show_registered(user, "character")
        if value == CANCEL:
            return await self._show_registered(user, "cancel")
        if value in {ADMIN, ADMIN_MENU}:
            return await self._admin_menu(user)
        if value.startswith("select:"):
            return await self._select_event(user, message, value)
        if value.startswith("page:"):
            return await self._page(user, value)
        if value.startswith("ag:"):
            return await self._admin_game_action(user, message, value)
        admin_response = await self.dispatch_idle_admin(user, value)
        if admin_response is not None:
            return admin_response
        return await self._main(user)

    async def _begin_profile(self, user: User) -> BotResponse:
        user.dialog_state = "PROFILE_SURNAME_CYRILLIC"
        user.dialog_context = {}
        return BotResponse(
            text="Введите фамилию кириллицей:",
            buttons=[Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)],
        )

    async def _continue(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        state = user.dialog_state
        if state.startswith("PROFILE_"):
            return await self._profile_step(user, value)
        if state.startswith("ENLIST_"):
            return await self._enlist_step(user, message, value)
        if state == "CONFIRM_WISH":
            return await self._character_command(user, message, value, Operation.CONFIRM)
        if state == "CHARACTER_EDIT":
            return await self._character_command(user, message, value, Operation.UPDATE_CHARACTER_WISH)
        if state == "CANCEL_CONFIRM":
            return await self._cancel_step(user, message, value)
        if state.startswith("ADMIN_"):
            return await self._admin_step(user, message, value)
        await self._clear(user)
        return await self._main(user, "Диалог сброшен. Выберите действие:")

    async def _profile_step(self, user: User, value: str) -> BotResponse:
        context = user.dialog_context
        state = user.dialog_state
        if state == "PROFILE_NAME":
            user.dialog_state = "PROFILE_SURNAME_CYRILLIC"
            user.dialog_context = {}
            return BotResponse(text="Формат профиля обновился. Введите фамилию кириллицей:")
        if state not in {"PROFILE_SURNAME_CYRILLIC", "PROFILE_NAME_CYRILLIC"} and (
            "surname_cyrillic" not in context or "name_cyrillic" not in context
        ):
            user.dialog_state = "PROFILE_SURNAME_CYRILLIC"
            user.dialog_context = {}
            return BotResponse(text="Формат профиля обновился. Введите фамилию кириллицей:")
        if state in {"PROFILE_SURNAME_CYRILLIC", "PROFILE_NAME_CYRILLIC"}:
            try:
                clean_name = validate_cyrillic_name(value)
            except ValueError:
                field = "фамилию" if state == "PROFILE_SURNAME_CYRILLIC" else "имя"
                return BotResponse(text=f"Введите {field}, используя только кириллицу:")
            if state == "PROFILE_SURNAME_CYRILLIC":
                context["surname_cyrillic"] = clean_name
                user.dialog_state = "PROFILE_NAME_CYRILLIC"
                return BotResponse(text="Введите имя кириллицей:")
            context["name_cyrillic"] = clean_name
            user.dialog_state = "PROFILE_CONTACT"
            if isinstance(user, TelegramUser):
                return BotResponse(text="Введите обязательную ссылку на вашу страницу VK:")
            return BotResponse(
                text="Ваш Telegram username, если он есть:",
                buttons=[Button(label="Пропустить", value="Пропустить")],
            )
        if state == "PROFILE_CONTACT":
            try:
                context["contact"] = (
                    normalize_vk_url(value) if isinstance(user, TelegramUser) else normalize_telegram_handle(value)
                )
            except ValueError as exc:
                return BotResponse(text=f"❌ {exc}. Попробуйте ещё раз:")
            user.dialog_state = "PROFILE_CROSSPLAY"
            return BotResponse(text="Готовы ли вы кроссполу?", buttons=yes_no_buttons())
        boolean_states = {
            "PROFILE_CROSSPLAY": ("crossplay", "PROFILE_EXPERIENCE", "Играли ли вы в LARP до этого?"),
            "PROFILE_EXPERIENCE": (
                "larp_experience",
                "PROFILE_NEEDS_PASS",
                "Нужен ли вам пропуск на локацию?",
            ),
        }
        if state in boolean_states:
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            key, next_state, prompt = boolean_states[state]
            context[key] = value == "Да"
            user.dialog_state = next_state
            return BotResponse(text=prompt, buttons=yes_no_buttons())
        if state == "PROFILE_NEEDS_PASS":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["needs_pass"] = value == "Да"
            if value == "Да":
                user.dialog_state = "PROFILE_PASS_PATRONYM_CYRILLIC"
                return BotResponse(
                    text="Введите отчество кириллицей или «-», если отчества нет:",
                    buttons=[Button(label="Нет отчества", value="-")],
                )
            return await self._finish_profile(user)
        if state in {"PROFILE_PASS_CYRILLIC", "PROFILE_PASS_LATIN", "PROFILE_PASS_EMAIL", "PROFILE_PASS_CITIZEN"}:
            user.dialog_state = "PROFILE_SURNAME_CYRILLIC"
            user.dialog_context = {}
            return BotResponse(text="Формат профиля обновился. Введите фамилию кириллицей:")
        if state == "PROFILE_PASS_FOREIGNER":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["foreigner"] = value == "Да"
            if context["foreigner"]:
                user.dialog_state = "PROFILE_PASS_SURNAME_LATIN"
                return BotResponse(text="Введите фамилию латиницей:")
            user.dialog_state = "PROFILE_PASS_PHONE"
            return BotResponse(text="Введите мобильный телефон:")
        cyrillic_steps = {
            "PROFILE_PASS_PATRONYM_CYRILLIC": (
                "patronym_cyrillic",
                "PROFILE_PASS_FOREIGNER",
                "Вы иностранный гражданин?",
                True,
            ),
        }
        if state in cyrillic_steps:
            key, next_state, prompt, patronym = cyrillic_steps[state]
            try:
                context[key] = validate_cyrillic_name(value, patronym=patronym)
            except ValueError:
                suffix = " или «-»" if patronym else ""
                return BotResponse(text=f"Используйте только кириллицу{suffix}. Попробуйте ещё раз:")
            user.dialog_state = next_state
            buttons = yes_no_buttons() if next_state == "PROFILE_PASS_FOREIGNER" else []
            return BotResponse(text=prompt, buttons=buttons)
        latin_steps = {
            "PROFILE_PASS_SURNAME_LATIN": (
                "surname_latin",
                "PROFILE_PASS_NAME_LATIN",
                "Введите имя латиницей:",
                False,
            ),
            "PROFILE_PASS_NAME_LATIN": (
                "name_latin",
                "PROFILE_PASS_PATRONYM_LATIN",
                "Введите отчество латиницей или «-», если отчества нет:",
                False,
            ),
            "PROFILE_PASS_PATRONYM_LATIN": (
                "patronym_latin",
                "PROFILE_PASS_PHONE",
                "Введите мобильный телефон:",
                True,
            ),
        }
        if state in latin_steps:
            key, next_state, prompt, patronym = latin_steps[state]
            try:
                latin_value = validate_latin_name(value, patronym=patronym)
            except ValueError:
                suffix = " или «-»" if patronym else ""
                return BotResponse(text=f"Используйте только латиницу{suffix}. Попробуйте ещё раз:")
            if patronym and (context.get("patronym_cyrillic") == "-") != (latin_value == "-"):
                return BotResponse(
                    text="Если отчества нет, укажите «-» и в кириллическом, и в латинском поле. Попробуйте ещё раз:",
                    buttons=[Button(label="Нет отчества", value="-")],
                )
            context[key] = latin_value
            user.dialog_state = next_state
            buttons = [Button(label="Нет отчества", value="-")] if next_state == "PROFILE_PASS_PATRONYM_LATIN" else []
            return BotResponse(text=prompt, buttons=buttons)
        if state == "PROFILE_PASS_PHONE":
            try:
                context["mobile_phone"] = normalize_mobile_phone(value)
            except ValueError:
                return BotResponse(text="Некорректный номер. Введите мобильный телефон ещё раз:")
            user.dialog_state = "PROFILE_PASS_EMAIL_ADDRESS"
            return BotResponse(text="Введите email:")
        if state == "PROFILE_PASS_EMAIL_ADDRESS":
            try:
                email = validate_pass_email(value)
            except ValidationError:
                return BotResponse(text="Некорректный email. Введите email ещё раз:")
            context["email"] = email
            return await self._finish_profile(user)
        raise AssertionError(f"unknown profile state: {state}")

    async def _finish_profile(self, user: User) -> BotResponse:
        context = user.dialog_context
        user.full_name = f"{context['surname_cyrillic']} {context['name_cyrillic']}"
        user.crossplay = bool(context["crossplay"])
        user.larp_experience = bool(context["larp_experience"])
        needs_pass = bool(context["needs_pass"])
        if needs_pass:
            details = PassDetails(
                surname_cyrillic=str(context["surname_cyrillic"]),
                name_cyrillic=str(context["name_cyrillic"]),
                patronym_cyrillic=str(context["patronym_cyrillic"]),
                foreigner=bool(context["foreigner"]),
                surname_latin=(str(context["surname_latin"]) if context.get("foreigner") else None),
                name_latin=(str(context["name_latin"]) if context.get("foreigner") else None),
                patronym_latin=(str(context["patronym_latin"]) if context.get("foreigner") else None),
                mobile_phone=str(context["mobile_phone"]),
                email=str(context["email"]),
            )
            user.needs_pass = True
            user.pass_details = details
        else:
            user.pass_details = None
            user.needs_pass = False
        if isinstance(user, TelegramUser):
            user.vk_url = str(context["contact"])
        else:
            contact = context.get("contact")
            user.telegram_handle = None if contact is None else str(contact)
        await self._clear(user)
        return await self._main(user, "✅ Профиль сохранён.")

    async def _show_events(
        self,
        flow: str,
        statuses: frozenset[EventStatus] | None,
        *,
        after: tuple[datetime, str] | None = None,
        empty_text: str = "Подходящих игр сейчас нет.",
    ) -> BotResponse:
        events = list(await self.events.list_page(statuses=statuses, after=after, limit=10))
        if not events:
            return BotResponse(text=empty_text, buttons=[Button(label=BACK, value=BACK)])
        if flow.startswith("admin-"):
            await asyncio.gather(*(self._sync_admin_leaders(event.event_id) for event in events))
        buttons = _event_buttons(events, flow)
        if len(events) == 10:
            last = events[-1]
            more = await self.events.list_page(
                statuses=statuses,
                after=(last.created_at, last.event_id),
                limit=1,
            )
            if more:
                buttons.append(Button(label="➡️ Далее", value=self._cursor(flow, last)))
        buttons.append(Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG))
        return BotResponse(text="🎮 Выберите игру:", buttons=buttons)

    async def _show_open_events(self, flow: str) -> BotResponse:
        return await self._show_events(
            flow,
            REGISTRATION_OPEN_STATUSES,
            empty_text="Сейчас нет игр с открытой регистрацией.",
        )

    @staticmethod
    def _cursor(flow: str, event: Event) -> str:
        micros = int(event.created_at.timestamp() * 1_000_000)
        return f"page:{flow}:{micros}:{event.event_id}"

    async def _page(self, user: User, value: str) -> BotResponse:
        if value.startswith("page:a:"):
            parts = value.split(":", 4)
            if len(parts) != 5 or parts[2] not in {"n", "p"} or not parts[3].isdigit():
                return BotResponse(text="Некорректная страница.")
            if not await self._has_admin_access(user):
                return BotResponse(text="Недостаточно прав.")
            cursor = (datetime.fromtimestamp(int(parts[3]) / 1_000_000, UTC), parts[4])
            if parts[2] == "p":
                return await self._show_admin_games(before=cursor)
            return await self._show_admin_games(after=cursor)
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
            return await self._show_admin_games()
        if flow == "admin-pass-list":
            return await self._show_admin_games()
        return await self._show_registered(user, flow, after=after)

    async def _show_registered(
        self,
        user: User,
        flow: str,
        *,
        after: tuple[datetime, str] | None = None,
    ) -> BotResponse:
        if not is_profile_complete(user):
            return BotResponse(
                text="Сначала зарегистрируйте профиль.",
                buttons=[Button(label=PROFILE, value=PROFILE)],
            )
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        matches, cursor = await self.registrations.registered_games_page(platform, user_id(user), after=after)
        actionable_matches = matches
        if flow == "confirm":
            actionable_matches = [item for item in matches if item[0].status is EventStatus.CONFIRMATION_OPEN]
        buttons = [Button(label=event.name, value=f"select:{flow}:{event.event_id}") for event, _ in actionable_matches]
        if cursor is not None:
            micros = int(cursor[0].timestamp() * 1_000_000)
            buttons.append(Button(label="➡️ Далее", value=f"page:{flow}:{micros}:{cursor[1]}"))
        buttons.append(Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG))
        if not matches and cursor is None:
            return BotResponse(text="У вас пока нет записей на игры.", buttons=buttons)
        if flow == "confirm" and not actionable_matches:
            created = any(event.status is EventStatus.CREATED for event, _ in matches)
            closed = any(event.status is EventStatus.CLOSED for event, _ in matches)
            if created and not closed:
                unavailable_text = "Подтверждение участия для ваших игр ещё не открыто. Организаторы откроют его позже."
            elif closed and not created:
                unavailable_text = "Регистрация и подтверждение участия для ваших игр уже закрыты."
            else:
                unavailable_text = (
                    "Для одних ваших игр подтверждение участия ещё не открыто, а для закрытых игр оно уже недоступно."
                )
            return BotResponse(
                text=unavailable_text,
                buttons=buttons,
            )
        return BotResponse(text="🎭 Выберите игру:", buttons=buttons)

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
            if event.status is EventStatus.CLOSED:
                return BotResponse(text="Регистрация на эту игру уже закрыта.")
            user.dialog_context = {"event_id": event_id, "event_name": event.name}
            user.dialog_state = "ENLIST_WISH_PLAY"
            return BotResponse(
                text=(
                    "С кем бы вы ХОТЕЛИ играть?\n\n"
                    "Нажмите «Пропустить» на клавиатуре или отправьте сообщением, "
                    "с кем вы хотите играть."
                ),
                buttons=[
                    Button(label=SKIP, value="enlist:wish-play:skip"),
                    Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                ],
            )
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
        registration = await self.registrations.get_registration(event_id, platform, uid)
        if registration is None:
            return BotResponse(text="Сначала запишитесь на эту игру.")
        if flow == "confirm" and event.status is not EventStatus.CONFIRMATION_OPEN:
            if event.status is EventStatus.CREATED:
                return BotResponse(
                    text=(
                        f"Вы записаны на игру «{event.name}», но подтверждение участия ещё не открыто. "
                        "Организаторы откроют его позже."
                    )
                )
            return BotResponse(text=f"Регистрация и подтверждение участия в игре «{event.name}» закрыты.")
        user.dialog_context = {
            "event_id": event_id,
            "event_name": event.name,
            "existing_character_wish": registration.character_wish,
        }
        if flow == "confirm":
            user.dialog_state = "CONFIRM_WISH"
            current = registration.character_wish or "ещё не указаны"
            buttons = [Button(label=NO_WISHES, value=NO_WISHES), Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)]
            if registration.character_wish:
                buttons.insert(0, Button(label=KEEP, value=KEEP))
            return BotResponse(
                text=(
                    f"Игра: «{event.name}»\nСтатус: {registration.attendance_status.value}\n\n"
                    f"Текущие пожелания: {current}\n\n"
                    "Отправьте пожелания по персонажу для этой игры."
                ),
                buttons=buttons,
            )
        if flow == "character":
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                await self._clear(user)
                return BotResponse(text="Запись отменена. Сначала снова подтвердите участие.")
            if registration.attendance_status is AttendanceStatus.WAITING and not registration.character_wish:
                await self._clear(user)
                return BotResponse(
                    text=("Пожелания ещё не вводились. Впервые укажите их через «✅ Подтвердить участие».")
                )
            user.dialog_state = "CHARACTER_EDIT"
            return BotResponse(
                text=(
                    f"Игра: «{event.name}»\nСтатус: {registration.attendance_status.value}\n\n"
                    f"Текущие пожелания:\n{registration.character_wish}\n\n"
                    "Отправьте новый вариант."
                ),
                buttons=[Button(label=NO_WISHES, value=NO_WISHES), Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)],
            )
        if flow == "cancel":
            user.dialog_state = "CANCEL_CONFIRM"
            return BotResponse(
                text=(
                    f"Вы уверены, что хотите отменить участие в игре «{event.name}»? "
                    "Если вы отмените участие, то сможете записаться снова, но попадёте "
                    "в конец очереди подтверждения"
                ),
                buttons=[
                    Button(label="Да, отменить", value="cancel:yes"),
                    Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                ],
            )
        return BotResponse(text="Некорректное действие.")

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
                        "Используйте текущую клавиатуру: нажмите «Пропустить» или "
                        "отправьте сообщением, с кем вы хотите играть."
                    ),
                    buttons=[
                        Button(label=SKIP, value="enlist:wish-play:skip"),
                        Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                    ],
                )
            user.dialog_context["wish_play"] = NO_CO_PLAYER_WISH if value == "enlist:wish-play:skip" else value[:2000]
            return await self._enqueue_enlist(user, message)
        if user.dialog_state == "ENLIST_CONFIRM":
            # Complete signups started before the confirmation step was removed.
            if value != "enlist:confirm":
                return BotResponse(text="Нажмите «Подтвердить» или «Отмена».")
            return await self._enqueue_enlist(user, message)
        raise AssertionError("unknown enlist state")

    async def _enqueue_enlist(self, user: User, message: InboundMessage) -> BotResponse:
        context = user.dialog_context.copy()
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
                display_name=user.full_name or "",
                wish_play=str(context["wish_play"]),
                larp_experience=user.larp_experience,
                crossplay=user.crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            ),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=(
                    f"🎲 Вы записаны на игру «{context['event_name']}».\n\n"
                    "Когда придёт время окончательно подтвердить участие, выберите "
                    "«✅ Подтвердить участие». Тогда бот попросит пожелания по персонажу."
                ),
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
        await self._clear(user)
        await self.registrations.enqueue(
            operation=operation,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=CharacterWishPayload(character_wish=wish),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=(
                    f"✅ Участие в игре «{context['event_name']}» подтверждено.\n\n"
                    f"Пожелания по персонажу:\n{wish}\n\n"
                    "Позже их можно изменить через «🎭 Пожелания по персонажу»."
                    if operation is Operation.CONFIRM
                    else f"🎭 Пожелания для игры «{context['event_name']}» обновлены."
                ),
                buttons=[main_menu_button()],
            ),
            idempotency_key=f"{message.update_id}:{operation.value}",
        )
        return BotResponse(text="⏳ Изменение принято в обработку.", deferred=True, command_enqueued=True)

    async def _cancel_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if value != "cancel:yes":
            return BotResponse(text="Подтвердите отмену кнопкой или выберите «Отмена».")
        context = user.dialog_context.copy()
        await self._clear(user)
        await self.registrations.enqueue(
            operation=Operation.CANCEL,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EmptyPayload(),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=f"❌ Участие в игре «{context['event_name']}» отменено.",
                buttons=[main_menu_button()],
            ),
            idempotency_key=f"{message.update_id}:CANCEL",
        )
        return BotResponse(text="⏳ Отмена принята в обработку.", deferred=True, command_enqueued=True)

    async def _admin_menu(self, user: User) -> BotResponse:
        if not await self._has_admin_access(user):
            return BotResponse(text="Недостаточно прав.")
        labels = [
            "➕ Создать игру",
            MANAGE_GAMES,
            BACK,
        ]
        if await self._is_admin(user):
            labels.insert(-1, GRANT_GAMEMASTER)
        return BotResponse(text="🛠 Администрирование", buttons=[Button(label=x, value=x) for x in labels])

    @staticmethod
    def _identity(user: User) -> BotIdentity:
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        return BotIdentity(platform=platform, platform_user_id=user_id(user))

    async def _sync_admin_leaders(self, event_id: str) -> None:
        admins = list(await self.admins.list_admins())
        if not admins:
            return
        current = {
            (identity.platform, identity.platform_user_id) for identity in await self.events.list_leaders(event_id)
        }
        await asyncio.gather(
            *(
                self.events.add_leader(event_id, identity)
                for identity in admins
                if (identity.platform, identity.platform_user_id) not in current
            )
        )

    async def _is_event_leader(self, user: User, event_id: str) -> bool:
        await self._sync_admin_leaders(event_id)
        return self._identity(user) in await self.events.list_leaders(event_id)

    async def _event_leader_required(self, user: User, event_id: str) -> BotResponse | None:
        if await self._is_event_leader(user, event_id):
            return None
        await self._clear(user)
        return BotResponse(text="Только ведущие этой игры могут изменять её данные.")

    async def _is_admin(self, user: User) -> bool:
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        return await self.admins.is_admin(platform, user_id(user))

    async def _has_admin_access(self, user: User) -> bool:
        if await self._is_admin(user):
            return True
        if user.is_gamemaster:
            return True
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        return await self.admins.is_gamemaster(platform, user_id(user))

    async def _admin_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._has_admin_access(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        event_bound_states = {
            "ADMIN_STATUS_SELECT",
            "ADMIN_CONFIRMATION_DEADLINE",
            "ADMIN_NOTIFICATION_TEXT",
            "ADMIN_ARCHIVE_NAME",
            "ADMIN_DELETE_NAME",
            "ADMIN_EVENT_LEADER_PLATFORM",
            "ADMIN_EVENT_LEADER_PROFILE",
            "ADMIN_REMOVE_PICK",
            "ADMIN_REMOVE_CONFIRM",
        }
        if user.dialog_state in event_bound_states:
            denied = await self._event_leader_required(user, str(user.dialog_context["event_id"]))
            if denied is not None:
                return denied
        if user.dialog_state == "ADMIN_GAMEMASTER_PLATFORM":
            if not await self._is_admin(user):
                await self._clear(user)
                return BotResponse(text="Недостаточно прав.")
            platforms = {
                "admin:gamemaster:telegram": Platform.TELEGRAM,
                "admin:gamemaster:vk": Platform.VK,
            }
            target_platform = platforms.get(value)
            if target_platform is None:
                return BotResponse(
                    text="Выберите бот, которым пользуется новый гейммастер:",
                    buttons=[
                        Button(label="Telegram", value="admin:gamemaster:telegram"),
                        Button(label="VK", value="admin:gamemaster:vk"),
                        Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                    ],
                )
            user.dialog_state = "ADMIN_GAMEMASTER_PROFILE"
            user.dialog_context = {"gamemaster_platform": target_platform.value}
            if target_platform is Platform.TELEGRAM:
                prompt = "Отправьте ссылку на профиль Telegram или @username будущего гейммастера:"
            else:
                prompt = "Отправьте ссылку на профиль VK будущего гейммастера:"
            return BotResponse(text=prompt, buttons=[Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)])
        if user.dialog_state == "ADMIN_GAMEMASTER_PROFILE":
            return await self._grant_gamemaster(user, message, value)
        if user.dialog_state == "ADMIN_EVENT_LEADER_PLATFORM":
            platforms = {
                "admin:event-leader:telegram": Platform.TELEGRAM,
                "admin:event-leader:vk": Platform.VK,
            }
            target_platform = platforms.get(value)
            if target_platform is None:
                return self._event_leader_platform_prompt(str(user.dialog_context["event_id"]))
            user.dialog_state = "ADMIN_EVENT_LEADER_PROFILE"
            user.dialog_context["gamemaster_platform"] = target_platform.value
            if target_platform is Platform.TELEGRAM:
                prompt = "Отправьте ссылку на профиль Telegram или @username гейммастера:"
            else:
                prompt = "Отправьте ссылку на профиль VK гейммастера:"
            return BotResponse(
                text=prompt,
                buttons=[game_management_button(str(user.dialog_context["event_id"]))],
            )
        if user.dialog_state == "ADMIN_EVENT_LEADER_PROFILE":
            return await self._add_event_leader(user, value)
        if user.dialog_state == "ADMIN_REMOVE_PICK":
            return await self._admin_remove_pick(user, message, value)
        if user.dialog_state == "ADMIN_REMOVE_CONFIRM":
            return await self._admin_remove_confirm(user, message, value)
        if user.dialog_state == "ADMIN_CREATE_NAME":
            leaders = [*(await self.admins.list_admins()), self._identity(user)]
            event = await self.administration.create_event(value, leaders)
            await self._clear(user)
            return BotResponse(
                text=(
                    f"✅ Игра создана.\n\nНазвание:\n{event.name}\n\n"
                    f"Статус: {STATUS_REGISTRATION}. Игроки уже могут записываться, "
                    "но пока не могут подтверждать участие."
                ),
                buttons=[game_management_button(event.event_id)],
            )
        if user.dialog_state == "ADMIN_STATUS_SELECT":
            choice = ADMIN_STATUS_CHOICES.get(value)
            if choice is None:
                return _admin_status_response(
                    str(user.dialog_context["event_name"]),
                    EventStatus(str(user.dialog_context["event_status"])),
                    str(user.dialog_context["event_id"]),
                )
            status_label, operation = choice
            if operation is Operation.OPEN_CONFIRMATION:
                user.dialog_state = "ADMIN_CONFIRMATION_DEADLINE"
                return _confirmation_deadline_response(str(user.dialog_context["event_id"]))
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=operation,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(
                    text_success=f"Статус игры «{context['event_name']}» изменён: {status_label}.",
                    buttons=[game_management_button(str(context["event_id"]))],
                ),
                idempotency_key=f"{message.update_id}:{operation.value}",
            )
            return BotResponse(
                text="⏳ Изменение Статуса принято в обработку.",
                buttons=[game_management_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state == "ADMIN_CONFIRMATION_DEADLINE":
            if value == "admin:deadline:nearest-thursday":
                deadline = closest_thursday_19()
            else:
                try:
                    deadline = parse_confirmation_deadline(value)
                except ValueError:
                    return _confirmation_deadline_response(str(user.dialog_context["event_id"]), invalid=True)
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.OPEN_CONFIRMATION,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=ConfirmationDeadlinePayload(deadline=deadline),
                reply_context=ReplyContext(
                    text_success=f"Статус игры «{context['event_name']}» изменён: {STATUS_CONFIRMATION}.",
                    buttons=[game_management_button(str(context["event_id"]))],
                ),
                idempotency_key=f"{message.update_id}:{Operation.OPEN_CONFIRMATION.value}",
            )
            return BotResponse(
                text=(
                    "⏳ Открытие подтверждения принято в обработку.\n\n"
                    f"Дедлайн: {format_confirmation_deadline(deadline)}"
                ),
                buttons=[game_management_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state == "ADMIN_NOTIFICATION_TEXT":
            event_name = str(user.dialog_context["event_name"])
            event_id = str(user.dialog_context["event_id"])
            if message.callback is not None:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    invalid="Отправьте уведомление обычным текстовым сообщением.",
                )
            if not value:
                return _confirmed_notification_prompt(
                    event_name, event_id, invalid="Текст уведомления не может быть пустым."
                )
            if len(value) > 4000:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    invalid="Текст уведомления не должен превышать 4000 символов.",
                )
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.SEND_CONFIRMED_NOTIFICATION,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=NotificationPayload(text=value),
                reply_context=ReplyContext(buttons=[game_management_button(str(context["event_id"]))]),
                idempotency_key=f"{message.update_id}:{Operation.SEND_CONFIRMED_NOTIFICATION.value}",
            )
            return BotResponse(
                text="⏳ Уведомление принято в обработку.",
                buttons=[game_management_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state in {"ADMIN_ARCHIVE_NAME", "ADMIN_DELETE_NAME"}:
            expected = str(user.dialog_context["event_name"])
            if value.strip() != expected:
                return BotResponse(
                    text=f"Название не совпало. Введите точно:\n\n{expected}",
                    buttons=[game_management_button(str(user.dialog_context["event_id"]))],
                )
            event_id = str(user.dialog_context["event_id"])
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.ARCHIVE_EVENT,
                event_id=event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(
                    text_success=(
                        f"📦 Игра «{expected}» архивирована. Игра, записи и XLSX-таблицы сохранены в постоянном списке."
                    ),
                    buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                ),
                idempotency_key=f"{message.update_id}:ARCHIVE_EVENT",
            )
            return BotResponse(
                text="⏳ Архивация принята в обработку.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                deferred=True,
                command_enqueued=True,
            )
        await self._clear(user)
        return BotResponse(text="Административный диалог сброшен.")

    async def _grant_gamemaster(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._is_admin(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        target_platform = Platform(str(user.dialog_context["gamemaster_platform"]))
        target: User | None
        try:
            if target_platform is Platform.TELEGRAM:
                handle = normalize_telegram_profile(value)
                target = await self.users.find_telegram_by_handle(handle)
            else:
                normalized = normalize_vk_url(value)
                if self.vk_user_ids is None:
                    raise RuntimeError("VK user ID resolver is not configured")
                target_id = await self.vk_user_ids.resolve_user_id(normalized)
                target = None if target_id is None else await self.users.get(Platform.VK, target_id)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:", buttons=[Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)]
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)],
            )
        if not target.profile_complete:
            return BotResponse(
                text="Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите.",
                buttons=[Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)],
            )
        target_id = user_id(target)
        request_id = f"gamemaster-grant:{target_platform.value}:{target_id}:{message.update_id}"
        configured_gamemaster = await self.admins.is_gamemaster(target_platform, target_id)
        if configured_gamemaster or (target.is_gamemaster and target.gamemaster_grant_operation_id != request_id):
            await self._clear(user)
            return BotResponse(text="Этот пользователь уже является гейммастером.")

        if not target.is_gamemaster:
            await self.users.grant_gamemaster(target_platform, target_id, request_id)
            refreshed = await self.users.get(target_platform, target_id)
            if refreshed is None or not refreshed.is_gamemaster:
                raise RuntimeError("gamemaster grant was not persisted")
            target = refreshed
        if target.gamemaster_grant_operation_id != request_id:
            await self._clear(user)
            return BotResponse(text="Этот пользователь уже является гейммастером.")
        if target.last_delivery_operation_id != request_id:
            if self.transport is None:
                raise RuntimeError("deferred transport is not configured")
            await self.transport.send(
                platform=target_platform,
                user_id=target_id,
                request_id=request_id,
                text=GAMEMASTER_NOTIFICATION,
            )
            await self.users.claim_delivery(target_platform, target_id, request_id)

        if message.identity.platform is target_platform and message.identity.platform_user_id == target_id:
            user.is_gamemaster = True
            user.gamemaster_grant_operation_id = request_id
            user.last_delivery_operation_id = request_id
        await self._clear(user)
        platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
        return BotResponse(text=f"✅ Пользователь назначен гейммастером в {platform_name}.")

    @staticmethod
    def _event_leader_platform_prompt(event_id: str) -> BotResponse:
        return BotResponse(
            text="Выберите бот, которым пользуется добавляемый гейммастер:",
            buttons=[
                Button(label="Telegram", value="admin:event-leader:telegram"),
                Button(label="VK", value="admin:event-leader:vk"),
                game_management_button(event_id),
            ],
        )

    async def _add_event_leader(self, user: User, value: str) -> BotResponse:
        target_platform = Platform(str(user.dialog_context["gamemaster_platform"]))
        target: User | None
        try:
            if target_platform is Platform.TELEGRAM:
                handle = normalize_telegram_profile(value)
                target = await self.users.find_telegram_by_handle(handle)
            else:
                normalized = normalize_vk_url(value)
                if self.vk_user_ids is None:
                    raise RuntimeError("VK user ID resolver is not configured")
                target_id = await self.vk_user_ids.resolve_user_id(normalized)
                target = None if target_id is None else await self.users.get(Platform.VK, target_id)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:",
                buttons=[game_management_button(str(user.dialog_context["event_id"]))],
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[game_management_button(str(user.dialog_context["event_id"]))],
            )
        if not target.profile_complete:
            return BotResponse(
                text="Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите.",
                buttons=[game_management_button(str(user.dialog_context["event_id"]))],
            )

        event_id = str(user.dialog_context["event_id"])
        event_name = str(user.dialog_context["event_name"])
        identity = BotIdentity(platform=target_platform, platform_user_id=user_id(target))
        if identity in await self.events.list_leaders(event_id):
            await self._clear(user)
            return BotResponse(
                text=f"Этот пользователь уже является ведущим игры «{event_name}».",
                buttons=[game_management_button(event_id)],
            )
        if not target.is_gamemaster and not await self.admins.is_gamemaster(target_platform, identity.platform_user_id):
            return BotResponse(
                text="Добавить ведущим можно только гейммастера.",
                buttons=[game_management_button(event_id)],
            )

        await self.events.add_leader(event_id, identity)
        if identity not in await self.events.list_leaders(event_id):
            raise RuntimeError("event leader grant was not persisted")
        await self._clear(user)
        return BotResponse(
            text=f"✅ Гейммастер добавлен ведущим игры «{event_name}».",
            buttons=[game_management_button(event_id)],
        )

    @staticmethod
    def _admin_game_cursor(direction: str, event: Event) -> str:
        micros = int(event.created_at.timestamp() * 1_000_000)
        return f"page:a:{direction}:{micros}:{event.event_id}"

    @staticmethod
    def _stored_registration_cursor(user: User, name: str) -> tuple[datetime, str]:
        raw = user.dialog_context[name]
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError("invalid registration-page cursor")
        return datetime.fromisoformat(str(raw[0])), str(raw[1])

    async def _show_removable_players(
        self,
        user: User,
        event: Event,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        invalid: str = "",
    ) -> BotResponse:
        page = await self.administration.active_registration_page(
            event.event_id,
            after=after,
            before=before,
        )
        user.dialog_state = "ADMIN_REMOVE_PICK"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
        }
        if not page.rows:
            return BotResponse(
                text=(
                    f"В игре «{event.name}» нет записавшихся или подтвердивших участие игроков."
                ),
                buttons=[game_management_button(event.event_id)],
            )

        choices: dict[str, str] = {}
        buttons: list[Button] = []
        for registration in page.rows:
            token = token_urlsafe(8)
            choices[token] = registration.participant_key
            buttons.append(
                Button(
                    label=f"{registration.display_name} — {registration.attendance_status.value}",
                    value=f"admin:remove:pick:{token}",
                )
            )
        page_token = token_urlsafe(8)
        if page.has_previous:
            buttons.append(Button(label="⬅️ Предыдущая", value=f"admin:remove:page:p:{page_token}"))
        if page.has_next:
            buttons.append(Button(label="➡️ Следующая", value=f"admin:remove:page:n:{page_token}"))
        buttons.append(game_management_button(event.event_id))
        user.dialog_context.update(
            {
                "remove_choices": choices,
                "remove_page_token": page_token,
                "remove_first": [page.rows[0].created_at.isoformat(), page.rows[0].participant_key],
                "remove_last": [page.rows[-1].created_at.isoformat(), page.rows[-1].participant_key],
            }
        )
        prefix = f"{invalid}\n\n" if invalid else ""
        return BotResponse(
            text=(
                f"{prefix}Удаление игрока из игры «{event.name}»\n\n"
                "Выберите игрока. Показаны записавшиеся и подтвердившие участие; "
                "на кнопках указаны имена из их профилей."
            ),
            buttons=buttons,
        )

    @staticmethod
    def _remove_confirmation_response(user: User, *, invalid: str = "") -> BotResponse:
        event_id = str(user.dialog_context["event_id"])
        token = str(user.dialog_context["remove_selected_token"])
        name = str(user.dialog_context["remove_selected_name"])
        status = str(user.dialog_context["remove_selected_status"])
        prefix = f"{invalid}\n\n" if invalid else ""
        return BotResponse(
            text=(
                f"{prefix}⚠️ Это действие нельзя отменить.\n\n"
                "Вы уверены, что выбрали нужного человека?\n\n"
                f"Имя из профиля: {name}\n"
                f"Статус: {status}"
            ),
            buttons=[
                Button(label="Да, удалить", value=f"admin:remove:confirm:{token}"),
                Button(label="Нет, вернуться к списку", value=f"ag:remove:{event_id}"),
            ],
        )

    async def _admin_remove_pick(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        del message
        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
        parts = value.split(":")
        if len(parts) == 5 and parts[:3] == ["admin", "remove", "page"]:
            direction, page_token = parts[3:]
            if page_token != user.dialog_context.get("remove_page_token") or direction not in {"n", "p"}:
                return await self._show_removable_players(user, event, invalid="Эта страница устарела.")
            if direction == "p":
                return await self._show_removable_players(
                    user,
                    event,
                    before=self._stored_registration_cursor(user, "remove_first"),
                )
            return await self._show_removable_players(
                user,
                event,
                after=self._stored_registration_cursor(user, "remove_last"),
            )
        if len(parts) != 4 or parts[:3] != ["admin", "remove", "pick"]:
            return await self._show_removable_players(user, event, invalid="Выберите игрока кнопкой из списка.")
        token = parts[3]
        choices = user.dialog_context.get("remove_choices")
        participant_key = choices.get(token) if isinstance(choices, dict) else None
        if not isinstance(participant_key, str):
            return await self._show_removable_players(user, event, invalid="Этот список устарел.")
        registration = await self.administration.get_registration(event_id, participant_key)
        if registration is None or registration.attendance_status is AttendanceStatus.CANCELLED:
            return await self._show_removable_players(
                user,
                event,
                invalid="Запись этого игрока уже недоступна для удаления.",
            )
        user.dialog_state = "ADMIN_REMOVE_CONFIRM"
        user.dialog_context.update(
            {
                "remove_selected_key": participant_key,
                "remove_selected_token": token,
                "remove_selected_name": registration.display_name,
                "remove_selected_status": registration.attendance_status.value,
            }
        )
        return self._remove_confirmation_response(user)

    async def _admin_remove_confirm(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        token = str(user.dialog_context["remove_selected_token"])
        event_id = str(user.dialog_context["event_id"])
        if value == f"ag:remove:{event_id}":
            event = await self.events.get(event_id)
            if event is None:
                await self._clear(user)
                return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
            return await self._show_removable_players(user, event)
        if value != f"admin:remove:confirm:{token}":
            return self._remove_confirmation_response(user, invalid="Удаление не подтверждено.")
        context = user.dialog_context.copy()
        participant_key = str(context["remove_selected_key"])
        registration = await self.administration.get_registration(event_id, participant_key)
        if registration is None or registration.attendance_status is AttendanceStatus.CANCELLED:
            event = await self.events.get(event_id)
            if event is None:
                await self._clear(user)
                return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
            return await self._show_removable_players(
                user,
                event,
                invalid="Запись этого игрока уже недоступна для удаления.",
            )
        await self._clear(user)
        await self.registrations.enqueue(
            operation=Operation.REMOVE_PARTICIPANT,
            event_id=event_id,
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EmptyPayload(),
            reply_context=ReplyContext(
                text_success=(
                    f"🗑 Игрок «{registration.display_name}» удалён из игры «{context['event_name']}»."
                ),
                buttons=[game_management_button(event_id)],
            ),
            idempotency_key=f"{message.update_id}:{Operation.REMOVE_PARTICIPANT.value}",
            target_participant_key=participant_key,
        )
        return BotResponse(
            text="⏳ Удаление принято в обработку.",
            buttons=[game_management_button(event_id)],
            deferred=True,
            command_enqueued=True,
        )

    async def _show_admin_games(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> BotResponse:
        events = list(
            await self.events.list_page(
                after=after,
                before=before,
                archived=False,
                limit=10,
            )
        )
        if not events:
            return BotResponse(
                text="Неархивированных игр нет.",
                buttons=[Button(label=BACK, value=ADMIN_MENU)],
            )

        first, last = events[0], events[-1]
        previous, following = await asyncio.gather(
            self.events.list_page(
                before=(first.created_at, first.event_id),
                archived=False,
                limit=1,
            ),
            self.events.list_page(
                after=(last.created_at, last.event_id),
                archived=False,
                limit=1,
            ),
        )
        buttons = [Button(label=event.name, value=f"ag:manage:{event.event_id}") for event in events]
        if previous:
            buttons.append(Button(label="⬅️ Предыдущая", value=self._admin_game_cursor("p", first)))
        if following:
            buttons.append(Button(label="➡️ Следующая", value=self._admin_game_cursor("n", last)))
        buttons.append(Button(label=BACK, value=ADMIN_MENU))
        return BotResponse(text="Управление играми\n\nВыберите игру:", buttons=buttons)

    async def _game_management(self, user: User, event: Event) -> BotResponse:
        if event.archived_at is not None:
            return BotResponse(
                text=f"Игра «{event.name}» уже архивирована.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )
        is_leader = await self._is_event_leader(user, event.event_id)
        text = f"Управление игрой «{event.name}»\n\nСтатус: {EVENT_STATUS_LABELS[event.status]}"
        if event.confirmation_deadline is not None:
            text += f"\nДедлайн подтверждения: {format_confirmation_deadline(event.confirmation_deadline)}"
        if not is_leader:
            return BotResponse(
                text=text + "\n\nАдминистративные действия доступны только ведущим этой игры.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )
        actions = [
            (CHANGE_STATUS, "status"),
            (SEND_CONFIRMATION_REMINDER, "remind"),
            (SEND_CONFIRMED_NOTIFICATION, "notify"),
            (EVENT_TABLES, "tables"),
            (REMOVE_PLAYER, "remove"),
            (ADD_EVENT_LEADER, "leader"),
            (ARCHIVE_GAME, "archive"),
        ]
        buttons = [Button(label=label, value=f"ag:{action}:{event.event_id}") for label, action in actions]
        buttons.append(Button(label=BACK, value=MANAGE_GAMES))
        return BotResponse(text=text + "\n\nВыберите действие:", buttons=buttons)

    async def _admin_game_action(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse:
        try:
            _, action, event_id = value.split(":", 2)
        except ValueError:
            return BotResponse(text="Некорректное действие.")
        if not await self._has_admin_access(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        event = await self.events.get(event_id)
        if event is None:
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
        if action == "manage":
            return await self._game_management(user, event)
        if event.archived_at is not None:
            return BotResponse(
                text=f"Игра «{event.name}» уже архивирована.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )
        denied = await self._event_leader_required(user, event.event_id)
        if denied is not None:
            denied.buttons = [Button(label=BACK, value=MANAGE_GAMES)]
            return denied

        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "event_status": event.status.value,
        }
        if action == "status":
            user.dialog_state = "ADMIN_STATUS_SELECT"
            return _admin_status_response(event.name, event.status, event.event_id)
        if action == "remind":
            await self._clear(user)
            if event.status is not EventStatus.CONFIRMATION_OPEN:
                return BotResponse(
                    text=f"Подтверждение участия для игры «{event.name}» не открыто.",
                    buttons=[game_management_button(event.event_id)],
                )
            if event.confirmation_deadline is None:
                return BotResponse(
                    text=(
                        f"Для игры «{event.name}» не задан дедлайн. "
                        "Снова откройте статус подтверждения и укажите дедлайн."
                    ),
                    buttons=[game_management_button(event.event_id)],
                )
            back = game_management_button(event.event_id)
            await self.registrations.enqueue(
                operation=Operation.SEND_CONFIRMATION_REMINDER,
                event_id=event.event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(buttons=[back]),
                idempotency_key=f"{message.update_id}:{Operation.SEND_CONFIRMATION_REMINDER.value}",
            )
            return BotResponse(
                text="⏳ Напоминание принято в обработку.",
                buttons=[back],
                deferred=True,
                command_enqueued=True,
            )
        if action == "notify":
            user.dialog_state = "ADMIN_NOTIFICATION_TEXT"
            return _confirmed_notification_prompt(event.name, event.event_id)
        if action == "tables":
            await self._clear(user)
            event = await self.administration.ensure_event_tables(event)
            await self.administration.create_pass_table(event.event_id)
            refreshed = await self.events.get(event.event_id)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
            return BotResponse(
                text=f"Таблицы игры «{event.name}»\n\n{event_table_links(refreshed)}",
                buttons=[game_management_button(event.event_id)],
            )
        if action == "remove":
            return await self._show_removable_players(user, event)
        if action == "leader":
            user.dialog_state = "ADMIN_EVENT_LEADER_PLATFORM"
            return self._event_leader_platform_prompt(event.event_id)
        if action == "archive":
            user.dialog_state = "ADMIN_ARCHIVE_NAME"
            return BotResponse(
                text=(
                    "Игра будет закрыта для новых регистраций и скрыта из управления. "
                    "Таблицы регистрации и пропусков и все записи будут сохранены.\n\n"
                    f"Для подтверждения введите точное название:\n\n{event.name}"
                ),
                buttons=[game_management_button(event.event_id)],
            )
        return await self._game_management(user, event)

    async def _admin_select(
        self,
        user: User,
        message: InboundMessage,
        event: Event,
        flow: str,
    ) -> BotResponse:
        action = {
            "admin-status": "status",
            "admin-reminder": "remind",
            "admin-notification": "notify",
            "admin-pass-create": "tables",
            "admin-leader-add": "leader",
            "admin-archive": "archive",
            "admin-delete": "archive",
        }.get(flow, "manage")
        return await self._admin_game_action(user, message, f"ag:{action}:{event.event_id}")

    async def _admin_start(self, user: User, value: str) -> BotResponse:
        if not await self._has_admin_access(user):
            return BotResponse(text="Недостаточно прав.")
        if value == "➕ Создать игру":
            user.dialog_state = "ADMIN_CREATE_NAME"
            return BotResponse(text="Введите название игры:")
        if value == MANAGE_GAMES or value in {
            CHANGE_STATUS,
            SEND_CONFIRMATION_REMINDER,
            SEND_CONFIRMED_NOTIFICATION,
            CREATE_PASS_TABLE,
            LIST_PASS_TABLES,
            EVENT_TABLES,
            REMOVE_PLAYER,
            ADD_EVENT_LEADER,
            ARCHIVE_GAME,
            LEGACY_DELETE_GAME,
            "📋 Список игр",
            *LEGACY_STATUS_ACTIONS,
        }:
            return await self._show_admin_games()
        if value == GRANT_GAMEMASTER:
            if not await self._is_admin(user):
                return BotResponse(text="Недостаточно прав.")
            user.dialog_state = "ADMIN_GAMEMASTER_PLATFORM"
            user.dialog_context = {}
            return BotResponse(
                text="Выберите бот, которым пользуется новый гейммастер:",
                buttons=[
                    Button(label="Telegram", value="admin:gamemaster:telegram"),
                    Button(label="VK", value="admin:gamemaster:vk"),
                    Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                ],
            )
        return await self._admin_menu(user)

    # Admin submenu commands arrive while the user is IDLE, so extend the root dispatcher.
    async def dispatch_idle_admin(self, user: User, value: str) -> BotResponse | None:
        if value in ADMIN_ACTIONS or value in LEGACY_STATUS_ACTIONS:
            return await self._admin_start(user, value)
        return None
