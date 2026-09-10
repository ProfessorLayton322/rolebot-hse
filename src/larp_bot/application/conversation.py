from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from secrets import token_urlsafe
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from larp_bot.domain.models import (
    MAX_PLAYER_AMOUNT,
    AttendanceStatus,
    BotIdentity,
    BotResponse,
    Button,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    CoPlayerWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    PassDetails,
    Platform,
    PlayerAmountPayload,
    ProfileSyncPayload,
    Registration,
    ReplyContext,
    StatisticsPayload,
    TelegramUser,
    User,
    VkUser,
    event_reference,
    format_game_date,
    normalize_mobile_phone,
    normalize_telegram_handle,
    normalize_telegram_profile,
    normalize_vk_url,
    parse_full_name,
    parse_game_date,
    surname_and_name,
    validate_pass_email,
)

from .deadlines import (
    NEAREST_THURSDAY,
    closest_thursday_19,
    format_confirmation_deadline,
    parse_confirmation_deadline,
)
from .navigation import ADMIN_MENU, MAIN_MENU, MAIN_MENU_LABEL, admin_menu_button, main_menu_button
from .ports import (
    AdminConfigProvider,
    DeferredTransport,
    EventRepository,
    UserRepository,
    VkUserIdResolver,
    new_user,
)
from .services import (
    EventAdministrationService,
    RegistrationService,
    closed_game_cancellation_message,
    confirmed_notification_text,
    is_plain_chat_link,
    same_day_signup_warning,
)

PROFILE = "📝 Профиль"
ENLIST = "🎮 Записаться на игру"
CONFIRM = "✅ Подтвердить участие"
# Keep the former value recognizable for keyboards sent before this rollout.
CHARACTER = "🎭 Пожелания по персонажу"
GAME_WISHES = "🎭 Пожелания по игре"
CHARACTER_WISHES = "Пожелания по персонажу"
CO_PLAYER_WISHES = "Пожелания по соигрокам"
CO_PLAYER_WISH_SKIP = "wishes:co-player:skip"
CANCEL = "❌ Отменить участие"
ADMIN = "🛠 Администрирование"
BACK = "⬅️ Назад"
CANCEL_DIALOG = "Отмена"
PAGE_BACK = "Предыдущая страница"
PAGE_FORWARD = "Вперёд"
PAGE_SIZE = 5
NO_WISHES = "Без пожеланий"
KEEP = "Оставить без изменений"
SKIP = "Пропустить"
NO_CO_PLAYER_WISH = "Без пожеланий"
CHANGE_STATUS = "🔄 Изменить статус"
CHANGE_PLAYER_AMOUNT = "👥 Изменить количество игроков"
SEND_CONFIRMATION_REMINDER = "🔔 Напомнить о подтверждении"
MANAGE_NOTIFICATIONS = "Уведомления"
SEND_CONFIRMED_LINK = "Отправить подтвержденным ссылку на чат"
SEND_CONFIRMED_NOTIFICATION = "Отправить подтвержденным сообщение"
LEGACY_SEND_CONFIRMED_NOTIFICATION = "📣 Уведомить подтвердивших"
LIST_WAITING_PLAYERS = "👥 Не подтвердившие участие"
REMOVE_PLAYER = "🗑 Удалить игрока"
MANAGE_PLAYERS = "Управление игроками"
REMOVE_WAITING_PLAYERS = "Удалить не подтвердившихся"
CREATE_PASS_TABLE = "🎫 Создать таблицу пропусков"
LIST_PASS_TABLES = "🔗 Ссылки на таблицы пропусков"
MANAGE_GAMES = "Управление играми"
ARCHIVED_GAMES = "Архив игр"
MANAGE_MODERATORS = "Управление модераторами"
MANAGE_STATISTICS = "Управление статистикой"
STATS_LINKS = "Ссылки на таблицы статистики"
STATS_ADD_GAME = "Добавить игру в статистику"
EVENT_TABLES = "Таблицы участников и пропусков"
GRANT_GAMEMASTER = "➕ Назначить модератора"
REVOKE_GAMEMASTER = "➖ Снять модератора"
LIST_GAMEMASTERS = "📋 Список модераторов"
MANAGE_MASTERS = "Мастера"
ADD_EVENT_LEADER = "➕ Добавить мастера"
ADD_SELF_EVENT_LEADER = "➕ Добавить себя мастером"
REMOVE_EVENT_LEADER = "➖ Удалить мастера"
LIST_EVENT_LEADERS = "📋 Список мастеров"
ARCHIVE_GAME = "📦 Архивировать игру"
UNARCHIVE_GAME = "📤 Разархивировать игру"
LEGACY_DELETE_GAME = "🗑 Удалить игру"
STATUS_REGISTRATION = "Регистрация"
STATUS_CONFIRMATION = "Подтверждение"
STATUS_CLOSED = "Закрытие регистрации"
GAMEMASTER_NOTIFICATION = "Вы назначены модератором! 🎉"
GAMEMASTER_REVOKED_NOTIFICATION = "Вы больше не являетесь модератором."
UNSUPPORTED_CONTENT_RESPONSE = "Пока я понимаю только текст и кнопки. Отправьте ответ текстовым сообщением."
INACTIVE_GAME_RESPONSE = "Игра больше не активна; запись и изменение данных недоступны."
INVALID_INPUT_RESPONSE = "Не удалось обработать ввод. Проверьте ответ и попробуйте ещё раз."
MAX_CO_PLAYER_WISH_LENGTH = 2_000
MAX_CHARACTER_WISH_LENGTH = 3_000
WELCOME_TEXT = (
    "Привет! Этот бот поможет зарегистрироваться на кабинетные ролевые игры.\n\n"
    "Сначала заполните профиль и запишитесь на игру. Мы сообщим вам, когда будет открыто "
    "подтверждение на игру — вы сможете подтвердиться, когда будете уверены, что сможете прийти на игру."
)

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
MASTER_TABLE_DISCLAIMER = "⚠️ Только для мастеров этой игры. Не пересылайте эту ссылку игрокам!"
# Keep buttons sent immediately before this rollout useful; they now open the
# unrestricted status picker instead of applying their former transition.
LEGACY_STATUS_ACTIONS = frozenset({"🔓 Открыть подтверждения", "🔒 Закрыть подтверждения", "🔒 Закрыть регистрацию"})
ADMIN_ACTIONS = frozenset(
    {
        MANAGE_STATISTICS,
        STATS_LINKS,
        STATS_ADD_GAME,
        "➕ Создать игру",
        MANAGE_GAMES,
        ARCHIVED_GAMES,
        EVENT_TABLES,
        CHANGE_STATUS,
        CHANGE_PLAYER_AMOUNT,
        MANAGE_NOTIFICATIONS,
        SEND_CONFIRMATION_REMINDER,
        SEND_CONFIRMED_LINK,
        SEND_CONFIRMED_NOTIFICATION,
        LEGACY_SEND_CONFIRMED_NOTIFICATION,
        LIST_WAITING_PLAYERS,
        REMOVE_PLAYER,
        MANAGE_PLAYERS,
        REMOVE_WAITING_PLAYERS,
        CREATE_PASS_TABLE,
        LIST_PASS_TABLES,
        MANAGE_MODERATORS,
        GRANT_GAMEMASTER,
        REVOKE_GAMEMASTER,
        LIST_GAMEMASTERS,
        MANAGE_MASTERS,
        ADD_EVENT_LEADER,
        REMOVE_EVENT_LEADER,
        LIST_EVENT_LEADERS,
        ARCHIVE_GAME,
        UNARCHIVE_GAME,
        LEGACY_DELETE_GAME,
        "📋 Список игр",
    }
)
FREE_TEXT_DIALOG_STATES = frozenset(
    {
        "PROFILE_FULL_NAME_CYRILLIC",
        "PROFILE_CONTACT",
        "PROFILE_PASS_FULL_NAME_LATIN",
        "PROFILE_PASS_PHONE",
        "PROFILE_PASS_EMAIL_ADDRESS",
        "ENLIST_WISH_PLAY",
        "CONFIRM_WISH",
        "CHARACTER_EDIT",
        "CO_PLAYER_EDIT",
        "ADMIN_CREATE_NAME",
        "ADMIN_CREATE_DATE",
        "ADMIN_CREATE_PLAYER_AMOUNT",
        "ADMIN_UPDATE_PLAYER_AMOUNT",
        "ADMIN_CONFIRMATION_DEADLINE",
        "ADMIN_NOTIFICATION_LINK_TEXT",
        "ADMIN_NOTIFICATION_MESSAGE_TEXT",
        "ADMIN_NOTIFICATION_CONFIRM",
        "ADMIN_NOTIFICATION_TEXT",
        "ADMIN_GAMEMASTER_PROFILE",
        "ADMIN_EVENT_LEADER_PROFILE",
        "ADMIN_MODERATOR_LIST",
        "ADMIN_MODERATOR_REMOVE",
        "ADMIN_MODERATOR_REMOVE_CONFIRM",
        "ADMIN_EVENT_LEADER_ADD_PICK",
        "ADMIN_EVENT_LEADER_REMOVE",
        "ADMIN_EVENT_LEADER_REMOVE_CONFIRM",
        "ADMIN_EVENT_LEADER_LIST",
        "ADMIN_ARCHIVE_NAME",
        "ADMIN_DELETE_NAME",
        "ADMIN_REMOVE_PICK",
        "ADMIN_REMOVE_CONFIRM",
        "ADMIN_REMOVE_WAITING_CONFIRM",
        "ADMIN_WAITING_PICK",
    }
)

PROFILE_FORMAT = "combined-full-name-v2-statistics-experience"
CYRILLIC_FULL_NAME_PROMPT = "Введите ФИО кириллицей (фамилию имя, если отчества нету):"
LATIN_FULL_NAME_PROMPT = "Введите ФИО латиницей (фамилию имя, если отчества нету):"
CYRILLIC_FULL_NAME_RETRY_PROMPT = "Введите ФИО кириллицей, либо фамилию имя, если отчества нету"
LATIN_FULL_NAME_RETRY_PROMPT = "Введите ФИО латиницей, либо фамилию имя, если отчества нету"
PHONE_PROMPT = "Введите ваш номер телефона. Он необходим для оформления пропуска."
EMAIL_PROMPT = "Введите ваш email. Он необходим для оформления пропуска."


def dialog_exit_button(*, label: str = MAIN_MENU_LABEL) -> Button:
    return Button(label=label, value=CANCEL_DIALOG)


def yes_no_buttons() -> list[Button]:
    return [Button(label="Да", value="Да"), Button(label="Нет", value="Нет"), dialog_exit_button()]


def main_buttons(has_admin_access: bool) -> list[Button]:
    labels = [PROFILE, ENLIST, CONFIRM, GAME_WISHES, CANCEL]
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


def _attendance_status_text(status: AttendanceStatus) -> str:
    if status is AttendanceStatus.WAITING:
        return "Ожидает подтверждения"
    return status.value


def _player_game_picker_text(flow: str) -> str:
    return {
        "enlist": "🎮 Выберите игру, на которую хотите записаться:",
        "confirm": "✅ Выберите игру, участие в которой хотите подтвердить:",
        "character": "🎭 Выберите игру, для которой хотите изменить пожелания:",
        "wishes": "🎭 Выберите игру, для которой хотите изменить пожелания:",
        "cancel": "❌ Выберите игру, участие в которой хотите отменить:",
    }[flow]


