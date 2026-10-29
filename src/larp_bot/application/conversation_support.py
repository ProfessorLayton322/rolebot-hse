from __future__ import annotations

import logging
import re
from datetime import date

from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    BotResponse,
    Button,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    Platform,
    PrivilegedRole,
    TelegramUser,
    User,
    event_name_with_date,
    event_reference,
    format_game_date,
    normalize_telegram_handle,
    normalize_telegram_profile,
)

from .deadlines import (
    NEAREST_FRIDAY,
    NEAREST_THURSDAY,
    NEXT_NIGHT_03,
    NEXT_SATURDAY,
    TUESDAY_BEFORE_GAME_18,
)
from .navigation import MAIN_MENU_LABEL
from .services import (
    GameOverview,
    confirmed_notification_text,
    event_masters_text,
    player_notification_text,
    same_day_signup_warning,
)
from .table_links import master_table_links, pass_table_links, public_table_links

PROFILE = "📝 Профиль"
EDIT_PROFILE = "Редактировать профиль"
REFILL_PROFILE = "Заполнить профиль заново"
PROFILE_EDITED = "Профиль успешно изменён!"
REMOVE_NICKNAME = "Убрать никнейм"
ENLIST = "🎮 Записаться на игру"
CONFIRM = "✅ Подтвердить участие"
# Keep the former value recognizable for keyboards sent before this rollout.
CHARACTER = "🎭 Пожелания по персонажу"
GAME_WISHES = "🎭 Пожелания по игре"
CHARACTER_WISHES = "Пожелания по персонажу"
CO_PLAYER_WISHES = "Пожелания по соигрокам"
CO_PLAYER_WISH_SKIP = "wishes:co-player:skip"
CANCEL = "❌ Отменить участие"
MY_GAMES = "Мои игры"
ADMIN = "🛠 Администрирование"
BACK = "⬅️ Назад"
CANCEL_DIALOG = "Отмена"
PAGE_BACK = "Предыдущая страница"
PAGE_FORWARD = "Вперёд"
PAGE_SIZE = 5
NO_WISHES = "Без пожеланий"
KEEP = "Оставить без изменений"
SKIP = "Пропустить"
ENLIST_SKIP = "Пропустить и записаться"
NO_CO_PLAYER_WISH = "Без пожеланий"
CHANGE_STATUS = "🔄 Изменить статус"
CHANGE_PLAYER_AMOUNT = "👥 Изменить количество игроков"
ADD_POST_LINKS = "Добавить ссылки на посты"
EDIT_GAME = "Изменить игру"
SEND_CONFIRMATION_REMINDER = "🔔 Напомнить о подтверждении"
MANAGE_NOTIFICATIONS = "Уведомления"
MANAGE_SCHEDULED_EVENTS = "Запланированные события"
SCHEDULED_TIME_INVALID = "Некорректная дата и время. Минуты должны быть 00. Попробуйте ещё раз."
SEND_CONFIRMED_LINK = "Отправить подтвержденным ссылку на чат"
SEND_CONFIRMED_NOTIFICATION = "Отправить подтвержденным сообщение"
SEND_ALL_PLAYERS_NOTIFICATION = "Уведомить всех игроков"
LEGACY_SEND_CONFIRMED_NOTIFICATION = "📣 Уведомить подтвердивших"
LIST_WAITING_PLAYERS = "👥 Не подтвердившие участие"
REMOVE_PLAYER = "🗑 Удалить игрока"
MANAGE_PLAYERS = "Управление игроками"
LIST_PLAYER_PROFILES = "📋 Список профилей игроков"
PROMOTE_RESERVE_PLAYER = "Подвинуть игрока в основную сессию"
REMOVE_WAITING_PLAYERS = "Убрать не подтвердившихся в запас"
LEGACY_REMOVE_WAITING_PLAYERS = "Удалить не подтвердившихся"
CREATE_PASS_TABLE = "🎫 Создать таблицу пропусков"
LIST_PASS_TABLES = "🔗 Ссылки на таблицы пропусков"
GET_DAILY_PASSES = "Получить все пропуска за день"
DAILY_PASSES_ACTION = "admin:passes:daily"
DAILY_PASSES_DATE_PREFIX = "admin:passes:day:"
DAILY_PASSES_PAGE_PREFIX = "admin:passes:page:"
MANAGE_GAMES = "Управление играми"
ARCHIVED_GAMES = "Архив игр"
MANAGE_MODERATORS = "Управление ролями"
LEGACY_MANAGE_MODERATORS = "Управление модераторами"

