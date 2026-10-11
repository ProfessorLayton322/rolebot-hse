from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, uuid5

from larp_bot.domain.models import (
    MAX_PLAYER_AMOUNT,
    BotResponse,
    Button,
    EmptyPayload,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    PrivilegedRole,
    ReplyContext,
    StatisticsPayload,
    User,
    event_reference,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    ADD_EVENT_LEADER,
    ADD_POST_LINKS,
    ADMIN_ACTIONS,
    ARCHIVE_GAME,
    ARCHIVED_GAMES,
    BACK,
    CHANGE_GAME_DETAILS,
    CHANGE_PLAYER_AMOUNT,
    CHANGE_STATUS,
    CREATE_PASS_TABLE,
    DAILY_PASSES_ACTION,
    DAILY_PASSES_DATE_PREFIX,
    DAILY_PASSES_PAGE_PREFIX,
    DELETE_GAME,
    EDIT_GAME,
    EVENT_STATUS_LABELS,
    EVENT_TABLES,
    EXPORT_ACHIEVEMENTS,
    GAME_FILES,
    GRANT_GAMEMASTER,
    GRANT_GAMEMASTER_VIEWER,
    GRANT_PASS_MANAGER,
    LEGACY_REMOVE_WAITING_PLAYERS,
    LEGACY_SEND_CONFIRMED_NOTIFICATION,
    LEGACY_STATUS_ACTIONS,
    LIST_EVENT_LEADERS,
    LIST_GAMEMASTERS,
    LIST_PASS_TABLES,
    LIST_PLAYER_PROFILES,
    LIST_WAITING_PLAYERS,
    MANAGE_GAMES,
    MANAGE_MASTERS,
    MANAGE_MODERATORS,
    MANAGE_NOTIFICATIONS,
    MANAGE_PLAYERS,
    MANAGE_SCHEDULED_EVENTS,
    MANAGE_STATISTICS,
    PRIVILEGED_ROLE_LABELS,
    PROMOTE_RESERVE_PLAYER,
    REMOVE_PLAYER,
    REMOVE_WAITING_PLAYERS,
    REVOKE_GAMEMASTER,
    SEND_ALL_PLAYERS_NOTIFICATION,
    SEND_CONFIRMATION_REMINDER,
    SEND_CONFIRMED_LINK,
    SEND_CONFIRMED_NOTIFICATION,
    STATS_ADD_GAME,
    STATS_LINKS,
    UNARCHIVE_GAME,
    _admin_status_response,
    _confirmed_notification_preview,
    _confirmed_notification_prompt,
    _scheduled_close_response,
    _scheduled_confirmation_deadline_response,
    _scheduled_confirmation_time_response,
    edit_game_button,
    event_table_links,
    game_files_button,
    game_management_button,
    notification_management_button,
    user_id,
)
from .deadlines import MOSCOW_TIMEZONE, format_confirmation_deadline, next_night_03, tuesday_before_game_18
from .navigation import admin_menu_button


