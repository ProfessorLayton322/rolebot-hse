from __future__ import annotations

from larp_bot.domain.models import Button

MAIN_MENU = "Главное меню"
ADMIN_MENU = "Администрирование"


def main_menu_button() -> Button:
    return Button(label=MAIN_MENU, value=MAIN_MENU)


def admin_menu_button() -> Button:
    return Button(label=ADMIN_MENU, value=ADMIN_MENU)