LOGGER = logging.getLogger("larp_bot.application.conversation")
MANAGE_STATISTICS = "Управление статистикой"
STATS_LINKS = "Ссылки на таблицы статистики"
STATS_ADD_GAME = "Добавить игру в статистику"
STATS_CONFIRM_ADD_GAME = "Да, добавить в статистику"
EVENT_TABLES = "Таблицы участников и пропусков"
EXPORT_ACHIEVEMENTS = "Выгрузить ачивки"
GAME_FILES = "Файлы игры"
CHANGE_GAME_DETAILS = "Изменить название и дату"
GRANT_GAMEMASTER = "➕ Назначить модератора"
GRANT_GAMEMASTER_VIEWER = "➕ Модератор-наблюдатель"
GRANT_PASS_MANAGER = "➕ Менеджер пропусков"
REVOKE_GAMEMASTER = "➖ Снять роль"
LIST_GAMEMASTERS = "📋 Список ролей"
MANAGE_MASTERS = "Мастера"
ADD_EVENT_LEADER = "➕ Добавить мастера"
ADD_SELF_EVENT_LEADER = "➕ Добавить себя мастером"
REMOVE_EVENT_LEADER = "➖ Удалить мастера"
LIST_EVENT_LEADERS = "📋 Список мастеров"
CREATION_MASTER_QUESTION = "Хотите назначить первого мастера игры сейчас или позже?"
CREATE_AS_MASTER = "Назначить себя"
CREATE_WITH_OTHER_MASTER = "Выбрать другого мастера"
CREATE_WITHOUT_MASTER = "Назначить позже"
SCHEDULE_REGISTRATION_QUESTION = "Хотите ли вы запланировать отложенное открытие игры?"
SCHEDULE_CONFIRMATION_QUESTION = "Хотите ли вы запланировать открытие подтверждения участия?"
SCHEDULE_CLOSE_QUESTION = (
    "Хотите ли вы запланировать закрытие игры? Игра будет закрыта для регистрации, добавлена в статистику "
    "посещений и архивирована сама. Можете запланировать на ночь после игры, либо выбрать удобное вам время"
)
DECLINE_SCHEDULED_CLOSE = "Не хочу планировать закрытие"
ARCHIVE_GAME = "📦 Архивировать игру"
UNARCHIVE_GAME = "📤 Разархивировать игру"
DELETE_GAME = "🗑 Удалить игру"
LEGACY_DELETE_GAME = DELETE_GAME
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
    "Сначала заполните профиль и запишитесь на игру, даже если пока не уверены, что сможете прийти. "
    "У вас ещё будет время подтвердить или отменить участие — мы сообщим, когда откроется подтверждение."
)