def _no_games_for_action_text(flow: str) -> str:
    return {
        "enlist": "Сейчас нет игр, доступных для записи.",
        "confirm": "Сейчас нет игр, доступных для подтверждения участия.",
        "character": "Сейчас нет игр, доступных для изменения пожеланий.",
        "wishes": "Нет игр, на которые вы записаны.",
        "cancel": "Сейчас нет игр, доступных для отмены участия.",
    }[flow]


def _event_buttons(events: list[Event], flow: str) -> list[Button]:
    return [Button(label=event.name, value=f"select:{flow}:{event.event_id}") for event in events]


def co_player_wish_prompt(*, skip_value: str, same_day_warning_date: date | None = None) -> BotResponse:
    warning = f"{same_day_signup_warning(same_day_warning_date)}\n\n" if same_day_warning_date is not None else ""
    return BotResponse(
        text=(
            f"{warning}"
            "Есть ли игроки, с которыми вы хотели бы играть в одной сессии? "
            "Укажите каждого как «Фамилия Имя», через запятую. "
            "Мастера постараются учесть пожелание, но не могут его гарантировать.\n\n"
            f"Ответ должен быть не длиннее {MAX_CO_PLAYER_WISH_LENGTH} символов. "
            "Если пожеланий нет, нажмите «Пропустить»."
        ),
        buttons=[Button(label=SKIP, value=skip_value), dialog_exit_button()],
    )


def confirmation_replacement_prompt(event: Event, replaced_event: Event) -> BotResponse:
    if event.game_date is None:
        raise ValueError("same-day confirmation requires a game date")
    return BotResponse(
        text=(
            f"Вы уже подтвердили своё участие в игре «{replaced_event.name}», которая проводится "
            f"{format_game_date(event.game_date)}. Чтобы подтвердить своё участие в «{event.name}» "
            f"вам придётся отменить своё участие в «{replaced_event.name}». "
            "Вы уверены, что хотите это сделать?"
        ),
        buttons=[
            Button(label="Да, я хочу участвовать в другой игре", value="confirm:replace:yes"),
            dialog_exit_button(label="Нет, не хочу ничего менять"),
        ],
    )


def _pagination_buttons(
    *,
    previous: str | None,
    following: str | None,
    cancel: str,
) -> list[Button]:
    buttons: list[Button] = []
    if previous is not None:
        buttons.append(Button(label=PAGE_BACK, value=previous))
    if following is not None:
        buttons.append(Button(label=PAGE_FORWARD, value=following))
    buttons.append(Button(label=MAIN_MENU_LABEL if cancel == CANCEL_DIALOG else CANCEL_DIALOG, value=cancel))
    return buttons


def game_management_button(event_id: str) -> Button:
    return Button(label=BACK, value=f"ag:manage:{event_id}")


def player_management_button(event_id: str) -> Button:
    return Button(label=BACK, value=f"ag:players:{event_id}")


def notification_management_button(event_id: str) -> Button:
    return Button(label=BACK, value=f"ag:notifications:{event_id}")


