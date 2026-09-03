from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Platform(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"
    SYSTEM = "system"


class EventStatus(StrEnum):
    CREATED = "CREATED"
    CONFIRMATION_OPEN = "CONFIRMATION_OPEN"
    CLOSED = "CLOSED"


class AttendanceStatus(StrEnum):
    WAITING = "Ожидается"
    CONFIRMED = "Подтверждено"
    CANCELLED = "Отменено"


class Operation(StrEnum):
    ENLIST = "ENLIST"
    CONFIRM = "CONFIRM"
    UPDATE_CHARACTER_WISH = "UPDATE_CHARACTER_WISH"
    CANCEL = "CANCEL"
    OPEN_CONFIRMATION = "OPEN_CONFIRMATION"
    CLOSE_EVENT = "CLOSE_EVENT"
    DELETE_EVENT = "DELETE_EVENT"


class BotIdentity(StrictModel):
    platform: Platform
    platform_user_id: int = Field(gt=0)


class PassDetails(StrictModel):
    legal_name_cyrillic: str = Field(min_length=2, max_length=300)
    legal_name_latin: str = Field(min_length=2, max_length=300)
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)
    russian_citizen: bool


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
    candidate = value.strip().removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", candidate):
        raise ValueError("Некорректный Telegram username")
    return f"@{candidate}"


class UserBase(StrictModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=300)
    crossplay: bool | None = None
    larp_experience: bool | None = None
    needs_pass: bool | None = None
    pass_details: PassDetails | None = None
    dialog_state: str = "IDLE"
    dialog_context: dict[str, Any] = Field(default_factory=dict)
    last_update_id: str | None = None
    last_update_at: datetime | None = None
    last_delivery_operation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def pass_details_match_choice(self) -> UserBase:
        if self.needs_pass is False and self.pass_details is not None:
            raise ValueError("pass_details must be empty when no pass is needed")
        return self

    @property
    def common_profile_complete(self) -> bool:
        basics = (
            self.full_name is not None
            and self.crossplay is not None
            and self.larp_experience is not None
            and self.needs_pass is not None
        )
        return basics and (not self.needs_pass or self.pass_details is not None)


class TelegramUser(UserBase):
    tg_id: int = Field(gt=0)
    vk_url: str | None = None

    @field_validator("vk_url")
    @classmethod
    def validate_vk_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_vk_url(value)

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
    disk_resource_path: str = Field(pattern=r"^disk:/larp-bot/events/.+\.xlsx$")
    public_registration_url: str
    status: EventStatus = EventStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Registration(StrictModel):
    event_id: str
    participant_key: str = Field(min_length=43, max_length=64)
    display_name: str
    wish_play: str
    character_wish: str = ""
    attendance_status: AttendanceStatus = AttendanceStatus.WAITING
    last_operation_id: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class EnlistPayload(StrictModel):
    display_name: str = Field(min_length=2, max_length=300)
    wish_play: str = Field(min_length=1, max_length=2000)
    # Optional defaults keep commands already persisted by the previous schema
    # readable while the worker is being rolled forward.
    larp_experience: bool | None = None
    crossplay: bool | None = None


class CharacterWishPayload(StrictModel):
    character_wish: str = Field(min_length=1, max_length=5000)

    @field_validator("character_wish")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Пожелания не могут быть пустыми")
        return normalized


class EmptyPayload(StrictModel):
    pass


CommandPayload = EnlistPayload | CharacterWishPayload | EmptyPayload


class ReplyContext(StrictModel):
    chat_id: int | None = None
    peer_id: int | None = None
    text_success: str = ""
    text_failure: str = ""


class OrderedRegistrationCommand(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=32, max_length=36)
    event_id: str
    operation: Operation
    platform: Platform
    platform_user_id: int = Field(ge=0)
    participant_key: str | None = None
    payload: CommandPayload = Field(default_factory=EmptyPayload)
    reply_context: ReplyContext = Field(default_factory=ReplyContext)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_shape(self) -> OrderedRegistrationCommand:
        participant_ops = {
            Operation.ENLIST,
            Operation.CONFIRM,
            Operation.UPDATE_CHARACTER_WISH,
            Operation.CANCEL,
        }
        if self.operation in participant_ops and not self.participant_key:
            raise ValueError("participant_key is required")
        if self.operation is Operation.ENLIST and not isinstance(self.payload, EnlistPayload):
            raise ValueError("ENLIST requires EnlistPayload")
        if self.operation in {Operation.CONFIRM, Operation.UPDATE_CHARACTER_WISH} and not isinstance(
            self.payload, CharacterWishPayload
        ):
            raise ValueError(f"{self.operation} requires CharacterWishPayload")
        empty_payload_operations = {
            Operation.CANCEL,
            Operation.OPEN_CONFIRMATION,
            Operation.CLOSE_EVENT,
            Operation.DELETE_EVENT,
        }
        if self.operation in empty_payload_operations and not isinstance(self.payload, EmptyPayload):
            raise ValueError(f"{self.operation} requires EmptyPayload")
        return self


class Button(StrictModel):
    label: str
    value: str


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
    chat_id: int | None = None
    peer_id: int | None = None


EnlistPayloadT = Annotated[EnlistPayload, Field()]
