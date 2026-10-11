from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from secrets import token_urlsafe

from larp_bot.domain.models import (
    AttendanceStatus,
    BotResponse,
    Button,
    EmptyPayload,
    Event,
    InboundMessage,
    Operation,
    Registration,
    ReplyContext,
    StatisticsPayload,
    TelegramUser,
    User,
    event_name_with_date,
    event_reference,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    ARCHIVED_GAMES,
    BACK,
    MANAGE_GAMES,
    MANAGE_STATISTICS,
    PAGE_SIZE,
    STATS_ADD_GAME,
    STATS_CONFIRM_ADD_GAME,
    STATS_LINKS,
    _attendance_status_text,
    _context_event_reference,
    _pagination_buttons,
    game_management_button,
    player_management_button,
)
from .navigation import ADMIN_MENU, admin_menu_button


class AdministrationRostersMixin(ConversationContext):
    """Administrative roster, profile, and statistics flows."""

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
                    label=registration.display_name,
                    value=f"admin:waiting:pick:{token}",
                )
            )
        page_token = token_urlsafe(8)
        buttons.extend(
            _pagination_buttons(
                previous=f"admin:waiting:page:p:{page_token}" if page.has_previous else None,
                following=f"admin:waiting:page:n:{page_token}" if page.has_next else None,
                cancel=f"ag:players:{event.event_id}",
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
                    player_management_button(event_id),
                ],
            )
        return BotResponse(
            text=self._waiting_player_profile_text(registration, participant),
            buttons=[
                Button(label="⬅️ К списку", value=f"ag:waiting:{event_id}"),
                player_management_button(event_id),
            ],
        )

    @classmethod
    def _optional_registration_cursor(cls, user: User, name: str) -> tuple[datetime, str] | None:
        if user.dialog_context.get(name) is None:
            return None
        return cls._stored_registration_cursor(user, name)

    async def _show_player_profiles(
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
        if not page.rows and (after is not None or before is not None):
            # The remembered page emptied out after cancellations; start over from the first page.
            return await self._show_player_profiles(user, event, invalid=invalid)
        user.dialog_state = "ADMIN_PROFILES_PICK"
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
                    value=f"admin:profiles:pick:{token}",
                )
            )
        page_token = token_urlsafe(8)
        buttons.extend(
            _pagination_buttons(
                previous=f"admin:profiles:page:p:{page_token}" if page.has_previous else None,
                following=f"admin:profiles:page:n:{page_token}" if page.has_next else None,
                cancel=f"ag:players:{event.event_id}",
            )
        )
        user.dialog_context.update(
            {
                "profiles_choices": choices,
                "profiles_page_token": page_token,
                "profiles_first": [page.rows[0].created_at.isoformat(), page.rows[0].participant_key],
                "profiles_last": [page.rows[-1].created_at.isoformat(), page.rows[-1].participant_key],
                # Remember how this page was loaded so a viewed profile can return to it.
                "profiles_after": None if after is None else [after[0].isoformat(), after[1]],
                "profiles_before": None if before is None else [before[0].isoformat(), before[1]],
            }
        )
        prefix = f"{invalid}\n\n" if invalid else ""
        return BotResponse(
            text=(
                f"{prefix}Профили игроков игры {event_reference(event.name, event.game_date)}\n\n"
                "Выберите игрока, чтобы посмотреть его профиль. Показаны все записавшиеся "
                "и подтвердившие участие, в основном составе и в запасе."
            ),
            buttons=buttons,
        )

    async def _current_player_profiles(self, user: User, event: Event, *, invalid: str = "") -> BotResponse:
        return await self._show_player_profiles(
            user,
            event,
            after=self._optional_registration_cursor(user, "profiles_after"),
            before=self._optional_registration_cursor(user, "profiles_before"),
            invalid=invalid,
        )

    @classmethod
    def _player_profile_text(
        cls,
        event: Event,
        registration: Registration,
        participant: User,
        *,
        role: str,
        in_main_roster: bool,
    ) -> str:
        telegram_profile = registration.telegram_profile
        if participant.telegram_handle is not None:
            telegram_profile = f"https://t.me/{participant.telegram_handle.removeprefix('@')}"
        if isinstance(participant, TelegramUser):
            bot_name = "Telegram"
            vk_profile = participant.vk_url or registration.vk_profile
        else:
            bot_name = "ВК"
            vk_profile = f"https://vk.com/id{participant.vk_id}"
        contacts = [
            f"Профиль Telegram: {telegram_profile or 'не указан'}",
            f"Профиль ВКонтакте: {vk_profile or 'не указан'}",
        ]
        roster = "основной состав" if in_main_roster else "запас"
        lines = [
            "Профиль",
            "",
            *cls._profile_lines(participant, role=role, contacts=contacts),
            "",
            f"Игра: {event_reference(event.name, event.game_date)}",
            f"Запись: {_attendance_status_text(registration.attendance_status)}, {roster}",
            f"Зарегистрирован через бота: {bot_name}",
        ]
        return "\n".join(lines)

    async def _admin_profiles_pick(self, user: User, value: str) -> BotResponse:
        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])

        parts = value.split(":")
        page_token = user.dialog_context.get("profiles_page_token")
        if len(parts) == 5 and parts[:3] == ["admin", "profiles", "page"]:
            direction, requested_token = parts[3:]
            if requested_token != page_token or direction not in {"n", "p", "c"}:
                return await self._show_player_profiles(user, event, invalid="Эта страница устарела.")
            if direction == "p":
                return await self._show_player_profiles(
                    user,
                    event,
                    before=self._stored_registration_cursor(user, "profiles_first"),
                )
            if direction == "n":
                return await self._show_player_profiles(
                    user,
                    event,
                    after=self._stored_registration_cursor(user, "profiles_last"),
                )
            return await self._current_player_profiles(user, event)
        if len(parts) != 4 or parts[:3] != ["admin", "profiles", "pick"]:
            return await self._current_player_profiles(user, event, invalid="Выберите игрока кнопкой из списка.")
        choices = user.dialog_context.get("profiles_choices")
        registration_key = choices.get(parts[3]) if isinstance(choices, dict) else None
        if not isinstance(registration_key, str):
            return await self._current_player_profiles(user, event, invalid="Этот список устарел.")
        registration = await self.administration.get_registration(event_id, registration_key)
        if registration is None or registration.attendance_status is AttendanceStatus.CANCELLED:
            return await self._current_player_profiles(
                user,
                event,
                invalid="Этот игрок больше не записан на игру.",
            )
        participant, in_main_roster = await asyncio.gather(
            self.administration.registration_user(event_id, registration_key),
            self.administration.in_main_roster(event_id, registration_key),
        )
        buttons = [
            Button(label="⬅️ К списку", value=f"admin:profiles:page:c:{page_token}"),
            player_management_button(event_id),
        ]
        if participant is None:
            return BotResponse(text="Не удалось найти профиль этого пользователя.", buttons=buttons)
        role = await self._user_role_label(participant)
        return BotResponse(
            text=self._player_profile_text(
                event,
                registration,
                participant,
                role=role,
                in_main_roster=in_main_roster,
            ),
            buttons=buttons,
        )

    async def _show_confirmed_reserve_players(
        self,
        user: User,
        event: Event,
        *,
        after: tuple[datetime, str] | None = None,
        before: tuple[datetime, str] | None = None,
        invalid: str = "",
    ) -> BotResponse:
        if not await self.administration.has_unconfirmed_main_slot(event.event_id):
            await self._clear(user)
            return BotResponse(
                text=(
                    f"В основном составе игры {event_reference(event.name, event.game_date)} нет свободных мест: "
                    "там нет неподтверждённых заявок, которые можно переместить в запас. "
                    "Сейчас никого нельзя подвинуть из запаса в основную сессию."
                ),
                buttons=[player_management_button(event.event_id)],
            )
        page = await self.administration.confirmed_reserve_registration_page(
            event.event_id,
            after=after,
            before=before,
            limit=PAGE_SIZE,
        )
        if not page.rows and (after is not None or before is not None):
            return await self._show_confirmed_reserve_players(user, event, invalid=invalid)
        user.dialog_state = "ADMIN_RESERVE_PROMOTION_PICK"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "game_date": event.game_date.isoformat() if event.game_date is not None else None,
        }
        if not page.rows:
            return BotResponse(
                text=(
                    f"В запасе игры {event_reference(event.name, event.game_date)} нет подтвердивших участие игроков."
                ),
                buttons=[player_management_button(event.event_id)],
            )

        choices: dict[str, str] = {}
        buttons: list[Button] = []
        for registration in page.rows:
            token = token_urlsafe(8)
            choices[token] = registration.participant_key
            buttons.append(
                Button(
                    label=registration.display_name,
                    value=f"admin:promote:pick:{token}",
                )
            )
        page_token = token_urlsafe(8)
        buttons.extend(
            _pagination_buttons(
                previous=f"admin:promote:page:p:{page_token}" if page.has_previous else None,
                following=f"admin:promote:page:n:{page_token}" if page.has_next else None,
                cancel=f"ag:players:{event.event_id}",
            )
        )
        user.dialog_context.update(
            {
                "promote_choices": choices,
                "promote_page_token": page_token,
                "promote_first": [page.rows[0].created_at.isoformat(), page.rows[0].participant_key],
                "promote_last": [page.rows[-1].created_at.isoformat(), page.rows[-1].participant_key],
                "promote_after": None if after is None else [after[0].isoformat(), after[1]],
                "promote_before": None if before is None else [before[0].isoformat(), before[1]],
            }
        )
        prefix = f"{invalid}\n\n" if invalid else ""
        return BotResponse(
            text=(
                f"{prefix}Подвинуть игрока в основную сессию игры "
                f"{event_reference(event.name, event.game_date)}\n\n"
                "Выберите подтвердившего участие игрока из запаса."
            ),
            buttons=buttons,
        )

    async def _current_confirmed_reserve_players(
        self,
        user: User,
        event: Event,
        *,
        invalid: str = "",
    ) -> BotResponse:
        return await self._show_confirmed_reserve_players(
            user,
            event,
            after=self._optional_registration_cursor(user, "promote_after"),
            before=self._optional_registration_cursor(user, "promote_before"),
            invalid=invalid,
        )

    async def _reserve_promotion_confirmation(
        self,
        user: User,
        event: Event,
        registration: Registration,
        *,
        invalid: str = "",
    ) -> BotResponse:
        participant = await self.administration.registration_user(event.event_id, registration.participant_key)
        if participant is None:
            return await self._current_confirmed_reserve_players(
                user,
                event,
                invalid="Не удалось найти профиль этого пользователя.",
            )
        role = await self._user_role_label(participant)
        prefix = f"{invalid}\n\n" if invalid else ""
        token = str(user.dialog_context["promote_selected_token"])
        profile_text = self._player_profile_text(
            event,
            registration,
            participant,
            role=role,
            in_main_roster=False,
        )
        return BotResponse(
            text=(
                f"{prefix}{profile_text}"
                "\n\nПереместить этого игрока в основную сессию? "
                "Последний неподтверждённый игрок в основном составе будет перемещён в запас."
            ),
            buttons=[
                Button(label="Да", value=f"admin:promote:confirm:{token}"),
                Button(label="Нет", value=f"admin:promote:page:c:{user.dialog_context['promote_page_token']}"),
            ],
        )

    async def _admin_reserve_promotion_pick(self, user: User, value: str) -> BotResponse:
        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])

        parts = value.split(":")
        page_token = user.dialog_context.get("promote_page_token")
        if len(parts) == 5 and parts[:3] == ["admin", "promote", "page"]:
            direction, requested_token = parts[3:]
            if requested_token != page_token or direction not in {"n", "p", "c"}:
                return await self._show_confirmed_reserve_players(user, event, invalid="Эта страница устарела.")
            if direction == "p":
                return await self._show_confirmed_reserve_players(
                    user,
                    event,
                    before=self._stored_registration_cursor(user, "promote_first"),
                )
            if direction == "n":
                return await self._show_confirmed_reserve_players(
                    user,
                    event,
                    after=self._stored_registration_cursor(user, "promote_last"),
                )
            return await self._current_confirmed_reserve_players(user, event)
        if len(parts) != 4 or parts[:3] != ["admin", "promote", "pick"]:
            return await self._current_confirmed_reserve_players(
                user,
                event,
                invalid="Выберите игрока кнопкой из списка.",
            )
        choices = user.dialog_context.get("promote_choices")
        registration_key = choices.get(parts[3]) if isinstance(choices, dict) else None
        if not isinstance(registration_key, str):
            return await self._current_confirmed_reserve_players(user, event, invalid="Этот список устарел.")
        registration = await self.administration.get_registration(event_id, registration_key)
        if registration is None or not await self.administration.can_promote_confirmed_reserve(
            event_id,
            registration_key,
        ):
            return await self._current_confirmed_reserve_players(
                user,
                event,
                invalid="Этот игрок уже не является подтвердившим участником в запасе.",
            )
        user.dialog_state = "ADMIN_RESERVE_PROMOTION_CONFIRM"
        user.dialog_context.update(
            {
                "promote_selected_key": registration_key,
                "promote_selected_token": parts[3],
                "promote_selected_name": registration.display_name,
            }
        )
        return await self._reserve_promotion_confirmation(user, event, registration)

    async def _admin_reserve_promotion_confirm(
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
        token = str(user.dialog_context["promote_selected_token"])
        if value == f"admin:promote:page:c:{user.dialog_context['promote_page_token']}":
            return await self._current_confirmed_reserve_players(user, event)
        participant_key = str(user.dialog_context["promote_selected_key"])
        registration = await self.administration.get_registration(event_id, participant_key)
        if value != f"admin:promote:confirm:{token}":
            if registration is None:
                return await self._current_confirmed_reserve_players(user, event, invalid="Этот список устарел.")
            return await self._reserve_promotion_confirmation(
                user,
                event,
                registration,
                invalid="Перемещение не подтверждено.",
            )
        if registration is None or not await self.administration.can_promote_confirmed_reserve(
            event_id,
            participant_key,
        ):
            return await self._current_confirmed_reserve_players(
                user,
                event,
                invalid="Состав игроков изменился. Выберите игрока заново.",
            )
        context = user.dialog_context.copy()
        await self._clear(user)
        await self.registrations.enqueue(
            operation=Operation.PROMOTE_RESERVE_PARTICIPANT,
            event_id=event_id,
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EmptyPayload(),
            reply_context=ReplyContext(
                text_success=(
                    f"✅ Игрок «{context['promote_selected_name']}» перемещён в основную сессию "
                    f"игры {_context_event_reference(context)}."
                ),
                buttons=[player_management_button(event_id)],
            ),
            idempotency_key=f"{message.update_id}:{Operation.PROMOTE_RESERVE_PARTICIPANT.value}",
            target_participant_key=participant_key,
        )
        return BotResponse(
            text="⏳ Перемещение принято в обработку.",
            buttons=[player_management_button(event_id)],
            deferred=True,
            command_enqueued=True,
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
                f"{prefix}Переместить подтверждённых игроков вверх списка, а не подтвердивших участие "
                f"в игре {event_reference(event.name, event.game_date)} — вниз?\n\n"
                f"Не подтвердили участие: {count}. Игроки, которые пересекут границу основного состава, "
                "получат уведомления."
            ),
            buttons=[
                Button(label="Да, пересортировать", value=f"admin:remove-waiting:confirm:{token}"),
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
            return await self._remove_waiting_confirmation(user, event, invalid="Пересортировка не подтверждена.")
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
            text="⏳ Пересортировка заявок принята в обработку.",
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
        buttons = [
            Button(
                label=event_name_with_date(event.name, event.game_date),
                value=f"stats:game:{event.event_id}",
            )
            for event in events
        ]
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
        if (
            len(parts) != 3
            or parts[0] != "stats"
            or parts[1]
            not in {
                "game",
                "game-manage",
                "confirm",
                "confirm-manage",
            }
        ):
            return BotResponse(text="Некорректное действие.")
        event = await self.events.get(parts[2])
        if event is None or event.archived_at is not None:
            return BotResponse(
                text="Игра не найдена или уже архивирована.",
                buttons=[Button(label=BACK, value=MANAGE_STATISTICS)],
            )
        from_management = parts[1].endswith("-manage")
        back = (
            game_management_button(event.event_id) if from_management else Button(label=BACK, value=MANAGE_STATISTICS)
        )
        if parts[1].startswith("game"):
            confirmation_action = "confirm-manage" if from_management else "confirm"
            return BotResponse(
                text=(
                    f"Добавить игру {event_reference(event.name, event.game_date)} в статистику?\n\n"
                    "Будут изменены обе постоянные таблицы статистики, а участие "
                    "будет засчитано подтверждённым игрокам и мастерам. "
                    "Отменить это действие через бота нельзя."
                ),
                buttons=[
                    Button(
                        label=STATS_CONFIRM_ADD_GAME,
                        value=f"stats:{confirmation_action}:{event.event_id}",
                    ),
                    back,
                ],
            )
        if value not in {button.value for button in user.last_bot_buttons}:
            return BotResponse(
                text="Это подтверждение устарело. Откройте игру и повторите действие.",
                buttons=[back],
            )
        return await self._enqueue_statistics(
            user,
            message,
            StatisticsPayload(action="game", game_id=event.event_id),
            back=back,
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
        buttons = [
            Button(
                label=event_name_with_date(event.name, event.game_date),
                value=f"ag:manage:{event.event_id}",
            )
            for event in events
        ]
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
