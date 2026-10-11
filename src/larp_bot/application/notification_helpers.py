"""Pure roster and chat-link helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from larp_bot.domain.models import AttendanceStatus, Registration

_TELEGRAM_CHAT_HOSTS = frozenset(
    {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog"}
)


_TELEGRAM_CHAT_PATH_RE = re.compile(r"^/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9_]{3,31})/?$")


_VK_CHAT_PATH_RE = re.compile(r"^/join/[A-Za-z0-9_+=-]+(?:/[A-Za-z0-9_+=-]+)*/?$")


def is_plain_chat_link(value: str) -> bool:
    """Recognize a message consisting solely of a Telegram or VK chat link."""

    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return False
    try:
        parsed = urlsplit(candidate)
        has_credentials_or_port = parsed.username is not None or parsed.password is not None or parsed.port is not None
        host = (parsed.hostname or "").casefold()
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or has_credentials_or_port:
        return False
    if parsed.query or parsed.fragment:
        return False
    if host in _TELEGRAM_CHAT_HOSTS:
        return bool(_TELEGRAM_CHAT_PATH_RE.fullmatch(parsed.path))
    return host in {"vk.me", "www.vk.me"} and bool(_VK_CHAT_PATH_RE.fullmatch(parsed.path))


def _active_main_roster(registrations: Sequence[Registration], player_amount: int) -> list[Registration]:
    active = [
        registration
        for registration in registrations
        if registration.attendance_status is not AttendanceStatus.CANCELLED
    ]
    active.sort(key=lambda registration: (registration.created_at, registration.participant_key))
    return active[:player_amount]


def _chat_link_recipient_keys(registrations: Sequence[Registration], player_amount: int) -> set[str]:
    """Stored chat links belong only to confirmed players inside the active main roster."""

    return {
        registration.participant_key
        for registration in _active_main_roster(registrations, player_amount)
        if registration.attendance_status is AttendanceStatus.CONFIRMED
    }
