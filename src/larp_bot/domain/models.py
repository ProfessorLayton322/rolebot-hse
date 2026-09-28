from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


DEFAULT_PLAYER_AMOUNT = 30
MAX_PLAYER_AMOUNT = 10_000
# Event listings are keyset-paged and storage rejects larger pages, so callers
# that scan every game must walk the pages instead of asking for one big page.
MAX_EVENT_PAGE_SIZE = 10


def format_game_date(value: date) -> str:
    return value.strftime("%d.%m.%y")


def parse_game_date(value: str) -> date:
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{2})", value.strip())
    if match is None:
        raise ValueError("date must use DD.MM.YY")
    day, month, year = map(int, match.groups())
    try:
        return date(2000 + year, month, day)
    except ValueError:
        raise ValueError("date is not a valid calendar day") from None


def event_name_with_date(name: str, game_date: date | None) -> str:
    return f"{name} ({format_game_date(game_date)})" if game_date is not None else name


def event_reference(name: str, game_date: date | None) -> str:
    suffix = f" ({format_game_date(game_date)})" if game_date is not None else ""
    return f"«{name}»{suffix}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


PassEmail = Annotated[str, StringConstraints(max_length=254, pattern=r"^[^=\s@][^\s@]*@[^\s@]+\.[^\s@]+$")]
Nickname = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[^\r\n]+$"),
]
_PASS_EMAIL_ADAPTER = TypeAdapter(PassEmail)
_NICKNAME_ADAPTER = TypeAdapter(Nickname)
_CYRILLIC_NAME_RE = re.compile(r"^[А-ЯЁа-яё](?:[А-ЯЁа-яё -]{0,98}[А-ЯЁа-яё])?$")
_LATIN_NAME_RE = re.compile(r"^[A-Za-z](?:[A-Za-z '\-]{0,98}[A-Za-z])?$")
_CYRILLIC_FULL_NAME_PART_RE = re.compile(r"^[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*$")
_LATIN_FULL_NAME_PART_RE = re.compile(r"^[A-Za-z]+(?:[-'][A-Za-z]+)*$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{7,23}$")


def validate_pass_email(value: str) -> str:
    """Validate an email before a dialogue is allowed to leave its email step."""

    return _PASS_EMAIL_ADAPTER.validate_python(value.strip())


def normalize_nickname(value: str) -> str:
    """Normalize a one-line public nickname entered in the profile editor."""

    return _NICKNAME_ADAPTER.validate_python(value)


def validate_cyrillic_name(value: str, *, patronym: bool = False) -> str:
    clean = value.strip()
    if patronym and clean == "-":
        return clean
    if not _CYRILLIC_NAME_RE.fullmatch(clean):
        raise ValueError("Используйте только кириллицу")
    return clean


def validate_latin_name(value: str, *, patronym: bool = False) -> str:
    clean = value.strip()
    if patronym and clean == "-":
        return clean
    if not _LATIN_NAME_RE.fullmatch(clean):
        raise ValueError("Используйте только латиницу")
    return clean


def parse_full_name(
    value: str,
    *,
    alphabet: Literal["cyrillic", "latin"],
) -> tuple[str, str] | tuple[str, str, str]:
    """Split a two- or three-part full name without treating hyphens as separators."""

    parts = value.split()
    pattern = _CYRILLIC_FULL_NAME_PART_RE if alphabet == "cyrillic" else _LATIN_FULL_NAME_PART_RE
    if (
        len(parts) not in {2, 3}
        or len(" ".join(parts)) > 300
        or any(len(part) > 100 or pattern.fullmatch(part) is None for part in parts)
    ):
        raise ValueError(f"Введите ФИО {alphabet}")
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], parts[1], parts[2]


def surname_and_name(value: str) -> str:
    """Return the two name parts allowed outside pass data."""

    return " ".join(value.split()[:2])


def normalize_mobile_phone(value: str) -> str:
    clean = value.strip()
    if not _PHONE_RE.fullmatch(clean) or not 10 <= sum(character.isdigit() for character in clean) <= 15:
        raise ValueError("Некорректный номер телефона")
    return clean


