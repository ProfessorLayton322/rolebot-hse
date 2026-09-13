from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from larp_bot.domain.models import (
    EnlistPayload,
    Event,
    EventStatus,
    PassDetails,
    PlayerAmountPayload,
    Registration,
    TelegramUser,
    VkUser,
    event_reference,
    format_game_date,
    normalize_telegram_profile,
    parse_full_name,
    parse_game_date,
    surname_and_name,
)


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
    [
        ("vk.com/id123", "https://vk.com/id123"),
        ("https://m.vk.com/name", "https://vk.com/name"),
        ("vk.ru/id456", "https://vk.com/id456"),
        ("https://m.vk.ru/name", "https://vk.com/name"),
    ],
)
def test_telegram_vk_url_is_normalized(raw: str, normalized: str) -> None:
    assert TelegramUser(tg_id=1, vk_url=raw).vk_url == normalized


@pytest.mark.parametrize("value", [None, "", "-", "Пропустить"])
def test_vk_profile_accepts_missing_telegram(value: str | None) -> None:
    assert VkUser(vk_id=1, telegram_handle=value).telegram_handle is None


def test_vk_handle_is_normalized() -> None:
    assert VkUser(vk_id=1, telegram_handle="some_user").telegram_handle == "@some_user"
    assert VkUser(vk_id=1, telegram_handle="@some_user").telegram_handle == "@some_user"


def test_larp_experience_is_not_required_to_complete_a_profile() -> None:
    telegram = TelegramUser(
        tg_id=1,
        full_name="Иванов Иван",
        vk_url="https://vk.com/id1",
        crossplay=False,
        needs_pass=False,
    )
    vk = VkUser(
        vk_id=2,
        full_name="Петров Пётр",
        crossplay=True,
        needs_pass=False,
    )

    assert telegram.profile_complete and telegram.larp_experience is None
    assert vk.profile_complete and vk.larp_experience is None


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("@Some_User", "@some_user"),
        ("https://t.me/Some_User", "@some_user"),
        ("telegram.me/Some_User", "@some_user"),
        ("[id42|@Some_User]", "@some_user"),
    ],
)
def test_telegram_profile_reference_is_normalized(raw: str, normalized: str) -> None:
    assert normalize_telegram_profile(raw) == normalized


@pytest.mark.parametrize("value", ["some_user", "https://example.com/some_user", "https://t.me/+invite"])
def test_telegram_profile_reference_rejects_non_profile_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_telegram_profile(value)


def test_malformed_vk_mention_has_user_facing_validation_error() -> None:
    with pytest.raises(ValueError, match=r"Нужна ссылка t\.me или @username"):
        normalize_telegram_profile("[id42|some_user]")


def test_character_wish_cannot_be_reintroduced_on_user_models() -> None:
    assert "character_wish" not in TelegramUser.model_fields
    assert "character_wish" not in VkUser.model_fields
    with pytest.raises(ValidationError):
        TelegramUser(tg_id=1, character_wish="global")


def test_negative_co_player_preference_is_absent_from_domain_models() -> None:
    forbidden_field = "dont" + "_wish_play"
    assert forbidden_field not in EnlistPayload.model_fields
    assert forbidden_field not in Registration.model_fields


def test_event_has_exactly_three_states_and_starts_before_confirmation() -> None:
    assert list(EventStatus) == [
        EventStatus.CREATED,
        EventStatus.CONFIRMATION_OPEN,
        EventStatus.CLOSED,
    ]
    event = Event(
        event_id="event-a1",
        name="Game",
        disk_resource_path="disk:/larp-bot/events/event-a1-game.xlsx",
        public_registration_url="https://disk.example/game",
    )
    assert event.status is EventStatus.CREATED
    assert event.player_amount > 0
    assert event.registration_is_open()
    with pytest.raises(ValidationError):
        event.player_amount = 0


def test_scheduled_registration_opens_at_the_stored_instant() -> None:
    opening = datetime(2026, 9, 18, 15, tzinfo=UTC)
    event = Event(
        event_id="event-a1",
        name="Game",
        disk_resource_path="disk:/larp-bot/events/event-a1-game.xlsx",
        public_registration_url="https://disk.example/game",
        registration_open_at=opening,
    )

    assert not event.registration_is_open(opening - timedelta(microseconds=1))
    assert event.registration_is_open(opening)

    with pytest.raises(ValidationError, match="timezone"):
        event.registration_open_at = datetime(2026, 9, 18, 18)