class AdministrationGamesMixin(ConversationContext):
    """Game lifecycle, file, notification, and scheduling actions."""

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
                    "Разархивируйте игру, чтобы вернуть её в список управления, "
                    "или удалите её без возможности восстановления."
                ),
                buttons=[
                    Button(label=UNARCHIVE_GAME, value=f"ag:unarchive:{event.event_id}"),
                    Button(label=DELETE_GAME, value=f"ag:delete:{event.event_id}"),
                    Button(label=BACK, value=ARCHIVED_GAMES),
                ],
            )
        is_leader, is_admin, can_view_unassigned = await asyncio.gather(
            self._is_event_leader(user, event.event_id),
            self._is_admin(user),
            self._can_view_unassigned_games(user),
        )
        text = (
            f"Управление игрой {event_reference(event.name, event.game_date)}\n\n"
            f"Количество игроков: {event.player_amount}\n"
            f"Статус: {EVENT_STATUS_LABELS[event.status]}\n"
            f"Пост в Telegram: {event.telegram_post_url or 'не добавлен'}\n"
            f"Пост во VK: {event.vk_post_url or 'не добавлен'}"
        )
        if not event.registration_is_open():
            assert event.registration_open_at is not None
            text += f"\nОткрытие регистрации: {format_confirmation_deadline(event.registration_open_at)} (МСК)"
        if event.scheduled_close_at is not None:
            text += f"\nАвтоматическое закрытие: {format_confirmation_deadline(event.scheduled_close_at)} (МСК)"
        if event.confirmation_deadline is not None:
            text += f"\nДедлайн подтверждения: {format_confirmation_deadline(event.confirmation_deadline)}"
        if not is_leader and not is_admin:
            if can_view_unassigned:
                view_actions = [
                    (MANAGE_PLAYERS, "players"),
                    (GAME_FILES, "files"),
                ]
                buttons = [Button(label=label, value=f"ag:{action}:{event.event_id}") for label, action in view_actions]
                buttons.extend(
                    [
                        Button(label=LIST_EVENT_LEADERS, value=f"master:list:{event.event_id}"),
                        Button(label=BACK, value=MANAGE_GAMES),
                    ]
                )
                return BotResponse(text=text + "\n\nДоступен просмотр данных игры:", buttons=buttons)
            return BotResponse(
                text=text + "\n\nАдминистративные действия доступны только мастерам этой игры.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )
        actions = [
            (EDIT_GAME, "edit"),
            (MANAGE_SCHEDULED_EVENTS, "schedule"),
            (MANAGE_NOTIFICATIONS, "notifications"),
            (MANAGE_PLAYERS, "players"),
            (GAME_FILES, "files"),
            (MANAGE_MASTERS, "masters"),
        ]
        buttons = [Button(label=label, value=f"ag:{action}:{event.event_id}") for label, action in actions]
        if is_admin:
            buttons.append(Button(label=STATS_ADD_GAME, value=f"stats:game-manage:{event.event_id}"))
        buttons.append(Button(label=BACK, value=MANAGE_GAMES))
        return BotResponse(text=text + "\n\nВыберите действие:", buttons=buttons)

    @staticmethod
    def _scheduled_events_menu(event: Event, *, notice: str = "") -> BotResponse:
        now = datetime.now(UTC)
        if event.registration_open_at is None or event.registration_open_at <= now:
            registration = "регистрация уже открыта"
        else:
            registration = f"{format_confirmation_deadline(event.registration_open_at)} (МСК)"
        if event.confirmation_open_at is None or event.confirmation_deadline is None:
            confirmation = (
                "подтверждение уже открыто" if event.status is EventStatus.CONFIRMATION_OPEN else "не запланировано"
            )
        else:
            confirmation = (
                f"{format_confirmation_deadline(event.confirmation_open_at)} (МСК); "
                f"дедлайн {format_confirmation_deadline(event.confirmation_deadline)} (МСК)"
            )
        closing = (
            "не запланировано"
            if event.scheduled_close_at is None
            else f"{format_confirmation_deadline(event.scheduled_close_at)} (МСК)"
        )
        buttons: list[Button] = []
        if event.status is EventStatus.CREATED:
            buttons.extend(
                [
                    Button(label="Открытие регистрации", value=f"ag:schedule-registration:{event.event_id}"),
                    Button(
                        label="Открытие подтверждения",
                        value=f"ag:schedule-confirmation:{event.event_id}",
                    ),
                ]
            )
        if event.scheduled_close_operation_id is None:
            buttons.append(Button(label="Закрытие игры", value=f"ag:schedule-close:{event.event_id}"))
        buttons.append(game_management_button(event.event_id))
        return BotResponse(
            text=(
                f"{notice}Запланированные события игры {event_reference(event.name, event.game_date)}\n\n"
                f"Открытие регистрации: {registration}.\n"
                f"Открытие подтверждения: {confirmation}.\n"
                f"Закрытие игры: {closing}."
            ),
            buttons=buttons,
        )

    @staticmethod
    def _registration_management_prompt(event: Event, *, invalid: str = "") -> BotResponse:
        prefix = f"{invalid}\n\n" if invalid else ""
        buttons = []
        if event.registration_open_at is not None:
            buttons.append(
                Button(
                    label="Отменить расписание и открыть регистрацию",
                    value=f"ag:schedule-clear-registration:{event.event_id}",
                )
            )
        buttons.append(Button(label=BACK, value=f"ag:schedule:{event.event_id}"))
        return BotResponse(
            text=(
                f"{prefix}Введите новое время открытия регистрации строго в формате DD.MM.YY HH:MM. "
                "Минуты должны быть 00. Время указывается по Москве."
            ),
            buttons=buttons,
        )

    @staticmethod
    def _confirmation_management_prompt(event: Event, *, invalid: str = "") -> BotResponse:
        back = Button(label=BACK, value=f"ag:schedule:{event.event_id}")
        response = _scheduled_confirmation_time_response(
            back=back,
            tuesday_value=(
                f"ag:schedule-confirmation-tuesday:{event.event_id}" if event.game_date is not None else None
            ),
            invalid=invalid,
        )
        if event.confirmation_open_at is not None:
            response.buttons.insert(
                0,
                Button(
                    label="Отменить открытие подтверждения",
                    value=f"ag:schedule-clear-confirmation:{event.event_id}",
                ),
            )
        return response

    @staticmethod
    def _scheduled_close_validation(event: Event, closes_at: datetime) -> str | None:
        if event.game_date is not None and closes_at.date() <= event.game_date:
            return "game"
        if closes_at <= datetime.now(MOSCOW_TIMEZONE):
            return "past"
        if event.registration_open_at is not None and closes_at <= event.registration_open_at:
            return "registration"
        if event.confirmation_deadline is not None and closes_at <= event.confirmation_deadline:
            return "confirmation"
        return None

    @staticmethod
    def _notification_management(event: Event) -> BotResponse:
        return BotResponse(
            text=f"Уведомления игры {event_reference(event.name, event.game_date)}\n\nВыберите действие:",
            buttons=[
                Button(label=SEND_CONFIRMATION_REMINDER, value=f"ag:remind:{event.event_id}"),
                Button(label=SEND_ALL_PLAYERS_NOTIFICATION, value=f"ag:notify-all:{event.event_id}"),
                Button(label=SEND_CONFIRMED_LINK, value=f"ag:notify-link:{event.event_id}"),
                Button(label=SEND_CONFIRMED_NOTIFICATION, value=f"ag:notify-message:{event.event_id}"),
                game_management_button(event.event_id),
            ],
        )

    @staticmethod
    def _edit_game_menu(event: Event) -> BotResponse:
        return BotResponse(
            text=f"Изменить игру {event_reference(event.name, event.game_date)}\n\nВыберите действие:",
            buttons=[
                Button(label=CHANGE_STATUS, value=f"ag:status:{event.event_id}"),
                Button(label=CHANGE_GAME_DETAILS, value=f"ag:details:{event.event_id}"),
                Button(label=CHANGE_PLAYER_AMOUNT, value=f"ag:player-amount:{event.event_id}"),
                Button(label=ADD_POST_LINKS, value=f"ag:post-links:{event.event_id}"),
                Button(label=ARCHIVE_GAME, value=f"ag:archive:{event.event_id}"),
                game_management_button(event.event_id),
            ],
        )

    @staticmethod
    def _game_files_menu(event: Event) -> BotResponse:
        return BotResponse(
            text=f"Файлы игры {event_reference(event.name, event.game_date)}\n\nВыберите действие:",
            buttons=[
                Button(label=EVENT_TABLES, value=f"ag:tables:{event.event_id}"),
                Button(label=EXPORT_ACHIEVEMENTS, value=f"ag:achievements:{event.event_id}"),
                game_management_button(event.event_id),
            ],
        )

    @staticmethod
    def _player_management(event: Event, *, can_manage: bool) -> BotResponse:
        buttons = [
            Button(label=LIST_WAITING_PLAYERS, value=f"ag:waiting:{event.event_id}"),
            Button(label=LIST_PLAYER_PROFILES, value=f"ag:profiles:{event.event_id}"),
        ]
        if can_manage:
            buttons[:0] = [
                Button(label=REMOVE_PLAYER, value=f"ag:remove:{event.event_id}"),
                Button(label=REMOVE_WAITING_PLAYERS, value=f"ag:remove-waiting:{event.event_id}"),
                Button(label=PROMOTE_RESERVE_PLAYER, value=f"ag:promote:{event.event_id}"),
            ]
        buttons.append(game_management_button(event.event_id))
        return BotResponse(
            text=(f"Управление игроками игры {event_reference(event.name, event.game_date)}\n\nВыберите действие:"),
            buttons=buttons,
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
        if action == "delete":
            if not await self._is_admin(user):
                await self._clear(user)
                return BotResponse(text="Недостаточно прав.")
            if event.archived_at is None:
                return BotResponse(
                    text="Удалить можно только архивированную игру.",
                    buttons=[game_management_button(event.event_id)],
                )
            user.dialog_state = "ADMIN_DELETE_NAME"
            user.dialog_context = {
                "event_id": event.event_id,
                "event_name": event.name,
                "game_date": event.game_date.isoformat() if event.game_date is not None else None,
            }
            return BotResponse(
                text=(
                    "⚠️ Это действие необратимо. Будут удалены игра, все записи, "
                    "таблицы регистрации и пропусков, а также файл ачивок.\n\n"
                    f"Для подтверждения введите точное название:\n\n{event.name}"
                ),
                buttons=[game_management_button(event.event_id)],
            )
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
        is_admin, is_leader, can_view_unassigned = await asyncio.gather(
            self._is_admin(user),
            self._is_event_leader(user, event.event_id),
            self._can_view_unassigned_games(user),
        )
        can_manage = is_admin or is_leader
        viewer_actions = {"files", "waiting", "tables", "achievements", "players", "profiles", "masters"}
        if not can_manage and (not can_view_unassigned or action not in viewer_actions):
            await self._clear(user)
            return BotResponse(
                text="Только мастера этой игры могут изменять её данные.",
                buttons=[Button(label=BACK, value=MANAGE_GAMES)],
            )

        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "game_date": event.game_date.isoformat() if event.game_date is not None else None,
            "event_status": event.status.value,
        }
        if action == "edit":
            return self._edit_game_menu(event)
        if action == "files":
            return self._game_files_menu(event)
        if action == "schedule":
            user.dialog_state = "IDLE"
            return self._scheduled_events_menu(event)
        if action in {
            "schedule-registration",
            "schedule-confirmation",
            "schedule-confirmation-tuesday",
            "schedule-close",
            "schedule-clear-registration",
            "schedule-clear-confirmation",
            "schedule-clear-close",
            "schedule-close-next-night",
        }:
            if (
                action
                in {
                    "schedule-registration",
                    "schedule-confirmation",
                    "schedule-confirmation-tuesday",
                    "schedule-clear-registration",
                    "schedule-clear-confirmation",
                }
                and event.status is not EventStatus.CREATED
            ):
                return self._scheduled_events_menu(
                    event,
                    notice="Регистрация или подтверждение уже открыты; это расписание изменить нельзя.\n\n",
                )
            if action == "schedule-registration":
                user.dialog_state = "ADMIN_SCHEDULE_REGISTRATION_TIME"
                return self._registration_management_prompt(event)
            if action == "schedule-confirmation":
                user.dialog_state = "ADMIN_SCHEDULE_CONFIRMATION_TIME"
                return self._confirmation_management_prompt(event)
            if action == "schedule-confirmation-tuesday":
                if event.game_date is None:
                    return self._scheduled_events_menu(event, notice="У игры не указана дата.\n\n")
                opens_at = tuesday_before_game_18(event.game_date)
                if opens_at <= datetime.now(MOSCOW_TIMEZONE):
                    invalid = "Открытие подтверждения должно быть запланировано на будущее время."
                elif event.registration_open_at is not None and opens_at <= event.registration_open_at:
                    invalid = "Подтверждение должно открываться строго после регистрации."
                elif event.scheduled_close_at is not None and opens_at >= event.scheduled_close_at:
                    invalid = "Подтверждение должно открываться строго до закрытия игры."
                else:
                    user.dialog_context["confirmation_open_at"] = opens_at.isoformat()
                    user.dialog_state = "ADMIN_SCHEDULE_CONFIRMATION_DEADLINE"
                    return _scheduled_confirmation_deadline_response(
                        back=Button(label=BACK, value=f"ag:schedule:{event.event_id}")
                    )
                user.dialog_state = "ADMIN_SCHEDULE_CONFIRMATION_TIME"
                return self._confirmation_management_prompt(event, invalid=invalid)
            if action == "schedule-close":
                if event.scheduled_close_operation_id is not None:
                    return self._scheduled_events_menu(
                        event,
                        notice="Автоматическое закрытие уже началось; изменить его время нельзя.\n\n",
                    )
                user.dialog_state = "ADMIN_SCHEDULE_CLOSE_TIME"
                return _scheduled_close_response(
                    next_night_value=f"ag:schedule-close-next-night:{event.event_id}",
                    cancel_value=f"ag:schedule-clear-close:{event.event_id}",
                    back=Button(label=BACK, value=f"ag:schedule:{event.event_id}"),
                )
            if action == "schedule-close-next-night":
                if event.game_date is None:
                    return self._scheduled_events_menu(event, notice="У игры не указана дата.\n\n")
                closes_at = next_night_03(event.game_date)
                error = self._scheduled_close_validation(event, closes_at)
                if error is not None:
                    user.dialog_state = "ADMIN_SCHEDULE_CLOSE_TIME"
                    return _scheduled_close_response(
                        past=error == "past",
                        not_after_game=error == "game",
                        confirmation_conflict=error == "confirmation",
                        registration_conflict=error == "registration",
                        next_night_value=f"ag:schedule-close-next-night:{event.event_id}",
                        cancel_value=f"ag:schedule-clear-close:{event.event_id}",
                        back=Button(label=BACK, value=f"ag:schedule:{event.event_id}"),
                    )
                await self.events.set_scheduled_close_at(event.event_id, closes_at)
                refreshed = await self.events.get(event.event_id)
                if refreshed is None:
                    return BotResponse(text="Игра не найдена.")
                return self._scheduled_events_menu(refreshed, notice="✅ Время закрытия сохранено.\n\n")
            if action == "schedule-clear-registration":
                await self.events.set_registration_open_at(event.event_id, None)
                notice = "✅ Расписание отменено; регистрация открыта.\n\n"
            elif action == "schedule-clear-confirmation":
                await self.events.set_confirmation_schedule(event.event_id, None, None)
                notice = "✅ Запланированное открытие подтверждения отменено.\n\n"
            else:
                if event.scheduled_close_operation_id is not None:
                    return self._scheduled_events_menu(
                        event,
                        notice="Автоматическое закрытие уже началось; отменить его нельзя.\n\n",
                    )
                await self.events.set_scheduled_close_at(event.event_id, None)
                notice = "✅ Запланированное закрытие отменено.\n\n"
            refreshed = await self.events.get(event.event_id)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.")
            return self._scheduled_events_menu(refreshed, notice=notice)
        if action == "status":
            user.dialog_state = "ADMIN_STATUS_SELECT"
            return _admin_status_response(event.name, event.status, event.event_id, event.game_date)
        if action == "details":
            user.dialog_state = "ADMIN_UPDATE_GAME_NAME"
            return BotResponse(
                text=f"Текущее название: {event.name}.\n\nВведите новое название игры:",
                buttons=[edit_game_button(event.event_id)],
            )
        if action == "player-amount":
            user.dialog_state = "ADMIN_UPDATE_PLAYER_AMOUNT"
            return BotResponse(
                text=(
                    f"Текущее количество игроков: {event.player_amount}.\n\n"
                    f"Введите новое количество игроков (от 1 до {MAX_PLAYER_AMOUNT}):"
                ),
                buttons=[edit_game_button(event.event_id)],
            )
        if action == "post-links":
            user.dialog_state = "ADMIN_POST_LINK_TELEGRAM"
            return BotResponse(
                text="Отправьте ссылку на пост об игре в Telegram:",
                buttons=[edit_game_button(event.event_id)],
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
            if event.telegram_chat_invite_link is None:
                await self._clear(user)
                return BotResponse(
                    text="Чат ещё не привязан к этой игре. Добавьте бота в чат и используйте команду /tie.",
                    buttons=[notification_management_button(event.event_id)],
                )
            recipient_count = await self.administration.confirmed_registration_count(
                event.event_id,
                main_roster_only=True,
            )
            user.dialog_state = "ADMIN_NOTIFICATION_CONFIRM"
            user.dialog_context["notification_text"] = event.telegram_chat_invite_link
            user.dialog_context["notification_recipient_count"] = recipient_count
            return _confirmed_notification_preview(
                event.name,
                event.event_id,
                event.telegram_chat_invite_link,
                recipient_count,
                event.game_date,
            )
        if action == "notify-all":
            user.dialog_state = "ADMIN_ALL_PLAYERS_NOTIFICATION_TEXT"
            return _confirmed_notification_prompt(
                event.name,
                event.event_id,
                link=False,
                all_players=True,
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
            return self._player_management(event, can_manage=can_manage)
        if action == "tables":
            await self._clear(user)
            pending_buttons = [game_files_button(event.event_id)]
            if can_manage:
                identity = self._identity(user)
                await self.registrations.enqueue(
                    operation=Operation.REFRESH_EVENT_TABLES,
                    event_id=event.event_id,
                    platform=identity.platform,
                    user_id=identity.platform_user_id,
                    payload=EmptyPayload(),
                    reply_context=ReplyContext(
                        chat_id=message.chat_id,
                        peer_id=message.peer_id,
                        buttons=pending_buttons,
                    ),
                    idempotency_key=f"{message.update_id}:{Operation.REFRESH_EVENT_TABLES.value}",
                )
                return BotResponse(
                    text="⏳ Таблицы обновляются. Бот пришлёт ссылки, когда загрузка завершится.",
                    buttons=pending_buttons,
                    deferred=True,
                    command_enqueued=True,
                )
            refreshed = await self.events.get(event.event_id)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
            if refreshed.public_table_public_url is None or refreshed.pass_table_public_url is None:
                return BotResponse(
                    text="Таблицы этой игры ещё не созданы. Обратитесь к мастеру игры или администратору.",
                    buttons=[game_files_button(event.event_id)],
                )
            return BotResponse(
                text=(f"Таблицы игры {event_reference(event.name, event.game_date)}\n\n{event_table_links(refreshed)}"),
                buttons=[game_files_button(event.event_id)],
            )
        if action == "achievements":
            await self._clear(user)
            requester = self._identity(user)
            pending_buttons = [game_files_button(event.event_id)]
            before_invoke = self._upload_pending_callback(
                user,
                message,
                text="⏳ Ачивки создаются. Бот пришлёт ссылку, когда загрузка завершится.",
                buttons=pending_buttons,
                operation=f"achievement:{event.event_id}",
            )
            export = await self.administration.export_achievements(
                event.event_id,
                requester=requester,
                reply_user_id=message.chat_id or message.peer_id or requester.platform_user_id,
                before_invoke=before_invoke,
            )
            if export.public_url is None:
                if before_invoke is not None:
                    return BotResponse(
                        text="",
                        buttons=pending_buttons,
                        deferred=requester.platform is Platform.TELEGRAM,
                        silent=True,
                    )
                return BotResponse(
                    text="⏳ Ачивки создаются. Бот пришлёт ссылку, когда загрузка завершится.",
                    buttons=pending_buttons,
                    deferred=True,
                )
            return BotResponse(
                text=(f"Ачивки игры {event_reference(event.name, event.game_date)}\n\n{export.public_url}"),
                buttons=[game_files_button(event.event_id)],
            )
        if action == "remove":
            return await self._show_removable_players(user, event)
        if action == "remove-waiting":
            return await self._remove_waiting_confirmation(user, event)
        if action == "promote":
            return await self._show_confirmed_reserve_players(user, event)
        if action == "profiles":
            return await self._show_player_profiles(user, event)
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
                    "Таблицы, файл ачивок и все записи будут сохранены.\n\n"
                    f"Для подтверждения введите точное название:\n\n{event.name}"
                ),
                buttons=[edit_game_button(event.event_id)],
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
        if value == DAILY_PASSES_ACTION:
            return await self._daily_passes_start(user, message)
        if value.startswith(DAILY_PASSES_PAGE_PREFIX):
            try:
                page = int(value.removeprefix(DAILY_PASSES_PAGE_PREFIX))
            except ValueError:
                page = 0
            return await self._daily_passes_start(user, message, page=page)
        if value.startswith(DAILY_PASSES_DATE_PREFIX):
            try:
                game_date = date.fromisoformat(value.removeprefix(DAILY_PASSES_DATE_PREFIX))
            except ValueError:
                return BotResponse(text="Некорректная дата.", buttons=[admin_menu_button()])
            return await self._daily_pass_table(user, message, game_date)
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
            EDIT_GAME,
            GAME_FILES,
            CHANGE_STATUS,
            CHANGE_PLAYER_AMOUNT,
            MANAGE_SCHEDULED_EVENTS,
            MANAGE_NOTIFICATIONS,
            SEND_CONFIRMATION_REMINDER,
            SEND_CONFIRMED_LINK,
            SEND_CONFIRMED_NOTIFICATION,
            SEND_ALL_PLAYERS_NOTIFICATION,
            LEGACY_SEND_CONFIRMED_NOTIFICATION,
            LIST_WAITING_PLAYERS,
            CREATE_PASS_TABLE,
            LIST_PASS_TABLES,
            EVENT_TABLES,
            EXPORT_ACHIEVEMENTS,
            REMOVE_PLAYER,
            MANAGE_PLAYERS,
            LIST_PLAYER_PROFILES,
            PROMOTE_RESERVE_PLAYER,
            REMOVE_WAITING_PLAYERS,
            LEGACY_REMOVE_WAITING_PLAYERS,
            ADD_EVENT_LEADER,
            CHANGE_GAME_DETAILS,
            ARCHIVE_GAME,
            DELETE_GAME,
            "📋 Список игр",
            *LEGACY_STATUS_ACTIONS,
        }:
            return await self._show_admin_games(user)
        role_grants = {
            GRANT_GAMEMASTER: PrivilegedRole.GAMEMASTER,
            GRANT_GAMEMASTER_VIEWER: PrivilegedRole.GAMEMASTER_VIEWER,
            GRANT_PASS_MANAGER: PrivilegedRole.PASS_MANAGER,
        }
        if value in role_grants:
            if not await self._is_admin(user):
                return BotResponse(text="Недостаточно прав.")
            role = role_grants[value]
            user.dialog_state = "ADMIN_GAMEMASTER_PLATFORM"
            user.dialog_context = {"privileged_role": role.value}
            return BotResponse(
                text=f"Выберите бот, которым пользуется будущий {PRIVILEGED_ROLE_LABELS[role].casefold()}:",
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
        if (
            value in ADMIN_ACTIONS
            or value in LEGACY_STATUS_ACTIONS
            or value.startswith((DAILY_PASSES_DATE_PREFIX, DAILY_PASSES_PAGE_PREFIX))
        ):
            return await self._admin_start(user, message, value)
        return None