REGISTRATION_OPEN_STATUSES = frozenset({EventStatus.CREATED, EventStatus.CONFIRMATION_OPEN})
EVENT_STATUS_LABELS = {
    EventStatus.CREATED: STATUS_REGISTRATION,
    EventStatus.CONFIRMATION_OPEN: STATUS_CONFIRMATION,
    EventStatus.CLOSED: STATUS_CLOSED,
}
PRIVILEGED_ROLE_LABELS = {
    PrivilegedRole.GAMEMASTER: "Модератор",
    PrivilegedRole.GAMEMASTER_VIEWER: "Модератор-наблюдатель",
    PrivilegedRole.PASS_MANAGER: "Менеджер пропусков",
    PrivilegedRole.ADMIN: "Администратор",
}
ASSIGNABLE_PRIVILEGED_ROLES = (
    PrivilegedRole.GAMEMASTER,
    PrivilegedRole.GAMEMASTER_VIEWER,
    PrivilegedRole.PASS_MANAGER,
)
ADMIN_STATUS_CHOICES = {
    "admin:status:registration": (STATUS_REGISTRATION, Operation.OPEN_REGISTRATION),
    "admin:status:confirmation": (STATUS_CONFIRMATION, Operation.OPEN_CONFIRMATION),
    "admin:status:closed": (STATUS_CLOSED, Operation.CLOSE_EVENT),
}
MASTER_TABLE_DISCLAIMER = "⚠️ Только для мастеров этой игры. Не пересылайте эту ссылку игрокам!"
VENUE_ADDRESS = "Старая Басманная улица, д, 21/4"
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
        GAME_FILES,
        EDIT_GAME,
        CHANGE_GAME_DETAILS,
        CHANGE_STATUS,
        CHANGE_PLAYER_AMOUNT,
        MANAGE_NOTIFICATIONS,
        MANAGE_SCHEDULED_EVENTS,
        SEND_CONFIRMATION_REMINDER,
        SEND_CONFIRMED_LINK,
        SEND_CONFIRMED_NOTIFICATION,
        SEND_ALL_PLAYERS_NOTIFICATION,
        LEGACY_SEND_CONFIRMED_NOTIFICATION,
        LIST_WAITING_PLAYERS,
        REMOVE_PLAYER,
        MANAGE_PLAYERS,
        LIST_PLAYER_PROFILES,
        PROMOTE_RESERVE_PLAYER,
        REMOVE_WAITING_PLAYERS,
        LEGACY_REMOVE_WAITING_PLAYERS,
        CREATE_PASS_TABLE,
        LIST_PASS_TABLES,
        DAILY_PASSES_ACTION,
        MANAGE_MODERATORS,
        LEGACY_MANAGE_MODERATORS,
        GRANT_GAMEMASTER,
        GRANT_GAMEMASTER_VIEWER,
        GRANT_PASS_MANAGER,
        REVOKE_GAMEMASTER,
        LIST_GAMEMASTERS,
        MANAGE_MASTERS,
        ADD_EVENT_LEADER,
        REMOVE_EVENT_LEADER,
        LIST_EVENT_LEADERS,
        ARCHIVE_GAME,
        UNARCHIVE_GAME,
        DELETE_GAME,
        "📋 Список игр",
    }
)
FREE_TEXT_DIALOG_STATES = frozenset(
    {
        "PROFILE_FULL_NAME_CYRILLIC",
        "PROFILE_NICKNAME",
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
        "ADMIN_CREATE_CLOSE_TIME",
        "ADMIN_CREATE_PLAYER_AMOUNT",
        "ADMIN_CREATE_MASTER_CHOICE",
        "ADMIN_CREATE_MASTER_PICK",
        "ADMIN_CREATE_MASTER_PLATFORM",
        "ADMIN_CREATE_MASTER_PROFILE",
        "ADMIN_CREATE_MASTER_PROMOTE_CONFIRM",
        "ADMIN_CREATE_SCHEDULE_CHOICE",
        "ADMIN_CREATE_SCHEDULE_TIME",
        "ADMIN_CREATE_CONFIRMATION_SCHEDULE_CHOICE",
        "ADMIN_CREATE_CONFIRMATION_SCHEDULE_TIME",
        "ADMIN_CREATE_CONFIRMATION_DEADLINE",
        "ADMIN_SCHEDULE_REGISTRATION_TIME",
        "ADMIN_SCHEDULE_CONFIRMATION_TIME",
        "ADMIN_SCHEDULE_CONFIRMATION_DEADLINE",
        "ADMIN_SCHEDULE_CLOSE_TIME",
        "ADMIN_UPDATE_PLAYER_AMOUNT",
        "ADMIN_UPDATE_GAME_NAME",
        "ADMIN_UPDATE_GAME_DATE",
        "ADMIN_POST_LINK_TELEGRAM",
        "ADMIN_POST_LINK_VK",
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
        "ADMIN_EVENT_LEADER_PROMOTE_CONFIRM",
        "ADMIN_EVENT_LEADER_REMOVE",
        "ADMIN_EVENT_LEADER_REMOVE_CONFIRM",
        "ADMIN_EVENT_LEADER_LIST",
        "ADMIN_ARCHIVE_NAME",
        "ADMIN_DELETE_NAME",
        "ADMIN_REMOVE_PICK",
        "ADMIN_REMOVE_CONFIRM",
        "ADMIN_REMOVE_WAITING_CONFIRM",
        "ADMIN_WAITING_PICK",
        "ADMIN_PROFILES_PICK",
        "ADMIN_RESERVE_PROMOTION_PICK",
        "ADMIN_RESERVE_PROMOTION_CONFIRM",
    }
)