class Platform(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"
    SYSTEM = "system"


class PrivilegedRole(StrEnum):
    """Ordered global bot roles, using the established internal role names."""

    GAMEMASTER = "gamemaster"
    GAMEMASTER_VIEWER = "gamemaster_viewer"
    PASS_MANAGER = "pass_manager"
    ADMIN = "admin"

    @property
    def level(self) -> int:
        return {
            PrivilegedRole.GAMEMASTER: 1,
            PrivilegedRole.GAMEMASTER_VIEWER: 2,
            PrivilegedRole.PASS_MANAGER: 3,
            PrivilegedRole.ADMIN: 4,
        }[self]

    def includes(self, required: PrivilegedRole) -> bool:
        return self.level >= required.level


class UnreachableRecipient(RuntimeError):
    """One recipient can never receive bot messages on their platform.

    Telegram and VK reject a send permanently when the person blocked the bot,
    deleted their account, or never allowed messages from the community. Every
    retry receives the same rejection, so an ordered command must record that
    recipient as done instead of failing; otherwise it keeps its FIFO message
    and blocks every later command for the same game.
    """


class EventStatus(StrEnum):
    CREATED = "CREATED"
    CONFIRMATION_OPEN = "CONFIRMATION_OPEN"
    CLOSED = "CLOSED"


class AttendanceStatus(StrEnum):
    WAITING = "Ожидается"
    CONFIRMED = "Подтверждено"
    CANCELLED = "Отменено"


class Operation(StrEnum):
    STATISTICS = "STATISTICS"
    SYNC_PROFILE = "SYNC_PROFILE"
    ENLIST = "ENLIST"
    UPDATE_PROFILE = "UPDATE_PROFILE"
    CONFIRM = "CONFIRM"
    UPDATE_CHARACTER_WISH = "UPDATE_CHARACTER_WISH"
    UPDATE_CO_PLAYER_WISH = "UPDATE_CO_PLAYER_WISH"
    CANCEL = "CANCEL"
    OPEN_REGISTRATION = "OPEN_REGISTRATION"
    OPEN_CONFIRMATION = "OPEN_CONFIRMATION"
    SCHEDULED_OPEN_CONFIRMATION = "SCHEDULED_OPEN_CONFIRMATION"
    SEND_CONFIRMATION_REMINDER = "SEND_CONFIRMATION_REMINDER"
    SEND_CONFIRMED_NOTIFICATION = "SEND_CONFIRMED_NOTIFICATION"
    SEND_ALL_PLAYERS_NOTIFICATION = "SEND_ALL_PLAYERS_NOTIFICATION"
    UPDATE_EVENT_DETAILS = "UPDATE_EVENT_DETAILS"
    UPDATE_PLAYER_AMOUNT = "UPDATE_PLAYER_AMOUNT"
    REMOVE_PARTICIPANT = "REMOVE_PARTICIPANT"
    REMOVE_WAITING_PARTICIPANTS = "REMOVE_WAITING_PARTICIPANTS"
    CLOSE_EVENT = "CLOSE_EVENT"
    ARCHIVE_EVENT = "ARCHIVE_EVENT"
    SCHEDULED_CLOSE_EVENT = "SCHEDULED_CLOSE_EVENT"
    SCHEDULED_CLOSE_STATISTICS = "SCHEDULED_CLOSE_STATISTICS"
    SCHEDULED_CLOSE_FINALIZE = "SCHEDULED_CLOSE_FINALIZE"
    UNARCHIVE_EVENT = "UNARCHIVE_EVENT"
    DELETE_EVENT = "DELETE_EVENT"


class Button(StrictModel):
    label: str
    value: str


class BotIdentity(StrictModel):
    platform: Platform
    platform_user_id: int = Field(gt=0)


class PassDetails(StrictModel):
    surname_cyrillic: str = Field(min_length=1, max_length=100)
    name_cyrillic: str = Field(min_length=1, max_length=100)
    patronym_cyrillic: str = Field(min_length=1, max_length=100)
    foreigner: bool
    surname_latin: str | None = Field(default=None, min_length=1, max_length=100)
    name_latin: str | None = Field(default=None, min_length=1, max_length=100)
    patronym_latin: str | None = Field(default=None, min_length=1, max_length=100)
    mobile_phone: str = Field(min_length=8, max_length=25)
    email: PassEmail

    @field_validator("surname_cyrillic", "name_cyrillic")
    @classmethod
    def validate_cyrillic_part(cls, value: str) -> str:
        return validate_cyrillic_name(value)

    @field_validator("patronym_cyrillic")
    @classmethod
    def validate_cyrillic_patronym(cls, value: str) -> str:
        return validate_cyrillic_name(value, patronym=True)

    @field_validator("surname_latin", "name_latin")
    @classmethod
    def validate_latin_part(cls, value: str | None) -> str | None:
        return None if value is None else validate_latin_name(value)

    @field_validator("patronym_latin")
    @classmethod
    def validate_latin_patronym(cls, value: str | None) -> str | None:
        return None if value is None else validate_latin_name(value, patronym=True)

    @field_validator("mobile_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_mobile_phone(value)

    @model_validator(mode="after")
    def names_match_citizenship(self) -> PassDetails:
        latin = (self.surname_latin, self.name_latin, self.patronym_latin)
        if not self.foreigner and any(value is not None for value in latin):
            raise ValueError("latin name fields must be empty for a Russian citizen")
        if self.foreigner and any(value is None for value in latin):
            raise ValueError("latin name fields are required for a foreign citizen")
        if self.foreigner and (self.patronym_cyrillic == "-") != (self.patronym_latin == "-"):
            raise ValueError("both patronym fields must use '-' when a foreign citizen has no patronym")
        return self


_VK_HOSTS = {"vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"}
_VK_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]{1,100}/?$")


def normalize_vk_url(value: str) -> str:
    from urllib.parse import urlsplit

    candidate = value.strip()
    if not candidate or candidate == "-":
        raise ValueError("Укажите ссылку на страницу VK")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname not in _VK_HOSTS:
        raise ValueError("Нужна HTTPS-ссылка на vk.com или vk.ru")
    if not _VK_PATH_RE.fullmatch(parsed.path) or parsed.query or parsed.fragment:
        raise ValueError("Некорректный адрес страницы VK")
    return f"https://vk.com/{parsed.path.strip('/')}"


def normalize_telegram_handle(value: str | None) -> str | None:
    if value is None or value.strip() in {"", "-", "Пропустить"}:
        return None
    candidate = value.strip().removeprefix("@").casefold()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", candidate):
        raise ValueError("Некорректный Telegram username")
    return f"@{candidate}"


_TELEGRAM_PROFILE_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
_VK_TELEGRAM_MENTION_RE = re.compile(r"^\[id[1-9][0-9]*\|(?P<handle>@[A-Za-z0-9_]{5,32})\]$")
_TELEGRAM_POST_PATH_RE = re.compile(r"^/(?:s/)?(?:[A-Za-z][A-Za-z0-9_]{3,31}/[1-9][0-9]*|c/[1-9][0-9]*/[1-9][0-9]*)/?$")
_VK_POST_PATH_RE = re.compile(r"^/wall-?[1-9][0-9]*_[1-9][0-9]*/?$")


def normalize_telegram_profile(value: str) -> str:
    """Normalize an admin-supplied Telegram @handle or public profile URL."""

    candidate = value.strip()
    vk_mention = _VK_TELEGRAM_MENTION_RE.fullmatch(candidate)
    if vk_mention is not None:
        candidate = vk_mention.group("handle")
    if candidate.startswith("@"):
        handle = normalize_telegram_handle(candidate)
        assert handle is not None
        return handle
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Нужна ссылка t.me или @username") from None
    if (
        parsed.scheme != "https"
        or hostname not in _TELEGRAM_PROFILE_HOSTS
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Нужна ссылка t.me или @username")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 1:
        raise ValueError("Некорректная ссылка на профиль Telegram")
    handle = normalize_telegram_handle(path_parts[0])
    if handle is None:
        raise ValueError("Некорректная ссылка на профиль Telegram")
    return handle


def _post_url(value: str, *, hosts: set[str], path_pattern: re.Pattern[str], error: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(error) from None
    if (
        parsed.scheme != "https"
        or hostname not in hosts
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or path_pattern.fullmatch(parsed.path) is None
    ):
        raise ValueError(error)
    return f"https://{hostname.removeprefix('www.')}{parsed.path.rstrip('/')}"


def normalize_telegram_post_url(value: str) -> str:
    """Normalize a link to an individual Telegram channel post."""

    return _post_url(
        value,
        hosts=_TELEGRAM_PROFILE_HOSTS,
        path_pattern=_TELEGRAM_POST_PATH_RE,
        error="Нужна HTTPS-ссылка на пост в Telegram",
    )


def normalize_vk_post_url(value: str) -> str:
    """Normalize a link to an individual VK wall post."""

    return (
        _post_url(
            value,
            hosts=_VK_HOSTS,
            path_pattern=_VK_POST_PATH_RE,
            error="Нужна HTTPS-ссылка на пост во VK",
        )
        .replace("https://vk.ru/", "https://vk.com/")
        .replace("https://m.vk.com/", "https://vk.com/")
        .replace("https://m.vk.ru/", "https://vk.com/")
    )


class UserBase(StrictModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=300)
    nickname: Nickname | None = None
    crossplay: bool | None = None
    larp_experience: bool | None = None
    needs_pass: bool | None = None
    pass_details: PassDetails | None = None
    dialog_state: str = "IDLE"
    dialog_context: dict[str, Any] = Field(default_factory=dict)
    last_update_id: str | None = None
    last_update_at: datetime | None = None
    last_delivery_operation_id: str | None = None
    last_bot_buttons: list[Button] = Field(default_factory=list)
    # ``is_gamemaster`` predates the role hierarchy. Keep it as a migration
    # fallback until every production row has received ``privileged_role``.
    is_gamemaster: bool = False
    privileged_role: PrivilegedRole | None = None
    gamemaster_grant_operation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def pass_details_match_choice(self) -> UserBase:
        if self.needs_pass is False and self.pass_details is not None:
            raise ValueError("pass_details must be empty when no pass is needed")
        return self

    @property
    def common_profile_complete(self) -> bool:
        basics = self.full_name is not None and self.crossplay is not None and self.needs_pass is not None
        return basics and (not self.needs_pass or self.pass_details is not None)

    @property
    def effective_privileged_role(self) -> PrivilegedRole | None:
        if self.privileged_role is not None:
            return self.privileged_role
        return PrivilegedRole.GAMEMASTER if self.is_gamemaster else None

    def has_privileged_role(self, required: PrivilegedRole) -> bool:
        role = self.effective_privileged_role
        return role is not None and role.includes(required)


class TelegramUser(UserBase):
    tg_id: int = Field(gt=0)
    vk_url: str | None = None
    # Numeric VK identity resolved from ``vk_url``. Keeping it next to the
    # Telegram-local profile lets reciprocal links work for vanity VK URLs
    # without changing or merging either local user row.
    linked_vk_id: int | None = Field(default=None, gt=0)
    telegram_handle: str | None = None

    @field_validator("vk_url")
    @classmethod
    def validate_vk_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_vk_url(value)

    @field_validator("telegram_handle")
    @classmethod
    def validate_handle(cls, value: str | None) -> str | None:
        return normalize_telegram_handle(value)

    @property
    def profile_complete(self) -> bool:
        return self.common_profile_complete and self.vk_url is not None


class VkUser(UserBase):
    vk_id: int = Field(gt=0)
    telegram_handle: str | None = None

    @field_validator("telegram_handle")
    @classmethod
    def validate_handle(cls, value: str | None) -> str | None:
        return normalize_telegram_handle(value)

    @property
    def profile_complete(self) -> bool:
        return self.common_profile_complete


User = TelegramUser | VkUser


class Event(StrictModel):
    event_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=200)
    # The nullable storage column deliberately leaves every pre-rollout,
    # archived game without an invented date. The creation dialogue requires
    # this field for all new games.
    game_date: date | None = None
    # Existing events predate the capacity field. They retain a conservative
    # default while every newly created game must collect an explicit value.
    player_amount: int = Field(default=DEFAULT_PLAYER_AMOUNT, ge=1, le=MAX_PLAYER_AMOUNT)
    disk_resource_path: str = Field(pattern=r"^disk:/larp-bot/events/.+\.xlsx$")
    public_registration_url: str
    public_table_resource_path: str | None = Field(default=None, pattern=r"^disk:/larp-bot/events/.+\.xlsx$")
    public_table_public_url: str | None = None
    status: EventStatus = EventStatus.CREATED
    # Null means immediate publication. A future timestamp hides registration
    # from players until that instant while leaving it manageable by masters.
    registration_open_at: datetime | None = None
    # Scheduled-opening notifications are maintained independently from the
    # timestamp gate so notification delivery can never delay registration.
    registration_open_notified_recipients: list[str] = Field(default_factory=list)
    registration_open_notification_completed_at: datetime | None = None
    # The confirmation deadline is collected together with this timestamp.
    # Once the timer opens confirmation, this field is cleared while the
    # deadline remains available to players.
    confirmation_open_at: datetime | None = None
    # A scheduled close is executed by the per-game FIFO worker. The operation
    # marker and delivery state make statistics, archival, and master
    # notifications retry-safe across separate worker invocations.
    scheduled_close_at: datetime | None = None
    scheduled_close_operation_id: str | None = None
    scheduled_close_notified_recipients: list[str] = Field(default_factory=list)
    scheduled_close_completed_at: datetime | None = None
    # Legacy games predate deep-link signup tokens. Every newly created game
    # receives one whose alphabet and length fit Telegram start parameters.
    signup_token: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # Announcement posts are added after game creation and remain nullable for
    # legacy games and newly created games whose announcements are not ready.
    telegram_post_url: str | None = Field(default=None, max_length=2048)
    vk_post_url: str | None = Field(default=None, max_length=2048)
    # A Telegram group can be bound only once. The bot-owned primary invite
    # link is generated during that binding and then reused permanently.
    telegram_chat_id: int | None = None
    telegram_chat_invite_link: str | None = Field(default=None, min_length=1, max_length=2048)
    confirmation_deadline: datetime | None = None
    registrations_migrated_at: datetime | None = Field(default_factory=utc_now)
    pass_table_resource_path: str | None = Field(default=None, pattern=r"^disk:/larp-bot/passes/.+\.xlsx$")
    pass_table_public_url: str | None = None
    achievement_resource_path: str | None = Field(default=None, pattern=r"^disk:/larp-bot/achievements/.+\.pptx$")
    achievement_public_url: str | None = None
    confirmed_notifications: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    ] = Field(default_factory=list)
    last_confirmed_notification_operation_id: str | None = None
    last_reserve_promotion_operation_id: str | None = None
    last_reserve_promotion_participant_key: str | None = None
    last_reserve_promotion_delivered_operation_id: str | None = None
    last_confirmed_link_promotion_operation_id: str | None = None
    last_confirmed_link_promotion_participant_key: str | None = None
    last_confirmed_link_promotion_delivered_operation_id: str | None = None
    last_participant_removal_operation_id: str | None = None
    participant_removal_keys: list[str] = Field(default_factory=list)
    participant_removal_notified_keys: list[str] = Field(default_factory=list)
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("registration_open_at", "confirmation_open_at")
    @classmethod
    def scheduled_opening_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("scheduled opening must include a timezone")
        return value

    @field_validator("scheduled_close_at", "scheduled_close_completed_at")
    @classmethod
    def scheduled_close_times_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("scheduled close time must include a timezone")
        return value

    @field_validator("registration_open_notification_completed_at")
    @classmethod
    def registration_open_notification_completion_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("registration opening notification completion must include a timezone")
        return value

    @field_validator("telegram_post_url")
    @classmethod
    def validate_telegram_post_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_telegram_post_url(value)

    @field_validator("vk_post_url")
    @classmethod
    def validate_vk_post_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_vk_post_url(value)

    @model_validator(mode="after")
    def table_fields_are_atomic(self) -> Event:
        if (self.public_table_resource_path is None) != (self.public_table_public_url is None):
            raise ValueError("public table resource path and public URL must be set together")
        if (self.pass_table_resource_path is None) != (self.pass_table_public_url is None):
            raise ValueError("pass table resource path and public URL must be set together")
        if (self.achievement_resource_path is None) != (self.achievement_public_url is None):
            raise ValueError("achievement resource path and public URL must be set together")
        if self.telegram_chat_invite_link is not None and self.telegram_chat_id is None:
            raise ValueError("Telegram chat invite link requires a chat binding")
        return self

    @property
    def master_table_resource_path(self) -> str:
        """The legacy column is retained as the stable master-table resource."""

        return self.disk_resource_path

    @property
    def master_table_public_url(self) -> str:
        """The legacy column is retained as the stable master-table URL."""

        return self.public_registration_url

    def registration_is_open(self, now: datetime | None = None) -> bool:
        current = datetime.now(UTC) if now is None else now
        return self.registration_open_at is None or self.registration_open_at <= current


class ScheduledLifecycleEvent(StrictModel):
    """Narrow event projection used by the periodic lifecycle scheduler."""

    event_id: str
    name: str
    game_date: date | None = None
    status: EventStatus
    registration_open_at: datetime | None = None
    registration_open_notified_recipients: list[str] = Field(default_factory=list)
    registration_open_notification_completed_at: datetime | None = None
    confirmation_open_at: datetime | None = None
    confirmation_deadline: datetime | None = None
    scheduled_close_at: datetime | None = None
    scheduled_close_operation_id: str | None = None
    scheduled_close_completed_at: datetime | None = None
    archived_at: datetime | None = None


class Registration(StrictModel):
    event_id: str
    participant_key: str = Field(min_length=43, max_length=64)
    display_name: str
    nickname: Nickname | None = None
    vk_profile: str = ""
    telegram_profile: str | None = None
    wish_play: str
    larp_experience: bool | None = None
    crossplay: bool | None = None
    character_wish: str = ""
    attendance_status: AttendanceStatus = AttendanceStatus.WAITING
    last_operation_id: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EnlistPayload(StrictModel):
    display_name: str = Field(min_length=2, max_length=300)
    nickname: Nickname | None = None
    wish_play: str = Field(min_length=1, max_length=2000)
    # Optional defaults keep commands already persisted by the previous schema
    # readable while the worker is being rolled forward.
    larp_experience: bool | None = None
    crossplay: bool | None = None
    vk_profile: str = ""
    telegram_profile: str | None = None
    same_day_warning_shown: bool = False


class CoPlayerWishPayload(StrictModel):
    wish_play: str = Field(min_length=1, max_length=2000)

    @field_validator("wish_play")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Пожелания не могут быть пустыми")
        return normalized


class CharacterWishPayload(StrictModel):
    character_wish: str = Field(min_length=1, max_length=5000)
    # Optional so confirmation commands already waiting in FIFO remain valid
    # during rollout. New switch confirmations identify the same-day game whose
    # registration must be removed.
    replaced_event_id: str | None = None

    @field_validator("character_wish")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Пожелания не могут быть пустыми")
        return normalized


class EmptyPayload(StrictModel):
    pass


class ConfirmationDeadlinePayload(StrictModel):
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must include a timezone")
        return value


class NotificationPayload(StrictModel):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Текст уведомления не может быть пустым")
        return normalized


class PlayerAmountPayload(StrictModel):
    player_amount: int = Field(ge=1, le=MAX_PLAYER_AMOUNT)


class EventDetailsPayload(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    game_date: date

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Название игры не может быть пустым")
        return normalized


class ProfileSyncPayload(StrictModel):
    names: list[str] = Field(min_length=1, max_length=2)

    @field_validator("names")
    @classmethod
    def normalize(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(name.strip() for name in values if name.strip()))
        if not normalized:
            raise ValueError("profile sync requires at least one name")
        return normalized


class StatisticsPayload(StrictModel):
    action: Literal["game", "link"]
    game_id: str | None = None

    @model_validator(mode="after")
    def validate_action(self) -> StatisticsPayload:
        if self.action == "game" and not self.game_id:
            raise ValueError("game requires game_id")
        return self


CommandPayload = (
    EnlistPayload
    | CoPlayerWishPayload
    | CharacterWishPayload
    | ConfirmationDeadlinePayload
    | NotificationPayload
    | EventDetailsPayload
    | PlayerAmountPayload
    | ProfileSyncPayload
    | StatisticsPayload
    | EmptyPayload
)


class ReplyContext(StrictModel):
    chat_id: int | None = None
    peer_id: int | None = None
    text_success: str = ""
    text_failure: str = ""
    buttons: list[Button] = Field(default_factory=list)


class OrderedRegistrationCommand(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=32, max_length=36)
    event_id: str
    operation: Operation
    platform: Platform
    platform_user_id: int = Field(ge=0)
    participant_key: str | None = None
    # New confirmation commands use the same FIFO group for both mutually
    # linked local profiles. Optional keeps commands queued before rollout valid.
    identity_group: str | None = Field(default=None, min_length=1, max_length=160)
    payload: CommandPayload = Field(default_factory=EmptyPayload)
    reply_context: ReplyContext = Field(default_factory=ReplyContext)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def queue_group_id(self) -> str:
        # Confirmation decisions span games, so serialize them per player
        # instead of independently per event. Other mutations retain their
        # established per-event ordering.
        if self.operation is Operation.CONFIRM:
            group = self.identity_group or f"{self.platform.value}:{self.platform_user_id}"
            return f"confirmation:{group}"
        if self.operation is Operation.SCHEDULED_CLOSE_STATISTICS:
            return "statistics"
        return self.event_id

    @model_validator(mode="after")
    def validate_shape(self) -> OrderedRegistrationCommand:
        if self.operation is Operation.STATISTICS:
            if not isinstance(self.payload, StatisticsPayload) or self.event_id != "statistics":
                raise ValueError("STATISTICS requires StatisticsPayload and the global statistics group")
        if self.operation is Operation.SYNC_PROFILE:
            expected_group = f"profile:{self.platform.value}:{self.platform_user_id}"
            if not isinstance(self.payload, ProfileSyncPayload) or self.event_id != expected_group:
                raise ValueError("SYNC_PROFILE requires ProfileSyncPayload and the user's profile group")
        participant_ops = {
            Operation.ENLIST,
            Operation.UPDATE_PROFILE,
            Operation.CONFIRM,
            Operation.UPDATE_CHARACTER_WISH,
            Operation.UPDATE_CO_PLAYER_WISH,
            Operation.CANCEL,
            Operation.REMOVE_PARTICIPANT,
        }
        if self.operation in participant_ops and not self.participant_key:
            raise ValueError("participant_key is required")
        if self.operation is Operation.ENLIST and not isinstance(self.payload, EnlistPayload):
            raise ValueError("ENLIST requires EnlistPayload")
        if self.operation in {Operation.CONFIRM, Operation.UPDATE_CHARACTER_WISH} and not isinstance(
            self.payload, CharacterWishPayload
        ):
            raise ValueError(f"{self.operation} requires CharacterWishPayload")
        if self.operation is Operation.UPDATE_CO_PLAYER_WISH and not isinstance(self.payload, CoPlayerWishPayload):
            raise ValueError("UPDATE_CO_PLAYER_WISH requires CoPlayerWishPayload")
        if self.operation in {Operation.OPEN_CONFIRMATION, Operation.SCHEDULED_OPEN_CONFIRMATION} and not isinstance(
            self.payload, ConfirmationDeadlinePayload
        ):
            raise ValueError(f"{self.operation} requires ConfirmationDeadlinePayload")
        notification_operations = {
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.SEND_ALL_PLAYERS_NOTIFICATION,
        }
        if self.operation in notification_operations and not isinstance(self.payload, NotificationPayload):
            raise ValueError(f"{self.operation} requires NotificationPayload")
        if self.operation is Operation.UPDATE_PLAYER_AMOUNT and not isinstance(self.payload, PlayerAmountPayload):
            raise ValueError("UPDATE_PLAYER_AMOUNT requires PlayerAmountPayload")
        if self.operation is Operation.UPDATE_EVENT_DETAILS and not isinstance(self.payload, EventDetailsPayload):
            raise ValueError("UPDATE_EVENT_DETAILS requires EventDetailsPayload")
        empty_payload_operations = {
            Operation.UPDATE_PROFILE,
            Operation.CANCEL,
            Operation.REMOVE_PARTICIPANT,
            Operation.OPEN_REGISTRATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.CLOSE_EVENT,
            Operation.REMOVE_WAITING_PARTICIPANTS,
            Operation.ARCHIVE_EVENT,
            Operation.SCHEDULED_CLOSE_EVENT,
            Operation.SCHEDULED_CLOSE_STATISTICS,
            Operation.SCHEDULED_CLOSE_FINALIZE,
            Operation.UNARCHIVE_EVENT,
            Operation.DELETE_EVENT,
        }
        if self.operation in empty_payload_operations and not isinstance(self.payload, EmptyPayload):
            raise ValueError(f"{self.operation} requires EmptyPayload")
        return self


class BotResponse(StrictModel):
    text: str
    buttons: list[Button] = Field(default_factory=list)
    deferred: bool = False
    command_enqueued: bool = False
    silent: bool = False


class InboundMessage(StrictModel):
    identity: BotIdentity
    update_id: str
    text: str = ""
    callback: str | None = None
    has_unsupported_content: bool = False
    chat_id: int | None = None
    peer_id: int | None = None
    telegram_username: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{5,32}$")


EnlistPayloadT = Annotated[EnlistPayload, Field()]
