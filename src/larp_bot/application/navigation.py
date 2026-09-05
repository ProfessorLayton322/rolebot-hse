from __future__ import annotations

from larp_bot.domain.models import Button

MAIN_MENU = "Главное меню"
ADMIN_MENU = "Администрирование"
CONFIRM_PARTICIPATION = "✅ Подтвердить участие"


def main_menu_button() -> Button:
    return Button(label=MAIN_MENU, value=MAIN_MENU)


def admin_menu_button() -> Button:
    return Button(label=ADMIN_MENU, value=ADMIN_MENU)


def confirmation_button(event_id: str) -> Button:
    return Button(label=CONFIRM_PARTICIPATION, value=f"select:confirm:{event_id}")