PROFILE_FORMAT = "combined-full-name-v2-statistics-experience"
CYRILLIC_FULL_NAME_PROMPT = "Введите ФИО кириллицей (фамилию имя, если отчества нету):"
LATIN_FULL_NAME_PROMPT = "Введите ФИО латиницей (фамилию имя, если отчества нету):"
CYRILLIC_FULL_NAME_RETRY_PROMPT = "Введите ФИО кириллицей, либо фамилию имя, если отчества нету"
LATIN_FULL_NAME_RETRY_PROMPT = "Введите ФИО латиницей, либо фамилию имя, если отчества нету"
PHONE_PROMPT = "Введите ваш номер телефона. Он необходим для оформления пропуска."
EMAIL_PROMPT = "Введите ваш email. Он необходим для оформления пропуска."
NICKNAME_PROMPT = (
    "Ваш никнейм - то, как вы будете отображаться в публичных таблицах записи на игры. "
    "Важно - организатором будет видно введенное вами паспортное имя!\n\n"
    "Пожалуйста, используйте узнаваемый никнейм. Если хотите, можете добавить в никнейм ваше имя, "
    'например "Канеки Кен/Eyepatch".'
)

PROFILE_EDIT_ACTIONS = {
    "profile:field:name": "ФИО",
    "profile:field:nickname": "Никнейм",
    "profile:field:crossplay": "Кросспол",
    "profile:field:pass": "Пропуск",
    "profile:field:phone": "Номер телефона",
    "profile:field:email": "Email",
    "profile:field:citizenship": "Гражданство",
    "profile:field:latin-name": "ФИО латиницей",
}


def dialog_exit_button(*, label: str = MAIN_MENU_LABEL) -> Button:
    return Button(label=label, value=CANCEL_DIALOG)


def profile_dialog_exit_button(user: User) -> Button:
    editing = user.dialog_context.get("profile_flow") in {"field", "reset"}
    return dialog_exit_button(label=CANCEL_DIALOG if editing else MAIN_MENU_LABEL)


def profile_yes_no_buttons(user: User) -> list[Button]:
    return [
        Button(label="Да", value="Да"),
        Button(label="Нет", value="Нет"),
        profile_dialog_exit_button(user),
    ]


def nickname_buttons(user: User) -> list[Button]:
    buttons = []
    if user.nickname is not None:
        buttons.append(Button(label=REMOVE_NICKNAME, value="profile:nickname:clear"))
    buttons.append(profile_dialog_exit_button(user))
    return buttons