def _admin_status_response(
    event_name: str,
    current_status: EventStatus,
    event_id: str,
    game_date: date | None = None,
) -> BotResponse:
    return BotResponse(
        text=(
            f"Игра: {event_reference(event_name, game_date)}\n"
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


def _confirmed_notification_prompt(
    event_name: str,
    event_id: str,
    *,
    link: bool,
    game_date: date | None = None,
    invalid: str = "",
) -> BotResponse:
    prefix = f"{invalid}\n\n" if invalid else ""
    reference = event_reference(event_name, game_date)
    if link:
        prompt = (
            f"Отправьте ссылку-приглашение в чат игры {reference}.\n\n"
            "Сообщение должно состоять только из корректной ссылки на чат Telegram или VK."
        )
    else:
        prompt = (
            "Эта кнопка нужна для рассылки оргинформации всем подтвердившимся на игру игрокам, "
            "включая подтвердившихся в запас.\n\n"
            f"Отправьте текст сообщения для подтвердившихся на игру {reference}."
        )
    return BotResponse(
        text=f"{prefix}{prompt}",
        buttons=[notification_management_button(event_id)],
    )


def _confirmed_notification_preview(
    event_name: str,
    event_id: str,
    message: str,
    recipient_count: int,
    game_date: date | None = None,
) -> BotResponse:
    return BotResponse(
        text=(
            f"Получателей: {recipient_count}\n\n"
            "Итоговое сообщение:\n\n"
            f"{confirmed_notification_text(event_name, message, game_date)}"
        ),
        buttons=[
            Button(label=f"✅ Отправить ({recipient_count})", value="admin:notification:confirm"),
            notification_management_button(event_id),
        ],
    )


def _normalize_vk_telegram_contact(value: str) -> str | None:
    """Parse an optional Telegram profile supplied through the VK questionnaire."""

    try:
        return normalize_telegram_profile(value)
    except ValueError:
        try:
            return normalize_telegram_handle(value)
        except ValueError:
            return None


def _context_event_reference(context: dict[str, object]) -> str:
    raw_date = context.get("game_date")
    game_date = date.fromisoformat(str(raw_date)) if raw_date is not None else None
    return event_reference(str(context["event_name"]), game_date)


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
            or value.startswith(
                ("ag:", "mod:", "master:", "stats:", "select:admin-", "page:admin-", "page:a:", "page:r:")
            )
        )
        current_button_values = {button.value for button in user.last_bot_buttons}
        if value == "/start" or (platform is Platform.VK and value == "Начать"):
            await self._clear(user)
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
            await self._clear(user)
            response = await self._main(user, "Действие отменено.")
        elif value in {ADMIN_MENU, MANAGE_MODERATORS}:
            await self._clear(user)
            response = (
                await self._moderator_management_menu(user)
                if value == MANAGE_MODERATORS
                else await self._admin_menu(user)
            )
        elif value.startswith(("ag:manage:", "ag:masters:", "ag:players:", "ag:notifications:")):
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
                        f"Кросспол: {_bool_text(user.crossplay)}\n"
                        f"Нужен пропуск: {_bool_text(user.needs_pass)}"
                    ),
                    buttons=[
                        Button(label="Заполнить профиль заново", value="profile:edit"),
                        main_menu_button(),
                    ],
                )
            return await self._begin_profile(user)
        if value == "profile:edit":
            return await self._begin_profile(user)
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

    async def _begin_profile(self, user: User) -> BotResponse:
        user.dialog_state = "PROFILE_FULL_NAME_CYRILLIC"
        user.dialog_context = {"profile_format": PROFILE_FORMAT}
        return BotResponse(
            text=CYRILLIC_FULL_NAME_PROMPT,
            buttons=[dialog_exit_button()],
        )

    @staticmethod
    def _profile_contact_prompt(user: User) -> BotResponse:
        user.dialog_state = "PROFILE_CONTACT"
        if isinstance(user, TelegramUser):
            return BotResponse(
                text="Введите обязательную ссылку на вашу страницу VK:",
                buttons=[dialog_exit_button()],
            )
        return BotResponse(
            text=(
                "Укажите имя пользователя Telegram в формате @username или ссылку https://t.me/username. "
                "Если Telegram нет, нажмите «Пропустить»."
            ),
            buttons=[Button(label=SKIP, value=SKIP), dialog_exit_button()],
        )

    async def _continue(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        state = user.dialog_state
        if state.startswith("PROFILE_"):
            return await self._profile_step(user, message, value)
        if state.startswith("ENLIST_"):
            return await self._enlist_step(user, message, value)
        if state == "CONFIRM_REPLACE":
            return await self._confirm_replacement_step(user, message, value)
        if state == "CONFIRM_WISH":
            return await self._character_command(user, message, value, Operation.CONFIRM)
        if state == "CHARACTER_EDIT":
            return await self._character_command(user, message, value, Operation.UPDATE_CHARACTER_WISH)
        if state == "CO_PLAYER_EDIT":
            return await self._co_player_wish_command(user, message, value)
        if state == "CANCEL_CONFIRM":
            return await self._cancel_step(user, message, value)
        if state.startswith("ADMIN_"):
            return await self._admin_step(user, message, value)
        await self._clear(user)
        return await self._main(user, "Предыдущее действие прервано. Выберите действие:")

    async def _profile_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        context = user.dialog_context
        state = user.dialog_state
        if context.get("profile_format") != PROFILE_FORMAT:
            response = await self._begin_profile(user)
            response.text = f"Формат профиля обновился. {response.text}"
            return response
        if state == "PROFILE_FULL_NAME_CYRILLIC":
            try:
                name_parts = parse_full_name(value, alphabet="cyrillic")
            except ValueError:
                return BotResponse(
                    text=CYRILLIC_FULL_NAME_RETRY_PROMPT,
                    buttons=[dialog_exit_button()],
                )
            surname, name = name_parts[:2]
            patronym = name_parts[2] if len(name_parts) == 3 else "-"
            context["surname_cyrillic"] = surname
            context["name_cyrillic"] = name
            context["patronym_cyrillic"] = patronym
            context["full_name"] = surname_and_name(value)
            user.dialog_state = "PROFILE_NEEDS_PASS"
            return BotResponse(
                text="Нужен ли вам пропуск для входа на место проведения игры?",
                buttons=yes_no_buttons(),
            )
        if state == "PROFILE_NEEDS_PASS":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["needs_pass"] = value == "Да"
            if value == "Да":
                user.dialog_state = "PROFILE_PASS_FOREIGNER"
                return BotResponse(text="Есть ли у вас российское гражданство?", buttons=yes_no_buttons())
            return self._profile_contact_prompt(user)
        if state == "PROFILE_CONTACT":
            if isinstance(user, TelegramUser):
                try:
                    context["contact"] = normalize_vk_url(value)
                except ValueError as exc:
                    return BotResponse(text=f"❌ {exc}. Попробуйте ещё раз:", buttons=[dialog_exit_button()])
            else:
                if value == SKIP:
                    context["contact"] = None
                else:
                    contact = _normalize_vk_telegram_contact(value)
                    if contact is None:
                        return BotResponse(
                            text="Не удалось разобрать профиль Telegram. Попробуйте ещё раз",
                            buttons=[Button(label=SKIP, value=SKIP), dialog_exit_button()],
                        )
                    context["contact"] = contact
            user.dialog_state = "PROFILE_CROSSPLAY"
            return BotResponse(
                text="Готовы ли вы играть за персонажа противоположного пола?",
                buttons=yes_no_buttons(),
            )
        if state == "PROFILE_CROSSPLAY":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["crossplay"] = value == "Да"
            return await self._finish_profile(user, message)
        if state == "PROFILE_PASS_FOREIGNER":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["foreigner"] = value == "Нет"
            if context["foreigner"]:
                user.dialog_state = "PROFILE_PASS_FULL_NAME_LATIN"
                return BotResponse(
                    text=LATIN_FULL_NAME_PROMPT,
                    buttons=[dialog_exit_button()],
                )
            user.dialog_state = "PROFILE_PASS_PHONE"
            return BotResponse(text=PHONE_PROMPT, buttons=[dialog_exit_button()])
        if state == "PROFILE_PASS_FULL_NAME_LATIN":
            try:
                name_parts = parse_full_name(value, alphabet="latin")
            except ValueError:
                return BotResponse(
                    text=LATIN_FULL_NAME_RETRY_PROMPT,
                    buttons=[dialog_exit_button()],
                )
            surname, name = name_parts[:2]
            patronym = name_parts[2] if len(name_parts) == 3 else "-"
            if (context["patronym_cyrillic"] == "-") != (patronym == "-"):
                return BotResponse(
                    text=LATIN_FULL_NAME_RETRY_PROMPT,
                    buttons=[dialog_exit_button()],
                )
            context["surname_latin"] = surname
            context["name_latin"] = name
            context["patronym_latin"] = patronym
            user.dialog_state = "PROFILE_PASS_PHONE"
            return BotResponse(text=PHONE_PROMPT, buttons=[dialog_exit_button()])
        if state == "PROFILE_PASS_PHONE":
            try:
                context["mobile_phone"] = normalize_mobile_phone(value)
            except ValueError:
                return BotResponse(
                    text=f"Некорректный номер. {PHONE_PROMPT}",
                    buttons=[dialog_exit_button()],
                )
            user.dialog_state = "PROFILE_PASS_EMAIL_ADDRESS"
            return BotResponse(text=EMAIL_PROMPT, buttons=[dialog_exit_button()])
        if state == "PROFILE_PASS_EMAIL_ADDRESS":
            try:
                email = validate_pass_email(value)
            except ValidationError:
                return BotResponse(
                    text=f"Некорректный email. {EMAIL_PROMPT}",
                    buttons=[dialog_exit_button()],
                )
            context["email"] = email
            return self._profile_contact_prompt(user)
        raise AssertionError(f"unknown profile state: {state}")

    async def _finish_profile(self, user: User, message: InboundMessage) -> BotResponse:
        context = user.dialog_context
        full_name = str(context["full_name"])
        profile_names = {full_name}
        if user.full_name is not None:
            profile_names.add(user.full_name)
        user.full_name = full_name
        user.crossplay = bool(context["crossplay"])
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
        # Persist the completed profile before handing slow external lookups and
        # per-game projections to the worker. Keep the current profile state until
        # the durable command is published so a failed request can retry safely.
        await self.users.save(user)
        await self.registrations.enqueue_profile_sync(
            message.identity.platform,
            message.identity.platform_user_id,
            ProfileSyncPayload(names=sorted(profile_names)),
            idempotency_key=f"{message.update_id}:{Operation.SYNC_PROFILE.value}",
        )
        await self._clear(user)
        return BotResponse(
            text="✅ Профиль успешно заполнен.",
            buttons=[main_menu_button()],
        )

    async def _show_events(
        self,
        flow: str,
        statuses: frozenset[EventStatus] | None,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        empty_text: str = "Подходящих игр сейчас нет.",
    ) -> BotResponse:
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
        first, last = events[0], events[-1]
        previous, following = await asyncio.gather(
            self.events.list_page(
                statuses=statuses,
                before=(first.created_at, first.event_id),
                archived=False,
                limit=1,
            ),
            self.events.list_page(
                statuses=statuses,
                after=(last.created_at, last.event_id),
                archived=False,
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
                f"Отправьте пожелания по персонажу для этой игры — до {MAX_CHARACTER_WISH_LENGTH} символов.\n\n"
                "После отправки ответа ваше участие будет подтверждено."
            ),
            buttons=[
                *buttons[:-1],
                dialog_exit_button(label="Не подтверждать сейчас"),
            ],
        )

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
        event_statuses = REGISTRATION_OPEN_STATUSES if flow == "cancel" else None
        archived = False if flow in {"cancel", "wishes"} else None
        registration_statuses = (
            frozenset({AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED}) if flow in {"cancel", "wishes"} else None
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
        if flow == "confirm":
            actionable_matches = [
                item
                for item in matches
                if item[0].status is EventStatus.CONFIRMATION_OPEN
                and item[1].attendance_status is not AttendanceStatus.CANCELLED
            ]
        elif flow == "cancel":
            actionable_matches = [
                item for item in matches if item[0].archived_at is None and item[0].status is not EventStatus.CLOSED
            ]
        elif flow == "character":
            actionable_matches = [
                item
                for item in matches
                if item[0].archived_at is None and item[1].attendance_status is not AttendanceStatus.CANCELLED
            ]
        if flow == "wishes" and len(matches) == 1 and not page.has_previous and not page.has_next:
            return self._game_wishes_menu(matches[0][0])
        buttons = [Button(label=event.name, value=f"select:{flow}:{event.event_id}") for event, _ in actionable_matches]
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
            return BotResponse(text=_no_games_for_action_text(flow), buttons=buttons)
        if flow == "confirm" and not actionable_matches:
            unavailable_text = _no_games_for_action_text(flow)
            if any(registration.attendance_status is AttendanceStatus.CANCELLED for _, registration in matches):
                unavailable_text += " Если вы отменили участие, сначала запишитесь на игру снова."
            return BotResponse(
                text=unavailable_text,
                buttons=buttons,
            )
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
            return co_player_wish_prompt(
                skip_value="enlist:wish-play:skip",
                same_day_warning_date=event.game_date if same_day_registrations else None,
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
        if event.archived_at is not None:
            return BotResponse(text=INACTIVE_GAME_RESPONSE)
        if registration.attendance_status is AttendanceStatus.CANCELLED and flow in {
            "confirm",
            "character",
            "wishes",
        }:
            return BotResponse(text="Сначала снова запишитесь на эту игру.")
        if flow == "cancel" and event.status is EventStatus.CLOSED:
            return BotResponse(text=closed_game_cancellation_message(event.name, event.game_date))
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
                    f"Отправьте новый вариант — до {MAX_CHARACTER_WISH_LENGTH} символов."
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
                        "Используйте текущую клавиатуру: нажмите «Пропустить» или "
                        "отправьте сообщением, с кем вы хотите играть."
                    ),
                    buttons=[
                        Button(label=SKIP, value="enlist:wish-play:skip"),
                        dialog_exit_button(),
                    ],
                )
            if value != "enlist:wish-play:skip" and len(value) > MAX_CO_PLAYER_WISH_LENGTH:
                return BotResponse(
                    text=INVALID_INPUT_RESPONSE,
                    buttons=[Button(label=SKIP, value="enlist:wish-play:skip"), dialog_exit_button()],
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
        if event.status is EventStatus.CLOSED:
            return BotResponse(text=closed_game_cancellation_message(event.name, event.game_date))
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

    async def _admin_menu(self, user: User) -> BotResponse:
        if not await self._has_admin_access(user):
            return BotResponse(text="Недостаточно прав.")
        labels = [
            "➕ Создать игру",
            MANAGE_GAMES,
            BACK,
        ]
        if await self._is_admin(user):
            labels.insert(-1, MANAGE_MODERATORS)
            labels.insert(-1, MANAGE_STATISTICS)
        return BotResponse(text="🛠 Администрирование", buttons=[Button(label=x, value=x) for x in labels])

    @staticmethod
    def _identity(user: User) -> BotIdentity:
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        return BotIdentity(platform=platform, platform_user_id=user_id(user))

    @staticmethod
    def _platform_code(platform: Platform) -> str:
        return "t" if platform is Platform.TELEGRAM else "v"

    @staticmethod
    def _platform_from_code(value: str) -> Platform:
        if value == "t":
            return Platform.TELEGRAM
        if value == "v":
            return Platform.VK
        raise ValueError("unknown platform")

    @classmethod
    def _person_label(cls, identity: BotIdentity, profile: User | None) -> str:
        platform_name = "TG" if identity.platform is Platform.TELEGRAM else "VK"
        name = profile.full_name if profile is not None and profile.full_name else f"ID {identity.platform_user_id}"
        return f"{name} · {platform_name}"

    @staticmethod
    def _profile_text(title: str, identity: BotIdentity, profile: User | None) -> str:
        name = profile.full_name if profile is not None and profile.full_name else "не указано"
        lines = [title, "", f"Имя: {name}"]
        if isinstance(profile, TelegramUser):
            telegram = (
                f"https://t.me/{profile.telegram_handle.removeprefix('@')}"
                if profile.telegram_handle is not None
                else "не указан"
            )
            lines.extend((f"Telegram: {telegram}", f"VK: {profile.vk_url or 'не указан'}"))
        elif isinstance(profile, VkUser):
            telegram = (
                f"https://t.me/{profile.telegram_handle.removeprefix('@')}"
                if profile.telegram_handle is not None
                else "не указан"
            )
            lines.extend((f"Telegram: {telegram}", f"VK: https://vk.com/id{profile.vk_id}"))
        elif identity.platform is Platform.VK:
            lines.extend(("Telegram: не указан", f"VK: https://vk.com/id{identity.platform_user_id}"))
        else:
            lines.extend((f"Telegram ID: {identity.platform_user_id}", "VK: не указан"))
        return "\n".join(lines)

    async def _moderator_users(self, *, removable: bool = False) -> list[User]:
        users, configured = await asyncio.gather(self.users.list_all(), self.admins.list_gamemasters())
        configured_keys = {(identity.platform, identity.platform_user_id) for identity in configured}
        matches = []
        for profile in users:
            identity = self._identity(profile)
            configured_role = (identity.platform, identity.platform_user_id) in configured_keys
            if removable:
                if profile.is_gamemaster and not configured_role:
                    matches.append(profile)
            elif profile.is_gamemaster or configured_role:
                matches.append(profile)
        return sorted(
            matches,
            key=lambda profile: (
                (profile.full_name or "").casefold(),
                self._identity(profile).platform.value,
                user_id(profile),
            ),
        )

    async def _event_master_records(self, event_id: str) -> list[tuple[BotIdentity, User | None]]:
        identities = list(await self.events.list_leaders(event_id))
        profiles = await asyncio.gather(
            *(self.users.get(identity.platform, identity.platform_user_id) for identity in identities)
        )
        records = list(zip(identities, profiles, strict=True))
        return sorted(
            records,
            key=lambda record: (
                ((record[1].full_name or "") if record[1] is not None else "").casefold(),
                record[0].platform.value,
                record[0].platform_user_id,
            ),
        )

    async def _notify_status_change(
        self,
        actor: User,
        target: User,
        *,
        request_id: str,
        text: str,
        buttons: list[Button] | None = None,
    ) -> None:
        if self.transport is None:
            raise RuntimeError("deferred transport is not configured")
        buttons = buttons or []
        identity = self._identity(target)
        await self.transport.send(
            platform=identity.platform,
            user_id=identity.platform_user_id,
            request_id=request_id,
            text=text,
            buttons=buttons,
        )
        await self.users.claim_delivery(
            identity.platform,
            identity.platform_user_id,
            request_id,
            buttons,
        )
        if self._identity(actor) == identity:
            actor.last_delivery_operation_id = request_id
            actor.last_bot_buttons = buttons.copy()

    async def _is_event_leader(self, user: User, event_id: str) -> bool:
        return self._identity(user) in await self.events.list_leaders(event_id)

    async def _event_leader_required(self, user: User, event_id: str) -> BotResponse | None:
        if await self._is_admin(user) or await self._is_event_leader(user, event_id):
            return None
        await self._clear(user)
        return BotResponse(text="Только мастера этой игры могут изменять её данные.")

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

    async def _moderator_management_menu(self, user: User) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        await self._clear(user)
        return BotResponse(
            text="Управление модераторами",
            buttons=[
                Button(label=GRANT_GAMEMASTER, value=GRANT_GAMEMASTER),
                Button(label=REVOKE_GAMEMASTER, value=REVOKE_GAMEMASTER),
                Button(label=LIST_GAMEMASTERS, value=LIST_GAMEMASTERS),
                Button(label=BACK, value=ADMIN_MENU),
            ],
        )

    async def _show_moderators(self, user: User, mode: str, *, offset: int = 0) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        if offset < 0:
            return BotResponse(text="Некорректная страница.")
        profiles = await self._moderator_users(removable=mode == "remove")
        if offset >= len(profiles) and offset:
            offset = max(0, ((len(profiles) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = profiles[offset : offset + PAGE_SIZE]
        user.dialog_state = "ADMIN_MODERATOR_REMOVE" if mode == "remove" else "ADMIN_MODERATOR_LIST"
        user.dialog_context = {"moderator_offset": offset}
        if mode == "remove":
            text = "Выберите модератора, с которого нужно снять полномочия:"
            empty = "Нет назначенных в боте модераторов, которых можно снять."
            action = "remove-pick"
        else:
            text = "Модераторы:"
            empty = "Модераторов пока нет."
            action = "view"
        buttons = [
            Button(
                label=self._person_label(self._identity(profile), profile),
                value=(f"mod:{action}:{self._platform_code(self._identity(profile).platform)}:{user_id(profile)}"),
            )
            for profile in page
        ]
        buttons.extend(
            _pagination_buttons(
                previous=f"mod:{mode}-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"mod:{mode}-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(profiles) else None,
                cancel=MANAGE_MODERATORS,
            )
        )
        return BotResponse(text=text if page else empty, buttons=buttons)

    async def _event_masters_menu(self, user: User, event: Event) -> BotResponse:
        denied = await self._event_leader_required(user, event.event_id)
        if denied is not None:
            return denied
        await self._clear(user)
        buttons = [
            Button(label=ADD_EVENT_LEADER, value=f"master:add:{event.event_id}"),
            Button(label=REMOVE_EVENT_LEADER, value=f"master:remove:{event.event_id}"),
            Button(label=LIST_EVENT_LEADERS, value=f"master:list:{event.event_id}"),
            game_management_button(event.event_id),
        ]
        if await self._is_admin(user) and not await self._is_event_leader(user, event.event_id):
            buttons.insert(0, Button(label=ADD_SELF_EVENT_LEADER, value=f"master:self-add:{event.event_id}"))
        return BotResponse(
            text=f"Мастера игры {event_reference(event.name, event.game_date)}",
            buttons=buttons,
        )

    async def _show_event_master_candidates(
        self,
        user: User,
        event: Event,
        *,
        offset: int = 0,
    ) -> BotResponse:
        if offset < 0:
            return BotResponse(text="Некорректная страница.")
        moderators, admins, current = await asyncio.gather(
            self._moderator_users(),
            self.admins.list_admins(),
            self.events.list_leaders(event.event_id),
        )
        admin_profiles = await asyncio.gather(
            *(self.users.get(identity.platform, identity.platform_user_id) for identity in admins)
        )
        eligible = {(self._identity(profile).platform, user_id(profile)): profile for profile in moderators}
        eligible.update(
            {
                (identity.platform, identity.platform_user_id): profile
                for identity, profile in zip(admins, admin_profiles, strict=True)
                if profile is not None
            }
        )
        current_keys = {(identity.platform, identity.platform_user_id) for identity in current}
        candidates = sorted(
            (profile for key, profile in eligible.items() if key not in current_keys),
            key=lambda profile: (
                (profile.full_name or "").casefold(),
                self._identity(profile).platform.value,
                user_id(profile),
            ),
        )
        if offset >= len(candidates) and offset:
            offset = max(0, ((len(candidates) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = candidates[offset : offset + PAGE_SIZE]
        user.dialog_state = "ADMIN_EVENT_LEADER_ADD_PICK"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "master_offset": offset,
        }
        buttons = [
            Button(
                label=self._person_label(self._identity(profile), profile),
                value=(f"master:add-pick:{self._platform_code(self._identity(profile).platform)}:{user_id(profile)}"),
            )
            for profile in page
        ]
        buttons.append(Button(label="🔗 Ввести профиль Telegram или VK", value="master:add-direct"))
        buttons.extend(
            _pagination_buttons(
                previous=f"master:add-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"master:add-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(candidates) else None,
                cancel=f"ag:masters:{event.event_id}",
            )
        )
        text = (
            "Выберите модератора или администратора, которого нужно добавить мастером игры "
            f"{event_reference(event.name, event.game_date)}:"
            if page
            else (
                "Нет модераторов или администраторов, которых можно выбрать. "
                "Можно ввести Telegram- или VK-профиль напрямую."
            )
        )
        return BotResponse(text=text, buttons=buttons)

    async def _show_event_masters(
        self,
        user: User,
        event: Event,
        mode: str,
        *,
        offset: int = 0,
    ) -> BotResponse:
        if offset < 0:
            return BotResponse(text="Некорректная страница.")
        records = await self._event_master_records(event.event_id)
        if mode == "remove":
            records = [record for record in records if record[1] is not None]
        if offset >= len(records) and offset:
            offset = max(0, ((len(records) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = records[offset : offset + PAGE_SIZE]
        user.dialog_state = "ADMIN_EVENT_LEADER_REMOVE" if mode == "remove" else "ADMIN_EVENT_LEADER_LIST"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "master_offset": offset,
        }
        action = "remove-pick" if mode == "remove" else "view"
        buttons = [
            Button(
                label=self._person_label(identity, profile),
                value=(f"master:{action}:{self._platform_code(identity.platform)}:{identity.platform_user_id}"),
            )
            for identity, profile in page
        ]
        buttons.extend(
            _pagination_buttons(
                previous=f"master:{mode}-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"master:{mode}-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(records) else None,
                cancel=f"ag:masters:{event.event_id}",
            )
        )
        if mode == "remove":
            text = f"Выберите мастера, которого нужно удалить из игры {event_reference(event.name, event.game_date)}:"
            empty = "В этой игре нет назначенных мастеров, которых можно удалить."
        else:
            text = f"Мастера игры {event_reference(event.name, event.game_date)}:"
            empty = "У этой игры пока нет мастеров."
        return BotResponse(text=text if page else empty, buttons=buttons)

    async def _admin_callback(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse | None:
        parts = value.split(":")
        try:
            if value.startswith("master:self-add:") and len(parts) == 3:
                if not await self._is_admin(user):
                    return BotResponse(text="Недостаточно прав.")
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                return await self._assign_event_leader(user, message, event, user)
            if value.startswith("mod:list-page:"):
                return await self._show_moderators(user, "list", offset=int(parts[2]))
            if value.startswith("mod:remove-page:"):
                return await self._show_moderators(user, "remove", offset=int(parts[2]))
            if value.startswith("mod:view:") and len(parts) == 4:
                if not await self._is_admin(user) or user.dialog_state != "ADMIN_MODERATOR_LIST":
                    return BotResponse(text="Недостаточно прав.")
                platform = self._platform_from_code(parts[2])
                target = await self.users.get(platform, int(parts[3]))
                if target is None:
                    return BotResponse(text="Пользователь не найден.")
                offset = int(user.dialog_context.get("moderator_offset", 0))
                return BotResponse(
                    text=self._profile_text("Модератор", self._identity(target), target),
                    buttons=[Button(label=BACK, value=f"mod:list-page:{offset}")],
                )
            if value.startswith("mod:remove-pick:") and len(parts) == 4:
                if not await self._is_admin(user) or user.dialog_state != "ADMIN_MODERATOR_REMOVE":
                    return BotResponse(text="Недостаточно прав.")
                platform = self._platform_from_code(parts[2])
                target = await self.users.get(platform, int(parts[3]))
                configured = {
                    (identity.platform, identity.platform_user_id) for identity in await self.admins.list_gamemasters()
                }
                if target is None or not target.is_gamemaster or (platform, int(parts[3])) in configured:
                    return await self._show_moderators(user, "remove")
                user.dialog_state = "ADMIN_MODERATOR_REMOVE_CONFIRM"
                user.dialog_context["target_platform"] = platform.value
                user.dialog_context["target_user_id"] = int(parts[3])
                return BotResponse(
                    text=(f"Снять полномочия модератора с пользователя «{target.full_name}»?"),
                    buttons=[
                        Button(label="Да, снять", value="mod:remove-confirm"),
                        Button(label=BACK, value=f"mod:remove-page:{user.dialog_context['moderator_offset']}"),
                    ],
                )
            if value == "mod:remove-confirm":
                if not await self._is_admin(user) or user.dialog_state != "ADMIN_MODERATOR_REMOVE_CONFIRM":
                    return BotResponse(text="Недостаточно прав.")
                platform = Platform(str(user.dialog_context["target_platform"]))
                target_id = int(user.dialog_context["target_user_id"])
                target = await self.users.get(platform, target_id)
                request_id = f"gamemaster-revoke:{platform.value}:{target_id}:{message.update_id}"
                if target is None:
                    await self._clear(user)
                    return BotResponse(
                        text="Этот пользователь уже не является модератором.",
                        buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
                    )
                if target.is_gamemaster:
                    await self.users.revoke_gamemaster(platform, target_id, request_id)
                    refreshed = await self.users.get(platform, target_id)
                    if refreshed is None or refreshed.is_gamemaster:
                        raise RuntimeError("gamemaster revocation was not persisted")
                    target = refreshed
                elif target.gamemaster_grant_operation_id != request_id:
                    await self._clear(user)
                    return BotResponse(
                        text="Этот пользователь уже не является модератором.",
                        buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
                    )
                if target.last_delivery_operation_id != request_id:
                    await self._notify_status_change(
                        user,
                        target,
                        request_id=request_id,
                        text=GAMEMASTER_REVOKED_NOTIFICATION,
                    )
                if self._identity(user) == self._identity(target):
                    user.is_gamemaster = False
                    user.gamemaster_grant_operation_id = request_id
                await self._clear(user)
                return BotResponse(
                    text="✅ Полномочия модератора сняты.",
                    buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
                )
            if value.startswith("master:add:") and len(parts) == 3:
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                denied = await self._event_leader_required(user, event.event_id)
                return denied or await self._show_event_master_candidates(user, event)
            if value.startswith("master:remove:") and len(parts) == 3:
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                denied = await self._event_leader_required(user, event.event_id)
                return denied or await self._show_event_masters(user, event, "remove")
            if value.startswith("master:list:") and len(parts) == 3:
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                denied = await self._event_leader_required(user, event.event_id)
                return denied or await self._show_event_masters(user, event, "list")
            if value.startswith("master:add-page:"):
                event = await self.events.get(str(user.dialog_context["event_id"]))
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                return await self._show_event_master_candidates(user, event, offset=int(parts[2]))
            if value.startswith("master:add-pick:") and len(parts) == 4:
                if user.dialog_state != "ADMIN_EVENT_LEADER_ADD_PICK":
                    return BotResponse(text="Некорректное действие.")
                event = await self.events.get(str(user.dialog_context["event_id"]))
                platform = self._platform_from_code(parts[2])
                target = await self.users.get(platform, int(parts[3]))
                if event is None or target is None:
                    return BotResponse(text="Пользователь или игра не найдены.")
                return await self._assign_event_leader(user, message, event, target)
            if value == "master:add-direct":
                if user.dialog_state != "ADMIN_EVENT_LEADER_ADD_PICK":
                    return BotResponse(text="Некорректное действие.")
                user.dialog_state = "ADMIN_EVENT_LEADER_PLATFORM"
                return self._event_leader_platform_prompt(str(user.dialog_context["event_id"]))
            if value.startswith("master:remove-page:") or value.startswith("master:list-page:"):
                mode = "remove" if parts[1] == "remove-page" else "list"
                event = await self.events.get(str(user.dialog_context["event_id"]))
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                return await self._show_event_masters(user, event, mode, offset=int(parts[2]))
            if value.startswith("master:view:") and len(parts) == 4:
                if user.dialog_state != "ADMIN_EVENT_LEADER_LIST":
                    return BotResponse(text="Некорректное действие.")
                platform = self._platform_from_code(parts[2])
                identity = BotIdentity(platform=platform, platform_user_id=int(parts[3]))
                profile = await self.users.get(platform, identity.platform_user_id)
                offset = int(user.dialog_context.get("master_offset", 0))
                return BotResponse(
                    text=self._profile_text("Мастер", identity, profile),
                    buttons=[Button(label=BACK, value=f"master:list-page:{offset}")],
                )
            if value.startswith("master:remove-pick:") and len(parts) == 4:
                if user.dialog_state != "ADMIN_EVENT_LEADER_REMOVE":
                    return BotResponse(text="Некорректное действие.")
                platform = self._platform_from_code(parts[2])
                target_id = int(parts[3])
                target = await self.users.get(platform, target_id)
                event_id = str(user.dialog_context["event_id"])
                identity = BotIdentity(platform=platform, platform_user_id=target_id)
                if target is None or identity not in await self.events.list_leaders(event_id):
                    event = await self.events.get(event_id)
                    if event is None:
                        return BotResponse(text="Игра не найдена.")
                    return await self._show_event_masters(user, event, "remove")
                user.dialog_state = "ADMIN_EVENT_LEADER_REMOVE_CONFIRM"
                user.dialog_context["target_platform"] = platform.value
                user.dialog_context["target_user_id"] = target_id
                return BotResponse(
                    text=f"Удалить мастера «{target.full_name}» из этой игры?",
                    buttons=[
                        Button(label="Да, удалить", value="master:remove-confirm"),
                        Button(
                            label=BACK,
                            value=f"master:remove-page:{user.dialog_context['master_offset']}",
                        ),
                    ],
                )
            if value == "master:remove-confirm":
                if user.dialog_state != "ADMIN_EVENT_LEADER_REMOVE_CONFIRM":
                    return BotResponse(text="Некорректное действие.")
                event_id = str(user.dialog_context["event_id"])
                event_name = str(user.dialog_context["event_name"])
                event = await self.events.get(event_id)
                if event is None:
                    await self._clear(user)
                    return BotResponse(text="Игра не найдена.")
                reference = event_reference(event_name, event.game_date)
                platform = Platform(str(user.dialog_context["target_platform"]))
                target_id = int(user.dialog_context["target_user_id"])
                identity = BotIdentity(platform=platform, platform_user_id=target_id)
                target = await self.users.get(platform, target_id)
                request_id = f"leader-revoke:{event_id}:{platform.value}:{target_id}:{message.update_id}"
                pending = user.dialog_context.get("pending_leader_revoke") == request_id
                if target is None:
                    await self._clear(user)
                    return BotResponse(
                        text="Этот пользователь уже не является мастером этой игры.",
                        buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                    )
                if identity in await self.events.list_leaders(event_id):
                    user.dialog_context["pending_leader_revoke"] = request_id
                    await self.users.save(user)
                    if not await self.events.remove_leader(event_id, identity):
                        raise RuntimeError("event leader revocation was not persisted")
                    pending = True
                elif not pending:
                    await self._clear(user)
                    return BotResponse(
                        text="Этот пользователь уже не является мастером этой игры.",
                        buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                    )
                await self.administration.refresh_pass_table(event_id)
                if target.last_delivery_operation_id != request_id:
                    await self._notify_status_change(
                        user,
                        target,
                        request_id=request_id,
                        text=f"Вы больше не являетесь мастером игры {reference}.",
                    )
                await self._clear(user)
                return BotResponse(
                    text=f"✅ Мастер удалён из игры {reference}.",
                    buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                )
        except (KeyError, TypeError, ValueError):
            return BotResponse(text="Некорректное действие.")
        return None

    async def _admin_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._has_admin_access(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        event_bound_states = {
            "ADMIN_STATUS_SELECT",
            "ADMIN_UPDATE_PLAYER_AMOUNT",
            "ADMIN_CONFIRMATION_DEADLINE",
            "ADMIN_NOTIFICATION_LINK_TEXT",
            "ADMIN_NOTIFICATION_MESSAGE_TEXT",
            "ADMIN_NOTIFICATION_CONFIRM",
            "ADMIN_NOTIFICATION_TEXT",
            "ADMIN_ARCHIVE_NAME",
            "ADMIN_DELETE_NAME",
            "ADMIN_EVENT_LEADER_PLATFORM",
            "ADMIN_EVENT_LEADER_PROFILE",
            "ADMIN_EVENT_LEADER_ADD_PICK",
            "ADMIN_EVENT_LEADER_REMOVE",
            "ADMIN_EVENT_LEADER_REMOVE_CONFIRM",
            "ADMIN_EVENT_LEADER_LIST",
            "ADMIN_REMOVE_PICK",
            "ADMIN_REMOVE_CONFIRM",
            "ADMIN_REMOVE_WAITING_CONFIRM",
            "ADMIN_WAITING_PICK",
        }
        if user.dialog_state in event_bound_states:
            event_id = str(user.dialog_context["event_id"])
            event = await self.events.get(event_id)
            if event is not None and event.archived_at is not None:
                await self._clear(user)
                if not await self._is_admin(user):
                    return BotResponse(text="Игра не найдена.")
                return BotResponse(
                    text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.",
                    buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                )
            pending_self_removal = (
                user.dialog_state == "ADMIN_EVENT_LEADER_REMOVE_CONFIRM"
                and "pending_leader_revoke" in user.dialog_context
                and user.dialog_context.get("target_platform") == self._identity(user).platform.value
                and user.dialog_context.get("target_user_id") == self._identity(user).platform_user_id
            )
            if not pending_self_removal:
                denied = await self._event_leader_required(user, event_id)
                if denied is not None:
                    return denied
        if value.startswith(("mod:", "master:")):
            admin_response = await self._admin_callback(user, message, value)
            if admin_response is not None:
                return admin_response
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
                    text="Выберите бот, которым пользуется новый модератор:",
                    buttons=[
                        Button(label="Telegram", value="admin:gamemaster:telegram"),
                        Button(label="VK", value="admin:gamemaster:vk"),
                        Button(label=BACK, value=MANAGE_MODERATORS),
                    ],
                )
            user.dialog_state = "ADMIN_GAMEMASTER_PROFILE"
            user.dialog_context = {"gamemaster_platform": target_platform.value}
            if target_platform is Platform.TELEGRAM:
                prompt = "Отправьте ссылку на профиль Telegram или @username будущего модератора:"
            else:
                prompt = "Отправьте ссылку на профиль VK будущего модератора:"
            return BotResponse(text=prompt, buttons=[Button(label=BACK, value=MANAGE_MODERATORS)])
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
                prompt = "Отправьте ссылку на профиль Telegram или @username модератора:"
            else:
                prompt = "Отправьте ссылку на профиль VK модератора:"
            return BotResponse(
                text=prompt,
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )
        if user.dialog_state == "ADMIN_EVENT_LEADER_PROFILE":
            return await self._add_event_leader(user, message, value)
        if user.dialog_state == "ADMIN_REMOVE_PICK":
            return await self._admin_remove_pick(user, message, value)
        if user.dialog_state == "ADMIN_REMOVE_CONFIRM":
            return await self._admin_remove_confirm(user, message, value)
        if user.dialog_state == "ADMIN_REMOVE_WAITING_CONFIRM":
            return await self._admin_remove_waiting_confirm(user, message, value)
        if user.dialog_state == "ADMIN_WAITING_PICK":
            return await self._admin_waiting_pick(user, value)
        if user.dialog_state == "ADMIN_CREATE_NAME":
            clean_name = value.strip()
            if not clean_name:
                return BotResponse(text="Название игры не может быть пустым. Введите название игры:")
            if len(clean_name) > 200:
                return BotResponse(text="Название игры не должно превышать 200 символов. Введите название игры:")
            user.dialog_state = "ADMIN_CREATE_DATE"
            user.dialog_context = {"event_name": clean_name}
            return BotResponse(text="Введите дату игры строго в формате DD.MM.YY:")
        if user.dialog_state == "ADMIN_CREATE_DATE":
            try:
                game_date = parse_game_date(value)
            except ValueError:
                return BotResponse(text="Введите корректную дату игры строго в формате DD.MM.YY:")
            user.dialog_state = "ADMIN_CREATE_PLAYER_AMOUNT"
            user.dialog_context["game_date"] = game_date.isoformat()
            return BotResponse(text=f"Введите количество игроков (от 1 до {MAX_PLAYER_AMOUNT}):")
        if user.dialog_state == "ADMIN_CREATE_PLAYER_AMOUNT":
            raw_amount = value.strip()
            if not raw_amount.isdecimal():
                return BotResponse(text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:")
            player_amount = int(raw_amount)
            if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
                return BotResponse(text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:")
            game_date = date.fromisoformat(str(user.dialog_context["game_date"]))
            event = await self.administration.create_event(
                str(user.dialog_context["event_name"]),
                game_date,
                player_amount,
                [self._identity(user)],
            )
            await self._clear(user)
            return BotResponse(
                text=(
                    f"✅ Игра создана.\n\nНазвание:\n{event.name}\n\n"
                    f"Дата игры: {format_game_date(game_date)}\n\n"
                    f"Количество игроков: {event.player_amount}\n\n"
                    f"Статус: {STATUS_REGISTRATION}. Игроки уже могут записываться, "
                    "но пока не могут подтверждать участие."
                ),
                buttons=[game_management_button(event.event_id)],
            )
        if user.dialog_state == "ADMIN_UPDATE_PLAYER_AMOUNT":
            raw_amount = value.strip()
            if not raw_amount.isdecimal():
                return BotResponse(text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:")
            player_amount = int(raw_amount)
            if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
                return BotResponse(text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:")
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.UPDATE_PLAYER_AMOUNT,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=PlayerAmountPayload(player_amount=player_amount),
                reply_context=ReplyContext(
                    text_success=(
                        f"Количество игроков игры {_context_event_reference(context)} изменено: {player_amount}."
                    ),
                    buttons=[game_management_button(str(context["event_id"]))],
                ),
                idempotency_key=f"{message.update_id}:{Operation.UPDATE_PLAYER_AMOUNT.value}",
            )
            return BotResponse(
                text="⏳ Изменение количества игроков принято в обработку.",
                buttons=[game_management_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state == "ADMIN_STATUS_SELECT":
            choice = ADMIN_STATUS_CHOICES.get(value)
            if choice is None:
                return _admin_status_response(
                    str(user.dialog_context["event_name"]),
                    EventStatus(str(user.dialog_context["event_status"])),
                    str(user.dialog_context["event_id"]),
                    (
                        date.fromisoformat(str(user.dialog_context["game_date"]))
                        if user.dialog_context.get("game_date") is not None
                        else None
                    ),
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
                    text_success=f"Статус игры {_context_event_reference(context)} изменён: {status_label}.",
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
                    text_success=(f"Статус игры {_context_event_reference(context)} изменён: {STATUS_CONFIRMATION}."),
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
        if user.dialog_state in {
            "ADMIN_NOTIFICATION_LINK_TEXT",
            "ADMIN_NOTIFICATION_MESSAGE_TEXT",
            "ADMIN_NOTIFICATION_TEXT",
        }:
            event_name = str(user.dialog_context["event_name"])
            event_id = str(user.dialog_context["event_id"])
            raw_game_date = user.dialog_context.get("game_date")
            notification_game_date = date.fromisoformat(str(raw_game_date)) if raw_game_date is not None else None
            link = user.dialog_state == "ADMIN_NOTIFICATION_LINK_TEXT"
            if message.callback is not None:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=link,
                    game_date=notification_game_date,
                    invalid="Отправьте содержимое обычным текстовым сообщением.",
                )
            if not value:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=link,
                    game_date=notification_game_date,
                    invalid="Сообщение не может быть пустым.",
                )
            if link and not is_plain_chat_link(value):
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=True,
                    game_date=notification_game_date,
                    invalid="Пришлите только корректную ссылку-приглашение в чат Telegram или VK.",
                )
            if not link and is_plain_chat_link(value):
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=False,
                    game_date=notification_game_date,
                    invalid=f"Для рассылки ссылки на чат используйте кнопку «{SEND_CONFIRMED_LINK}».",
                )
            if len(value) > 4000 or len(confirmed_notification_text(event_name, value, notification_game_date)) > 4000:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=link,
                    game_date=notification_game_date,
                    invalid="Итоговое сообщение не должно превышать 4000 символов.",
                )
            recipient_count = await self.administration.confirmed_registration_count(
                event_id,
                main_roster_only=link,
            )
            user.dialog_state = "ADMIN_NOTIFICATION_CONFIRM"
            user.dialog_context["notification_text"] = value
            user.dialog_context["notification_recipient_count"] = recipient_count
            return _confirmed_notification_preview(
                event_name,
                event_id,
                value,
                recipient_count,
                notification_game_date,
            )
        if user.dialog_state == "ADMIN_NOTIFICATION_CONFIRM":
            event_name = str(user.dialog_context["event_name"])
            event_id = str(user.dialog_context["event_id"])
            raw_game_date = user.dialog_context.get("game_date")
            notification_game_date = date.fromisoformat(str(raw_game_date)) if raw_game_date is not None else None
            notification_text = str(user.dialog_context["notification_text"])
            recipient_count = int(user.dialog_context["notification_recipient_count"])
            if value != "admin:notification:confirm":
                return _confirmed_notification_preview(
                    event_name,
                    event_id,
                    notification_text,
                    recipient_count,
                    notification_game_date,
                )
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.SEND_CONFIRMED_NOTIFICATION,
                event_id=event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=NotificationPayload(text=notification_text),
                reply_context=ReplyContext(buttons=[notification_management_button(event_id)]),
                idempotency_key=f"{message.update_id}:{Operation.SEND_CONFIRMED_NOTIFICATION.value}",
            )
            return BotResponse(
                text="⏳ Уведомление принято в обработку.",
                buttons=[notification_management_button(event_id)],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state in {"ADMIN_ARCHIVE_NAME", "ADMIN_DELETE_NAME"}:
            context = user.dialog_context.copy()
            expected = str(context["event_name"])
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
                        f"📦 Игра {_context_event_reference(context)} архивирована. "
                        "Игра, записи и XLSX-таблицы сохранены в постоянном списке."
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
                target = await self._find_telegram_target(value)
            else:
                normalized = normalize_vk_url(value)
                if self.vk_user_ids is None:
                    raise RuntimeError("VK user ID resolver is not configured")
                target_id = await self.vk_user_ids.resolve_user_id(normalized)
                target = None if target_id is None else await self.users.get(Platform.VK, target_id)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:", buttons=[Button(label=BACK, value=MANAGE_MODERATORS)]
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
            )
        if not target.profile_complete:
            return BotResponse(
                text="Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите.",
                buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
            )
        target_id = user_id(target)
        request_id = f"gamemaster-grant:{target_platform.value}:{target_id}:{message.update_id}"
        configured_gamemaster = await self.admins.is_gamemaster(target_platform, target_id)
        if configured_gamemaster or (target.is_gamemaster and target.gamemaster_grant_operation_id != request_id):
            await self._clear(user)
            return BotResponse(text="Этот пользователь уже является модератором.")

        if not target.is_gamemaster:
            await self.users.grant_gamemaster(target_platform, target_id, request_id)
            refreshed = await self.users.get(target_platform, target_id)
            if refreshed is None or not refreshed.is_gamemaster:
                raise RuntimeError("gamemaster grant was not persisted")
            target = refreshed
        if target.gamemaster_grant_operation_id != request_id:
            await self._clear(user)
            return BotResponse(text="Этот пользователь уже является модератором.")
        if target.last_delivery_operation_id != request_id:
            await self._notify_status_change(
                user,
                target,
                request_id=request_id,
                text=GAMEMASTER_NOTIFICATION,
            )

        if message.identity.platform is target_platform and message.identity.platform_user_id == target_id:
            user.is_gamemaster = True
            user.gamemaster_grant_operation_id = request_id
            user.last_delivery_operation_id = request_id
        await self._clear(user)
        platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
        return BotResponse(text=f"✅ Пользователь назначен модератором в {platform_name}.")

    async def _find_telegram_target(self, value: str) -> TelegramUser | None:
        handle = normalize_telegram_profile(value)
        target = await self.users.find_telegram_by_handle(handle)
        if target is not None:
            return target
        target = await self.administration.find_telegram_user_by_profile(handle)
        if target is not None:
            await self.users.update_telegram_handle(target.tg_id, handle)
            target.telegram_handle = handle
        return target

    @staticmethod
    def _event_leader_platform_prompt(event_id: str) -> BotResponse:
        return BotResponse(
            text="Выберите бот, которым пользуется модератор, которого нужно добавить мастером:",
            buttons=[
                Button(label="Telegram", value="admin:event-leader:telegram"),
                Button(label="VK", value="admin:event-leader:vk"),
                Button(label=BACK, value=f"ag:masters:{event_id}"),
            ],
        )

    async def _add_event_leader(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        target_platform = Platform(str(user.dialog_context["gamemaster_platform"]))
        target: User | None
        try:
            if target_platform is Platform.TELEGRAM:
                target = await self._find_telegram_target(value)
            else:
                normalized = normalize_vk_url(value)
                if self.vk_user_ids is None:
                    raise RuntimeError("VK user ID resolver is not configured")
                target_id = await self.vk_user_ids.resolve_user_id(normalized)
                target = None if target_id is None else await self.users.get(Platform.VK, target_id)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:",
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )
        if not target.profile_complete:
            return BotResponse(
                text="Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите.",
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )

        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.")
        return await self._assign_event_leader(user, message, event, target)

    async def _assign_event_leader(
        self,
        user: User,
        message: InboundMessage,
        event: Event,
        target: User,
    ) -> BotResponse:
        identity = self._identity(target)
        event_id = event.event_id
        request_id = (
            f"leader-grant:{event_id}:{identity.platform.value}:{identity.platform_user_id}:{message.update_id}"
        )
        configured_moderator, configured_admin = await asyncio.gather(
            self.admins.is_gamemaster(identity.platform, identity.platform_user_id),
            self.admins.is_admin(identity.platform, identity.platform_user_id),
        )
        if not target.is_gamemaster and not configured_moderator and not configured_admin:
            return BotResponse(
                text="Добавить мастером можно только модератора или администратора.",
                buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
            )
        role_name = "Администратор" if configured_admin else "Модератор"
        reference = event_reference(event.name, event.game_date)
        notification_button = Button(label="Управление игрой", value=f"ag:manage:{event_id}")
        if identity in await self.events.list_leaders(event_id):
            if (
                user.dialog_context.get("pending_leader_grant") == request_id
                and target.last_delivery_operation_id != request_id
            ):
                await self.administration.refresh_pass_table(event_id)
                await self._notify_status_change(
                    user,
                    target,
                    request_id=request_id,
                    text=f"Вы добавлены мастером игры {reference}.",
                    buttons=[notification_button],
                )
                await self._clear(user)
                return BotResponse(
                    text=f"✅ {role_name} добавлен мастером игры {reference}.",
                    buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                )
            await self._clear(user)
            return BotResponse(
                text=f"Этот пользователь уже является мастером игры {reference}.",
                buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
            )

        user.dialog_context["pending_leader_grant"] = request_id
        await self.users.save(user)
        await self.events.add_leader(event_id, identity)
        if identity not in await self.events.list_leaders(event_id):
            raise RuntimeError("event leader grant was not persisted")
        await self.administration.refresh_pass_table(event_id)
        await self._notify_status_change(
            user,
            target,
            request_id=request_id,
            text=f"Вы добавлены мастером игры {reference}.",
            buttons=[notification_button],
        )
        await self._clear(user)
        return BotResponse(
            text=f"✅ {role_name} добавлен мастером игры {reference}.",
            buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
        )

    @staticmethod
    def _admin_game_cursor(direction: str, event: Event, *, archived: bool) -> str:
        micros = int(event.created_at.timestamp() * 1_000_000)
        scope = "r" if archived else "a"
        return f"page:{scope}:{direction}:{micros}:{event.event_id}"

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
            limit=PAGE_SIZE,
        )
        user.dialog_state = "ADMIN_REMOVE_PICK"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "game_date": event.game_date.isoformat() if event.game_date is not None else None,
        }
        if not page.rows:
            return BotResponse(
                text=(
                    f"В игре {event_reference(event.name, event.game_date)} нет записавшихся "
                    "или подтвердивших участие игроков."
                ),
                buttons=_pagination_buttons(
                    previous=None,
                    following=None,
                    cancel=f"ag:players:{event.event_id}",
                ),
            )

        choices: dict[str, str] = {}
        buttons: list[Button] = []
        for registration in page.rows:
            token = token_urlsafe(8)
            choices[token] = registration.participant_key
            buttons.append(
                Button(
                    label=f"{registration.display_name} — {_attendance_status_text(registration.attendance_status)}",
                    value=f"admin:remove:pick:{token}",
                )
            )
        page_token = token_urlsafe(8)
        buttons.extend(
            _pagination_buttons(
                previous=f"admin:remove:page:p:{page_token}" if page.has_previous else None,
                following=f"admin:remove:page:n:{page_token}" if page.has_next else None,
                cancel=f"ag:players:{event.event_id}",
            )
        )
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
                f"{prefix}Удаление игрока из игры {event_reference(event.name, event.game_date)}\n\n"
                "Выберите игрока. Показаны записавшиеся и подтвердившие участие; "
                "на кнопках указаны имена из их профилей."
            ),
            buttons=buttons,
        )

    async def _show_waiting_players(
        self,
        user: User,
        event: Event,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        invalid: str = "",
    ) -> BotResponse:
        page = await self.administration.waiting_registration_page(
            event.event_id,
            after=after,
            before=before,
            limit=PAGE_SIZE,
        )
        user.dialog_state = "ADMIN_WAITING_PICK"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "game_date": event.game_date.isoformat() if event.game_date is not None else None,
        }
        if not page.rows:
            return BotResponse(
                text=(
                    f"В игре {event_reference(event.name, event.game_date)} нет записавшихся, "
                    "которые ещё не подтвердили участие."
                ),
                buttons=_pagination_buttons(
                    previous=None,
                    following=None,
                    cancel=f"ag:manage:{event.event_id}",
                ),
            )

        choices: dict[str, str] = {}
        buttons: list[Button] = []
        for registration in page.rows:
            token = token_urlsafe(8)
            choices[token] = registration.participant_key
            buttons.append(
                Button(
                    label=registration.display_name,
                    value=f"admin:waiting:pick:{token}",
                )
            )
        page_token = token_urlsafe(8)
        buttons.extend(
            _pagination_buttons(
                previous=f"admin:waiting:page:p:{page_token}" if page.has_previous else None,
                following=f"admin:waiting:page:n:{page_token}" if page.has_next else None,
                cancel=f"ag:manage:{event.event_id}",
            )
        )
        user.dialog_context.update(
            {
                "waiting_choices": choices,
                "waiting_page_token": page_token,
                "waiting_first": [page.rows[0].created_at.isoformat(), page.rows[0].participant_key],
                "waiting_last": [page.rows[-1].created_at.isoformat(), page.rows[-1].participant_key],
            }
        )
        prefix = f"{invalid}\n\n" if invalid else ""
        return BotResponse(
            text=(
                f"{prefix}Не подтвердившие участие в игре "
                f"{event_reference(event.name, event.game_date)}\n\n"
                "Выберите игрока, чтобы увидеть его профили."
            ),
            buttons=buttons,
        )

    @staticmethod
    def _waiting_player_profile_text(registration: Registration, participant: User) -> str:
        if isinstance(participant, TelegramUser):
            bot_name = "Telegram"
            vk_profile = registration.vk_profile or participant.vk_url or "не указан"
            telegram_profile = registration.telegram_profile
            if telegram_profile is None and participant.telegram_handle is not None:
                telegram_profile = f"https://t.me/{participant.telegram_handle.removeprefix('@')}"
        else:
            bot_name = "ВК"
            vk_profile = registration.vk_profile or f"https://vk.com/id{participant.vk_id}"
            telegram_profile = registration.telegram_profile
            if telegram_profile is None and participant.telegram_handle is not None:
                telegram_profile = f"https://t.me/{participant.telegram_handle.removeprefix('@')}"

        text = f"Этот пользователь зарегистрировался через бота {bot_name}. Вот его профиль вк: {vk_profile}"
        if telegram_profile is not None:
            text += f"\nВот его профиль Telegram: {telegram_profile}"
        return text

    async def _admin_waiting_pick(self, user: User, value: str) -> BotResponse:
        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
        if value == f"ag:waiting:{event_id}":
            return await self._show_waiting_players(user, event)

        parts = value.split(":")
        if len(parts) == 5 and parts[:3] == ["admin", "waiting", "page"]:
            direction, page_token = parts[3:]
            if page_token != user.dialog_context.get("waiting_page_token") or direction not in {"n", "p"}:
                return await self._show_waiting_players(user, event, invalid="Эта страница устарела.")
            if direction == "p":
                return await self._show_waiting_players(
                    user,
                    event,
                    before=self._stored_registration_cursor(user, "waiting_first"),
                )
            return await self._show_waiting_players(
                user,
                event,
                after=self._stored_registration_cursor(user, "waiting_last"),
            )
        if len(parts) != 4 or parts[:3] != ["admin", "waiting", "pick"]:
            return await self._show_waiting_players(user, event, invalid="Выберите игрока кнопкой из списка.")
        token = parts[3]
        choices = user.dialog_context.get("waiting_choices")
        registration_key = choices.get(token) if isinstance(choices, dict) else None
        if not isinstance(registration_key, str):
            return await self._show_waiting_players(user, event, invalid="Этот список устарел.")
        registration = await self.administration.get_registration(event_id, registration_key)
        if registration is None or registration.attendance_status is not AttendanceStatus.WAITING:
            return await self._show_waiting_players(
                user,
                event,
                invalid="Этот игрок уже не находится в списке не подтвердивших участие.",
            )
        participant = await self.administration.registration_user(event_id, registration_key)
        if participant is None:
            return BotResponse(
                text="Не удалось найти профиль этого пользователя.",
                buttons=[
                    Button(label="⬅️ К списку", value=f"ag:waiting:{event_id}"),
                    game_management_button(event_id),
                ],
            )
        return BotResponse(
            text=self._waiting_player_profile_text(registration, participant),
            buttons=[
                Button(label="⬅️ К списку", value=f"ag:waiting:{event_id}"),
                game_management_button(event_id),
            ],
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
                "remove_selected_status": _attendance_status_text(registration.attendance_status),
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
                    f"🗑 Игрок «{registration.display_name}» удалён из игры {_context_event_reference(context)}."
                ),
                buttons=[player_management_button(event_id)],
            ),
            idempotency_key=f"{message.update_id}:{Operation.REMOVE_PARTICIPANT.value}",
            target_participant_key=participant_key,
        )
        return BotResponse(
            text="⏳ Удаление принято в обработку.",
            buttons=[player_management_button(event_id)],
            deferred=True,
            command_enqueued=True,
        )

    async def _remove_waiting_confirmation(self, user: User, event: Event, *, invalid: str = "") -> BotResponse:
        count = await self.administration.waiting_registration_count(event.event_id)
        if count == 0:
            await self._clear(user)
            return BotResponse(
                text=(f"В игре {event_reference(event.name, event.game_date)} нет игроков, не подтвердивших участие."),
                buttons=[player_management_button(event.event_id)],
            )
        token = token_urlsafe(8)
        user.dialog_state = "ADMIN_REMOVE_WAITING_CONFIRM"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "game_date": event.game_date.isoformat() if event.game_date is not None else None,
            "remove_waiting_count": count,
            "remove_waiting_token": token,
        }
        prefix = f"{invalid}\n\n" if invalid else ""
        return BotResponse(
            text=(
                f"{prefix}⚠️ Вы действительно хотите удалить всех игроков, которые не подтвердили участие "
                f"в игре {event_reference(event.name, event.game_date)}?\n\nБудет удалено игроков: {count}."
            ),
            buttons=[
                Button(label="Да, удалить", value=f"admin:remove-waiting:confirm:{token}"),
                Button(label="Нет", value=f"ag:players:{event.event_id}"),
            ],
        )

    async def _admin_remove_waiting_confirm(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse:
        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
        token = str(user.dialog_context["remove_waiting_token"])
        if value != f"admin:remove-waiting:confirm:{token}":
            return await self._remove_waiting_confirmation(user, event, invalid="Удаление не подтверждено.")
        current_count = await self.administration.waiting_registration_count(event_id)
        expected_count = int(user.dialog_context["remove_waiting_count"])
        if current_count != expected_count:
            return await self._remove_waiting_confirmation(
                user,
                event,
                invalid="Список не подтвердивших участие изменился. Проверьте новое количество.",
            )
        await self._clear(user)
        await self.registrations.enqueue(
            operation=Operation.REMOVE_WAITING_PARTICIPANTS,
            event_id=event_id,
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EmptyPayload(),
            reply_context=ReplyContext(buttons=[player_management_button(event_id)]),
            idempotency_key=f"{message.update_id}:{Operation.REMOVE_WAITING_PARTICIPANTS.value}",
        )
        return BotResponse(
            text="⏳ Удаление не подтвердивших участие принято в обработку.",
            buttons=[player_management_button(event_id)],
            deferred=True,
            command_enqueued=True,
        )

    @staticmethod
    def _statistics_cursor(direction: str, event: Event) -> str:
        micros = int(event.created_at.timestamp() * 1_000_000)
        return f"stats:p:{direction}:{micros}:{event.event_id}"

    async def _statistics_menu(self, user: User) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        return BotResponse(
            text=MANAGE_STATISTICS,
            buttons=[
                Button(label=STATS_LINKS, value=STATS_LINKS),
                Button(label=STATS_ADD_GAME, value=STATS_ADD_GAME),
                admin_menu_button(),
            ],
        )

    async def _show_statistics_games(
        self,
        user: User,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        events = list(
            await self.events.list_page(
                after=after,
                before=before,
                archived=False,
                newest_first=True,
                limit=PAGE_SIZE,
            )
        )
        if not events:
            return BotResponse(
                text="Нет неархивированных игр для добавления в статистику.",
                buttons=_pagination_buttons(previous=None, following=None, cancel=MANAGE_STATISTICS),
            )
        first, last = events[0], events[-1]
        previous, following = await asyncio.gather(
            self.events.list_page(
                before=(first.created_at, first.event_id),
                archived=False,
                newest_first=True,
                limit=1,
            ),
            self.events.list_page(
                after=(last.created_at, last.event_id),
                archived=False,
                newest_first=True,
                limit=1,
            ),
        )
        buttons = [Button(label=event.name, value=f"stats:game:{event.event_id}") for event in events]
        buttons.extend(
            _pagination_buttons(
                previous=self._statistics_cursor("p", first) if previous else None,
                following=self._statistics_cursor("n", last) if following else None,
                cancel=MANAGE_STATISTICS,
            )
        )
        return BotResponse(text="Выберите неархивированную игру для добавления в статистику:", buttons=buttons)

    async def _statistics_action(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        parts = value.split(":", 4)
        if len(parts) == 5 and parts[:2] in (["stats", "p"], ["stats", "page"]):
            if parts[2] not in {"n", "p"} or not parts[3].isdigit():
                return BotResponse(text="Некорректная страница.")
            cursor = (datetime.fromtimestamp(int(parts[3]) / 1_000_000, UTC), parts[4])
            if parts[2] == "p":
                return await self._show_statistics_games(user, before=cursor)
            return await self._show_statistics_games(user, after=cursor)
        if len(parts) != 3 or parts[:2] != ["stats", "game"]:
            return BotResponse(text="Некорректное действие.")
        event = await self.events.get(parts[2])
        if event is None or event.archived_at is not None:
            return BotResponse(
                text="Игра не найдена или уже архивирована.",
                buttons=[Button(label=BACK, value=MANAGE_STATISTICS)],
            )
        return await self._enqueue_statistics(
            user,
            message,
            StatisticsPayload(action="game", game_id=event.event_id),
            back=Button(label=BACK, value=MANAGE_STATISTICS),
        )

    async def _show_admin_games(
        self,
        user: User,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        archived: bool = False,
    ) -> BotResponse:
        is_admin = await self._is_admin(user)
        if archived and not is_admin:
            return BotResponse(text="Недостаточно прав.")
        events = list(
            await self.events.list_page(
                after=after,
                before=before,
                archived=archived,
                newest_first=True,
                limit=PAGE_SIZE,
            )
        )
        if not events:
            buttons = []
            if not archived and is_admin:
                buttons.append(Button(label=ARCHIVED_GAMES, value=ARCHIVED_GAMES))
            buttons.extend(
                _pagination_buttons(
                    previous=None,
                    following=None,
                    cancel=MANAGE_GAMES if archived else ADMIN_MENU,
                )
            )
            return BotResponse(
                text="Архив игр пуст." if archived else "Игр для управления нет.",
                buttons=buttons,
            )

        first, last = events[0], events[-1]
        previous, following = await asyncio.gather(
            self.events.list_page(
                before=(first.created_at, first.event_id),
                archived=archived,
                newest_first=True,
                limit=1,
            ),
            self.events.list_page(
                after=(last.created_at, last.event_id),
                archived=archived,
                newest_first=True,
                limit=1,
            ),
        )
        buttons = [Button(label=event.name, value=f"ag:manage:{event.event_id}") for event in events]
        if not archived and is_admin:
            buttons.append(Button(label=ARCHIVED_GAMES, value=ARCHIVED_GAMES))
        buttons.extend(
            _pagination_buttons(
                previous=self._admin_game_cursor("p", first, archived=archived) if previous else None,
                following=self._admin_game_cursor("n", last, archived=archived) if following else None,
                cancel=MANAGE_GAMES if archived else ADMIN_MENU,
            )
        )
        title = ARCHIVED_GAMES if archived else MANAGE_GAMES
        return BotResponse(text=f"{title}\n\nВыберите игру:", buttons=buttons)

    async def _game_management(self, user: User, event: Event) -> BotResponse:
        if event.archived_at is not None:
            if not await self._is_admin(user):
                return BotResponse(
                    text="Игра не найдена.",
                    buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                )
            return BotResponse(
                text=(
                    f"Управление игрой {event_reference(event.name, event.game_date)}\n\n"
                    f"Количество игроков: {event.player_amount}\n"
                    "Статус: Архивирована\n\n"
                    "Разархивируйте игру, чтобы вернуть её в список управления."
                ),
                buttons=[
                    Button(label=UNARCHIVE_GAME, value=f"ag:unarchive:{event.event_id}"),
                    Button(label=BACK, value=ARCHIVED_GAMES),
                ],
            )
        is_leader, is_admin = await asyncio.gather(
            self._is_event_leader(user, event.event_id),
            self._is_admin(user),
        )
        text = (
            f"Управление игрой {event_reference(event.name, event.game_date)}\n\n"
            f"Количество игроков: {event.player_amount}\n"
            f"Статус: {EVENT_STATUS_LABELS[event.status]}"
        )
        if event.confirmation_deadline is not None:
            text += f"\nДедлайн подтверждения: {format_confirmation_deadline(event.confirmation_deadline)}"
        if not is_leader and not is_admin:
            return BotResponse(
                text=text + "\n\nАдминистративные действия доступны только мастерам этой игры.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )
        actions = [
            (CHANGE_STATUS, "status"),
            (CHANGE_PLAYER_AMOUNT, "player-amount"),
            (MANAGE_NOTIFICATIONS, "notifications"),
            (LIST_WAITING_PLAYERS, "waiting"),
            (EVENT_TABLES, "tables"),
            (MANAGE_PLAYERS, "players"),
            (MANAGE_MASTERS, "masters"),
            (ARCHIVE_GAME, "archive"),
        ]
        buttons = [Button(label=label, value=f"ag:{action}:{event.event_id}") for label, action in actions]
        buttons.append(Button(label=BACK, value=MANAGE_GAMES))
        return BotResponse(text=text + "\n\nВыберите действие:", buttons=buttons)

    @staticmethod
    def _notification_management(event: Event) -> BotResponse:
        return BotResponse(
            text=f"Уведомления игры {event_reference(event.name, event.game_date)}\n\nВыберите действие:",
            buttons=[
                Button(label=SEND_CONFIRMATION_REMINDER, value=f"ag:remind:{event.event_id}"),
                Button(label=SEND_CONFIRMED_LINK, value=f"ag:notify-link:{event.event_id}"),
                Button(label=SEND_CONFIRMED_NOTIFICATION, value=f"ag:notify-message:{event.event_id}"),
                game_management_button(event.event_id),
            ],
        )

    @staticmethod
    def _player_management(event: Event) -> BotResponse:
        return BotResponse(
            text=(f"Управление игроками игры {event_reference(event.name, event.game_date)}\n\nВыберите действие:"),
            buttons=[
                Button(label=REMOVE_PLAYER, value=f"ag:remove:{event.event_id}"),
                Button(label=REMOVE_WAITING_PLAYERS, value=f"ag:remove-waiting:{event.event_id}"),
                game_management_button(event.event_id),
            ],
        )

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
        if event.archived_at is not None and not await self._is_admin(user):
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
        if action == "manage":
            return await self._game_management(user, event)
        if action == "unarchive":
            if not await self._is_admin(user):
                await self._clear(user)
                return BotResponse(text="Недостаточно прав.")
            if event.archived_at is None:
                return BotResponse(
                    text=f"Игра {event_reference(event.name, event.game_date)} уже разархивирована.",
                    buttons=[game_management_button(event.event_id)],
                )
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.UNARCHIVE_EVENT,
                event_id=event.event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(
                    text_success=(
                        f"📤 Игра {event_reference(event.name, event.game_date)} разархивирована. "
                        "Регистрация остаётся закрытой."
                    ),
                    buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                ),
                idempotency_key=f"{message.update_id}:{Operation.UNARCHIVE_EVENT.value}",
            )
            return BotResponse(
                text="⏳ Разархивация принята в обработку.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                deferred=True,
                command_enqueued=True,
            )
        if event.archived_at is not None:
            return BotResponse(
                text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )
        denied = await self._event_leader_required(user, event.event_id)
        if denied is not None:
            denied.buttons = [Button(label=BACK, value=MANAGE_GAMES)]
            return denied

        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "game_date": event.game_date.isoformat() if event.game_date is not None else None,
            "event_status": event.status.value,
        }
        if action == "status":
            user.dialog_state = "ADMIN_STATUS_SELECT"
            return _admin_status_response(event.name, event.status, event.event_id, event.game_date)
        if action == "player-amount":
            user.dialog_state = "ADMIN_UPDATE_PLAYER_AMOUNT"
            return BotResponse(
                text=(
                    f"Текущее количество игроков: {event.player_amount}.\n\n"
                    f"Введите новое количество игроков (от 1 до {MAX_PLAYER_AMOUNT}):"
                ),
                buttons=[game_management_button(event.event_id)],
            )
        if action == "notifications":
            return self._notification_management(event)
        if action == "remind":
            await self._clear(user)
            if event.status is not EventStatus.CONFIRMATION_OPEN:
                return BotResponse(
                    text=(f"Подтверждение участия для игры {event_reference(event.name, event.game_date)} не открыто."),
                    buttons=[notification_management_button(event.event_id)],
                )
            if event.confirmation_deadline is None:
                return BotResponse(
                    text=(
                        f"Для игры {event_reference(event.name, event.game_date)} не задан дедлайн. "
                        "Снова откройте статус подтверждения и укажите дедлайн."
                    ),
                    buttons=[notification_management_button(event.event_id)],
                )
            back = notification_management_button(event.event_id)
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
        if action == "notify-link":
            user.dialog_state = "ADMIN_NOTIFICATION_LINK_TEXT"
            return _confirmed_notification_prompt(
                event.name,
                event.event_id,
                link=True,
                game_date=event.game_date,
            )
        if action in {"notify", "notify-message"}:
            user.dialog_state = "ADMIN_NOTIFICATION_MESSAGE_TEXT"
            return _confirmed_notification_prompt(
                event.name,
                event.event_id,
                link=False,
                game_date=event.game_date,
            )
        if action == "waiting":
            return await self._show_waiting_players(user, event)
        if action == "players":
            return self._player_management(event)
        if action == "tables":
            await self._clear(user)
            event = await self.administration.ensure_event_tables(event)
            await self.administration.create_pass_table(event.event_id)
            refreshed = await self.events.get(event.event_id)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
            return BotResponse(
                text=(f"Таблицы игры {event_reference(event.name, event.game_date)}\n\n{event_table_links(refreshed)}"),
                buttons=[game_management_button(event.event_id)],
            )
        if action == "remove":
            return await self._show_removable_players(user, event)
        if action == "remove-waiting":
            return await self._remove_waiting_confirmation(user, event)
        if action == "masters":
            return await self._event_masters_menu(user, event)
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

    async def _enqueue_statistics(
        self,
        user: User,
        message: InboundMessage,
        payload: StatisticsPayload,
        *,
        back: Button | None = None,
    ) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        back = back or admin_menu_button()
        command = OrderedRegistrationCommand(
            operation_id=str(
                uuid5(NAMESPACE_URL, f"stats:{message.identity.platform}:{user_id(user)}:{message.update_id}")
            ),
            event_id="statistics",
            operation=Operation.STATISTICS,
            platform=message.identity.platform,
            platform_user_id=user_id(user),
            payload=payload,
            reply_context=ReplyContext(buttons=[back]),
        )
        await self.registrations.publisher.publish(command)
        await self._clear(user)
        return BotResponse(
            text="⏳ Действие со статистикой принято в обработку.", buttons=[back], deferred=True, command_enqueued=True
        )

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

    async def _admin_start(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._has_admin_access(user):
            return BotResponse(text="Недостаточно прав.")
        if value == MANAGE_STATISTICS:
            return await self._statistics_menu(user)
        if value == STATS_LINKS:
            return await self._enqueue_statistics(
                user,
                message,
                StatisticsPayload(action="link"),
                back=Button(label=BACK, value=MANAGE_STATISTICS),
            )
        if value == STATS_ADD_GAME:
            return await self._show_statistics_games(user)
        if value == ARCHIVED_GAMES:
            return await self._show_admin_games(user, archived=True)
        if value == MANAGE_MODERATORS:
            return await self._moderator_management_menu(user)
        if value == "➕ Создать игру":
            user.dialog_state = "ADMIN_CREATE_NAME"
            user.dialog_context = {}
            return BotResponse(text="Введите название игры:")
        if value == MANAGE_GAMES or value in {
            CHANGE_STATUS,
            CHANGE_PLAYER_AMOUNT,
            MANAGE_NOTIFICATIONS,
            SEND_CONFIRMATION_REMINDER,
            SEND_CONFIRMED_LINK,
            SEND_CONFIRMED_NOTIFICATION,
            LEGACY_SEND_CONFIRMED_NOTIFICATION,
            LIST_WAITING_PLAYERS,
            CREATE_PASS_TABLE,
            LIST_PASS_TABLES,
            EVENT_TABLES,
            REMOVE_PLAYER,
            MANAGE_PLAYERS,
            REMOVE_WAITING_PLAYERS,
            ADD_EVENT_LEADER,
            ARCHIVE_GAME,
            LEGACY_DELETE_GAME,
            "📋 Список игр",
            *LEGACY_STATUS_ACTIONS,
        }:
            return await self._show_admin_games(user)
        if value == GRANT_GAMEMASTER:
            if not await self._is_admin(user):
                return BotResponse(text="Недостаточно прав.")
            user.dialog_state = "ADMIN_GAMEMASTER_PLATFORM"
            user.dialog_context = {}
            return BotResponse(
                text="Выберите бот, которым пользуется новый модератор:",
                buttons=[
                    Button(label="Telegram", value="admin:gamemaster:telegram"),
                    Button(label="VK", value="admin:gamemaster:vk"),
                    Button(label=BACK, value=MANAGE_MODERATORS),
                ],
            )
        if value == REVOKE_GAMEMASTER:
            return await self._show_moderators(user, "remove")
        if value == LIST_GAMEMASTERS:
            return await self._show_moderators(user, "list")
        return await self._admin_menu(user)

    # Admin submenu commands arrive while the user is IDLE, so extend the root dispatcher.
    async def dispatch_idle_admin(self, user: User, message: InboundMessage, value: str) -> BotResponse | None:
        if value in ADMIN_ACTIONS or value in LEGACY_STATUS_ACTIONS:
            return await self._admin_start(user, message, value)
        return None
