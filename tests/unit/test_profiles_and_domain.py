from __future__ import annotations

import pytest
from pydantic import ValidationError

from larp_bot.domain.models import TelegramUser, VkUser


@pytest.mark.parametrize("value", [None, "", "-"])
def test_telegram_profile_requires_vk_url(value: str | None) -> None:
    if value is None:
        user = TelegramUser(tg_id=1, vk_url=value)
        assert not user.profile_complete
    else:
        with pytest.raises(ValidationError):
            TelegramUser(tg_id=1, vk_url=value)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("vk.com/id123", "https://vk.com/id123"), ("https://m.vk.com/name", "https://vk.com/name")],
)
def test_telegram_vk_url_is_normalized(raw: str, normalized: str) -> None:
    assert TelegramUser(tg_id=1, vk_url=raw).vk_url == normalized


@pytest.mark.parametrize("value", [None, "", "-", "Пропустить"])
def test_vk_profile_accepts_missing_telegram(value: str | None) -> None:
    assert VkUser(vk_id=1, telegram_handle=value).telegram_handle is None


def test_vk_handle_is_normalized() -> None:
    assert VkUser(vk_id=1, telegram_handle="some_user").telegram_handle == "@some_user"
    assert VkUser(vk_id=1, telegram_handle="@some_user").telegram_handle == "@some_user"


def test_character_wish_cannot_be_reintroduced_on_user_models() -> None:
    assert "character_wish" not in TelegramUser.model_fields
    assert "character_wish" not in VkUser.model_fields
    with pytest.raises(ValidationError):
        TelegramUser(tg_id=1, character_wish="global")