def main_buttons(has_admin_access: bool, *, has_registrations: bool, has_available_games: bool) -> list[Button]:
    labels = [PROFILE]
    if has_available_games:
        labels.append(ENLIST)
    labels.append(CONFIRM)
    if has_registrations:
        labels.extend([GAME_WISHES, CANCEL, MY_GAMES])
    if has_admin_access:
        labels.append(ADMIN)
    return [Button(label=label, value=label) for label in labels]


def event_table_links(event: Event) -> str:
    if event.public_table_public_url is None:
        raise RuntimeError("public game table is not initialized")
    if event.pass_table_public_url is None:
        raise RuntimeError("pass table is not initialized")
    return (
        "Публичная таблица (без контактов Telegram и VK):\n"
        f"{public_table_links(event).for_admin()}\n\n"
        f"Административная таблица:\n{MASTER_TABLE_DISCLAIMER}\n"
        f"{master_table_links(event).for_admin()}\n\n"
        f"Таблица пропусков:\n{pass_table_links(event).for_admin()}"
    )


def user_id(user: User) -> int:
    return user.tg_id if isinstance(user, TelegramUser) else user.vk_id


def is_profile_complete(user: User) -> bool:
    return user.profile_complete


def _start_command(value: str, message: InboundMessage) -> tuple[bool, str | None]:
    if message.identity.platform is Platform.VK:
        return re.fullmatch(r"(?:Начать|/start)", value) is not None, None
    match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]{5,32})?(?:[ =](\S+))?", value)
    if match is None:
        return False, None
    parameter = match.group(1)
    if parameter is None or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parameter) is None:
        return True, None
    return True, parameter


def _bool_text(value: bool | None) -> str:
    return "Да" if value else "Нет"


def _pass_name(parts: tuple[str | None, str | None, str | None]) -> str:
    return " ".join(part for part in parts if part not in {None, "-"})


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
        "mine": "Выберите игру, чтобы посмотреть информацию о ней:",
    }[flow]


def _no_games_for_action_text(flow: str) -> str:
    return {
        "enlist": "Сейчас нет игр, доступных для записи.",
        "confirm": "Сейчас нет игр, доступных для подтверждения участия.",
        "character": "Сейчас нет игр, доступных для изменения пожеланий.",
        "wishes": "Нет игр, на которые вы записаны.",
        "cancel": "Сейчас нет игр, доступных для отмены участия.",
        "mine": "Сейчас нет игр, доступных для просмотра.",
    }[flow]


def _event_buttons(events: list[Event], flow: str) -> list[Button]:
    return [
        Button(label=event_name_with_date(event.name, event.game_date), value=f"select:{flow}:{event.event_id}")
        for event in events
    ]


def _signup_game_text(event: Event, platform: Platform) -> str:
    game_date = format_game_date(event.game_date) if event.game_date is not None else "не указана"
    post_url = event.telegram_post_url if platform is Platform.TELEGRAM else event.vk_post_url
    return (
        f"Это меню записи на игру «{event.name}» ({game_date}).\n\n"
        f"Игра: «{event.name}»\n"
        f"Дата: {game_date}\n"
        f"Пост об игре: {post_url or 'ссылка пока не добавлена'}"
    )


def _my_game_text(
    event: Event,
    overview: GameOverview,
    masters: list[tuple[BotIdentity, User | None]],
) -> str:
    statuses = ["Мастер"] if overview.participation.is_leader else []
    registration = overview.participation.registration
    if registration is not None:
        roster = "основной состав" if overview.in_main_roster else "запас"
        statuses.append(f"{_attendance_status_text(registration.attendance_status)}, {roster}")
    game_date = format_game_date(event.game_date) if event.game_date is not None else "не указана"
    status = "; ".join(statuses)
    text = f"Игра: «{event.name}»\nДата: {game_date}\nВаш статус: {status}"
    masters_text = event_masters_text(masters)
    if masters_text:
        text = f"{text}\n\n{masters_text}"
    links = [f"Чат игры: {link}" for link in overview.chat_links]
    if event.public_table_public_url is not None:
        links.append(f"Таблица участников: {public_table_links(event).for_player()}")
    if not links:
        return text
    return f"{text}\n\n" + "\n".join(links)