def test_game_date_uses_strict_two_digit_russian_format() -> None:
    parsed = parse_game_date("14.11.26")

    assert parsed == date(2026, 11, 14)
    assert format_game_date(parsed) == "14.11.26"
    assert event_reference("Лесной предел", parsed) == "«Лесной предел» (14.11.26)"
    for invalid in ("14.11.2026", "1.11.26", "31.02.26"):
        with pytest.raises(ValueError):
            parse_game_date(invalid)


@pytest.mark.parametrize("value", [0, 10_001])
def test_player_amount_payload_enforces_game_capacity_bounds(value: int) -> None:
    with pytest.raises(ValidationError):
        PlayerAmountPayload(player_amount=value)


def test_russian_pass_profile_keeps_latin_fields_blank_and_uses_dash_for_no_patronym() -> None:
    details = PassDetails(
        surname_cyrillic="Иванов",
        name_cyrillic="Иван",
        patronym_cyrillic="-",
        foreigner=False,
        mobile_phone="+7 999 123-45-67",
        email="ivan@example.com",
    )

    assert details.patronym_cyrillic == "-"
    assert details.surname_latin is None
    assert details.name_latin is None
    assert details.patronym_latin is None


def test_foreign_pass_profile_requires_latin_names_and_matching_no_patronym_markers() -> None:
    details = PassDetails(
        surname_cyrillic="Ли",
        name_cyrillic="Анна",
        patronym_cyrillic="-",
        foreigner=True,
        surname_latin="Li",
        name_latin="Anna",
        patronym_latin="-",
        mobile_phone="+44 7700 900123",
        email="anna@example.com",
    )
    assert details.foreigner

    with pytest.raises(ValidationError, match="both patronym"):
        PassDetails(
            surname_cyrillic="Ли",
            name_cyrillic="Анна",
            patronym_cyrillic="-",
            foreigner=True,
            surname_latin="Li",
            name_latin="Anna",
            patronym_latin="Ivanovna",
            mobile_phone="+44 7700 900123",
            email="anna@example.com",
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"surname_cyrillic": "Ivanov"}, "кириллицу"),
        ({"surname_latin": "Иванов"}, "латиницу"),
        ({"mobile_phone": "12345678"}, "телефона"),
    ],
)
def test_pass_profile_rejects_invalid_identity_fields(overrides: dict[str, str], message: str) -> None:
    values = {
        "surname_cyrillic": "Иванов",
        "name_cyrillic": "Иван",
        "patronym_cyrillic": "Иванович",
        "foreigner": True,
        "surname_latin": "Ivanov",
        "name_latin": "Ivan",
        "patronym_latin": "Ivanovich",
        "mobile_phone": "+7 999 123-45-67",
        "email": "ivan@example.com",
    }
    with pytest.raises(ValidationError, match=message):
        PassDetails(**(values | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "alphabet", "expected"),
    [
        (
            "Семёнова-Звенигородская  Ева-Лотта\tИльинична",
            "cyrillic",
            ("Семёнова-Звенигородская", "Ева-Лотта", "Ильинична"),
        ),
        ("Li Anna", "latin", ("Li", "Anna")),
        ("O'Connor Eva-Lotta Marie", "latin", ("O'Connor", "Eva-Lotta", "Marie")),
    ],
)
def test_full_name_uses_only_whitespace_as_the_part_delimiter(
    raw: str,
    alphabet: str,
    expected: tuple[str, str, str],
) -> None:
    assert parse_full_name(raw, alphabet=alphabet) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "alphabet"),
    [
        ("Иванов", "cyrillic"),
        ("Иванов Иван Иванович Младший", "cyrillic"),
        ("Ivanov Иван", "cyrillic"),
        ("Иванов Иван", "latin"),
        ("Иванов,Иван", "cyrillic"),
    ],
)
def test_full_name_requires_two_or_three_parts_in_the_requested_script(raw: str, alphabet: str) -> None:
    with pytest.raises(ValueError):
        parse_full_name(raw, alphabet=alphabet)  # type: ignore[arg-type]


def test_surname_and_name_normalizes_whitespace_and_omits_patronym() -> None:
    assert surname_and_name("  Иванов\t Иван   Иванович ") == "Иванов Иван"