def co_player_wish_prompt(
    *,
    skip_value: str,
    skip_label: str = SKIP,
    same_day_warning_date: date | None = None,
) -> BotResponse:
    warning = f"{same_day_signup_warning(same_day_warning_date)}\n\n" if same_day_warning_date is not None else ""
    return BotResponse(
        text=(
            f"{warning}"
            "Есть ли игроки, с которыми вы хотели бы играть в одной сессии? "
            "Укажите каждого как «Фамилия Имя», через запятую. "
            "Мастера постараются учесть пожелание, но не могут его гарантировать.\n\n"
            f"Если пожеланий нет, нажмите «{skip_label}»."
        ),
        buttons=[Button(label=skip_label, value=skip_value), dialog_exit_button()],
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
            Button(
                label="Да, я хочу участвовать в этой игре, а не в другой",
                value="confirm:replace:yes",
            ),
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


def edit_game_button(event_id: str) -> Button:
    return Button(label=BACK, value=f"ag:edit:{event_id}")


def game_files_button(event_id: str) -> Button:
    return Button(label=BACK, value=f"ag:files:{event_id}")


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
            f"{STATUS_CLOSED} — игроки не могут ни записываться, ни подтверждать участие. "
            "Закрытую регистрацию можно открыть снова, но закрывайте её только после того, "
            "как игровая сессия уже собрана."
        ),
        buttons=[Button(label=label, value=value) for value, (label, _) in ADMIN_STATUS_CHOICES.items()]
        + [edit_game_button(event_id)],
    )


def _confirmation_deadline_response(event_id: str, *, invalid: bool = False) -> BotResponse:
    prefix = "Некорректная дата и время. Попробуйте ещё раз.\n\n" if invalid else ""
    return BotResponse(
        text=(
            f"{prefix}Введите дедлайн подтверждения строго в формате DD.MM.YY HH:MM или нажмите «{NEAREST_THURSDAY}»."
        ),
        buttons=[
            Button(label=NEAREST_THURSDAY, value="admin:deadline:nearest-thursday"),
            edit_game_button(event_id),
        ],
    )


def _game_date_response(*, invalid: bool = False) -> BotResponse:
    prefix = "Некорректная дата. Попробуйте ещё раз.\n\n" if invalid else ""
    return BotResponse(
        text=f"{prefix}Введите дату игры строго в формате DD.MM.YY или нажмите «{NEXT_SATURDAY}».",
        buttons=[Button(label=NEXT_SATURDAY, value="admin:date:next-saturday")],
    )


def _game_details_date_response(event_id: str, current_date: date | None, *, invalid: bool = False) -> BotResponse:
    prefix = "Некорректная дата. Попробуйте ещё раз.\n\n" if invalid else ""
    formatted = format_game_date(current_date) if current_date is not None else "не указана"
    return BotResponse(
        text=(
            f"{prefix}Текущая дата игры: {formatted}.\n\n"
            f"Введите новую дату строго в формате DD.MM.YY или нажмите «{NEXT_SATURDAY}»."
        ),
        buttons=[
            Button(label=NEXT_SATURDAY, value="admin:details:next-saturday"),
            edit_game_button(event_id),
        ],
    )


def _registration_schedule_response(
    *,
    invalid: bool = False,
    past: bool = False,
    after_close: bool = False,
) -> BotResponse:
    if after_close:
        prefix = "Регистрация должна открываться строго до закрытия игры.\n\n"
    elif past:
        prefix = "Время открытия должно быть строго позже текущего московского времени.\n\n"
    elif invalid:
        prefix = f"{SCHEDULED_TIME_INVALID}\n\n"
    else:
        prefix = ""
    return BotResponse(
        text=(
            f"{prefix}Введите время публикации строго в формате DD.MM.YY HH:MM "
            f"или нажмите «{NEAREST_FRIDAY}». Минуты должны быть 00. Время указывается по Москве."
        ),
        buttons=[
            Button(label=NEAREST_FRIDAY, value="create:schedule:nearest-friday"),
            Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
        ],
    )


def _scheduled_close_response(
    *,
    invalid: bool = False,
    not_after_game: bool = False,
    past: bool = False,
    confirmation_conflict: bool = False,
    registration_conflict: bool = False,
    next_night_value: str = "create:close:next-night",
    cancel_value: str = "create:close:cancel",
    back: Button | None = None,
) -> BotResponse:
    if registration_conflict:
        prefix = "Закрытие должно быть позже открытия регистрации.\n\n"
    elif confirmation_conflict:
        prefix = "Закрытие должно быть позже дедлайна подтверждения.\n\n"
    elif not_after_game:
        prefix = "Закрытие должно быть запланировано строго после даты игры.\n\n"
    elif past:
        prefix = "Закрытие должно быть запланировано на будущее время.\n\n"
    elif invalid:
        prefix = f"{SCHEDULED_TIME_INVALID}\n\n"
    else:
        prefix = ""
    return BotResponse(
        text=(
            f"{prefix}{SCHEDULE_CLOSE_QUESTION}\n\n"
            "Введите время строго в формате DD.MM.YY HH:MM. Минуты должны быть 00. "
            "Время указывается по Москве."
        ),
        buttons=[
            Button(label=NEXT_NIGHT_03, value=next_night_value),
            Button(label=DECLINE_SCHEDULED_CLOSE, value=cancel_value),
            *([] if back is None else [back]),
        ],
    )


def _scheduled_confirmation_time_response(
    *,
    back: Button,
    tuesday_value: str | None = None,
    invalid: str = "",
) -> BotResponse:
    prefix = f"{invalid}\n\n" if invalid else ""
    shortcut = f" или нажмите «{TUESDAY_BEFORE_GAME_18}»" if tuesday_value is not None else ""
    buttons = [] if tuesday_value is None else [Button(label=TUESDAY_BEFORE_GAME_18, value=tuesday_value)]
    buttons.append(back)
    return BotResponse(
        text=(
            f"{prefix}Введите время открытия подтверждения строго в формате DD.MM.YY HH:MM{shortcut}. "
            "Минуты должны быть 00. Время указывается по Москве."
        ),
        buttons=buttons,
    )


def _scheduled_confirmation_deadline_response(
    *,
    back: Button,
    invalid: str = "",
) -> BotResponse:
    prefix = f"{invalid}\n\n" if invalid else ""
    return BotResponse(
        text=(
            f"{prefix}Введите дедлайн подтверждения строго в формате DD.MM.YY HH:MM. "
            "Он должен быть позже открытия подтверждения. Время указывается по Москве."
        ),
        buttons=[back],
    )


def _confirmed_notification_prompt(
    event_name: str,
    event_id: str,
    *,
    link: bool,
    all_players: bool = False,
    game_date: date | None = None,
    invalid: str = "",
) -> BotResponse:
    prefix = f"{invalid}\n\n" if invalid else ""
    reference = event_reference(event_name, game_date)
    if all_players:
        prompt = (
            "Эта кнопка нужна для рассылки сообщения всем записавшимся на игру игрокам: "
            "ожидающим подтверждения и подтвердившим участие.\n\n"
            f"Отправьте текст сообщения для всех игроков игры {reference}."
        )
    elif link:
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
    *,
    all_players: bool = False,
) -> BotResponse:
    final_text = (
        player_notification_text(event_name, message, game_date)
        if all_players
        else confirmed_notification_text(event_name, message, game_date)
    )
    return BotResponse(
        text=(f"Получателей: {recipient_count}\n\nИтоговое сообщение:\n\n{final_text}"),
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
