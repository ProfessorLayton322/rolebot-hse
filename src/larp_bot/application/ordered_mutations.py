"""Ordered mutation orchestration for event and registration commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime

from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    CharacterWishPayload,
    ConfirmationDeadlinePayload,
    CoPlayerWishPayload,
    EnlistPayload,
    Event,
    EventDetailsPayload,
    EventStatus,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    PlayerAmountPayload,
    Registration,
    TelegramUser,
    event_reference,
    surname_and_name,
)
from larp_bot.domain.security import participant_key

from .confirmation_openings import ScheduledConfirmationOpeningDispatcher
from .deadlines import MOSCOW_TIMEZONE
from .identity import identities_for_local_user
from .navigation import confirmation_button
from .notification_helpers import (
    _active_main_roster,
)
from .notification_services import (
    _current_sift_entries,
    confirmation_success_text,
    enlistment_success_text,
    event_master_records,
)
from .ports import AdminConfigProvider, EventRepository, UserRepository
from .registration_services import (
    RegistrationCatalog,
    _daily_pass_profiles,
    _effective_registration,
    _event_pass_profiles,
    player_registrations_on_date,
)
from .service_support import (
    SIFT_DEMOTED_PREFIX,
    SIFT_PROMOTED_PREFIX,
    EventNotFound,
    MutationResult,
    OperationNotAllowed,
    RegistrationNotFound,
    _cancellation_snapshot_entry,
    _sift_snapshot_entry,
    daily_pass_table_path,
)


class OrderedMutationService:
    """Authoritative worker-time validation, YDB mutation, and showcase refresh."""

    def __init__(
        self,
        events: EventRepository,
        catalog: RegistrationCatalog,
        users: UserRepository | None = None,
        participant_secret: str | None = None,
        admins: AdminConfigProvider | None = None,
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.users = users
        self.participant_secret = participant_secret
        self.admins = admins

    async def _require_event_leader(self, command: OrderedRegistrationCommand) -> None:
        identities = await identities_for_local_user(self.users, command.platform, command.platform_user_id)
        leaders = await self.events.list_leaders(command.event_id)
        if any(identity in leaders for identity in identities):
            return
        if self.admins is not None and any(
            [await self.admins.is_admin(identity.platform, identity.platform_user_id) for identity in identities]
        ):
            return
        raise OperationNotAllowed("Только мастера этой игры и администраторы бота могут изменять её данные")

    async def _require_admin(self, command: OrderedRegistrationCommand) -> None:
        identities = await identities_for_local_user(self.users, command.platform, command.platform_user_id)
        if self.admins is not None and any(
            [await self.admins.is_admin(identity.platform, identity.platform_user_id) for identity in identities]
        ):
            return
        raise OperationNotAllowed("Только администраторы бота могут изменять архив игр")

    async def _actor_registrations(
        self,
        event: Event,
        command: OrderedRegistrationCommand,
    ) -> tuple[tuple[BotIdentity, ...], list[Registration]]:
        if self.participant_secret is None:
            identity = BotIdentity(platform=command.platform, platform_user_id=command.platform_user_id)
            registration = await self.catalog.registrations.get(event.event_id, command.participant_key or "")
            return (identity,), [] if registration is None else [registration]
        identities = await identities_for_local_user(self.users, command.platform, command.platform_user_id)
        registrations = await asyncio.gather(
            *(
                self.catalog.registrations.get(
                    event.event_id,
                    participant_key(
                        self.participant_secret,
                        identity.platform,
                        identity.platform_user_id,
                        event.event_id,
                    ),
                )
                for identity in identities
            )
        )
        return identities, [registration for registration in registrations if registration is not None]

    async def _refresh_pass_table(
        self,
        event: Event,
        registration_rows: Sequence[Registration] | None = None,
    ) -> None:
        if event.pass_table_resource_path is not None:
            if self.users is None or self.participant_secret is None:
                raise RuntimeError("pass table refresh requires users and participant secret")
            profiles = await _event_pass_profiles(
                event,
                self.events,
                self.catalog.registrations,
                self.users,
                self.participant_secret,
                registration_rows,
            )
            await self.catalog.showcase.replace_pass_table(event.pass_table_resource_path, profiles)
        await self._refresh_daily_pass_table(event.game_date)

    async def _refresh_daily_pass_table(self, game_date: date | None) -> None:
        if game_date is None:
            return
        disk_path = daily_pass_table_path(game_date)
        if not await self.catalog.showcase.pass_table_exists(disk_path):
            return
        if self.users is None or self.participant_secret is None:
            raise RuntimeError("daily pass table refresh requires users and participant secret")
        _, profiles = await _daily_pass_profiles(
            game_date,
            self.events,
            self.catalog,
            self.users,
            self.participant_secret,
        )
        await self.catalog.showcase.replace_pass_table(disk_path, profiles)

    async def _record_reserve_promotion(
        self,
        event: Event,
        command: OrderedRegistrationCommand,
        registration: Registration | None,
        active: Sequence[Registration],
    ) -> None:
        if event.last_reserve_promotion_operation_id == command.operation_id:
            return
        promoted_key: str | None = None
        if registration is not None and registration.attendance_status is not AttendanceStatus.CANCELLED:
            target_index = next(
                (index for index, row in enumerate(active) if row.participant_key == registration.participant_key),
                None,
            )
            if target_index is not None and target_index < event.player_amount < len(active):
                promoted_key = active[event.player_amount].participant_key
        await self.events.set_reserve_promotion(event.event_id, command.operation_id, promoted_key)
        event.last_reserve_promotion_operation_id = command.operation_id
        event.last_reserve_promotion_participant_key = promoted_key

    async def _record_confirmed_link_promotion(
        self,
        event: Event,
        command: OrderedRegistrationCommand,
        registration: Registration | None,
        active: Sequence[Registration],
    ) -> None:
        if event.last_confirmed_link_promotion_operation_id == command.operation_id:
            return
        promoted_key: str | None = None
        if registration is not None and registration.attendance_status is not AttendanceStatus.CANCELLED:
            target_index = next(
                (index for index, row in enumerate(active) if row.participant_key == registration.participant_key),
                None,
            )
            if target_index is not None and target_index < event.player_amount < len(active):
                promoted = active[event.player_amount]
                if promoted.attendance_status is AttendanceStatus.CONFIRMED:
                    promoted_key = promoted.participant_key
        await self.events.set_confirmed_link_promotion(event.event_id, command.operation_id, promoted_key)
        event.last_confirmed_link_promotion_operation_id = command.operation_id
        event.last_confirmed_link_promotion_participant_key = promoted_key

    async def _record_promotions(
        self,
        event: Event,
        command: OrderedRegistrationCommand,
        registration: Registration | None,
    ) -> None:
        active: list[Registration] = []
        if (
            registration is not None
            and registration.attendance_status is not AttendanceStatus.CANCELLED
            and (
                event.last_reserve_promotion_operation_id != command.operation_id
                or event.last_confirmed_link_promotion_operation_id != command.operation_id
            )
        ):
            active = [
                row
                for row in await self.catalog.registrations.list_for_event(event.event_id)
                if row.attendance_status is not AttendanceStatus.CANCELLED
            ]
            active.sort(key=lambda row: (row.created_at, row.participant_key))
        await self._record_reserve_promotion(event, command, registration, active)
        await self._record_confirmed_link_promotion(event, command, registration, active)

    async def apply(self, command: OrderedRegistrationCommand) -> MutationResult:
        event = await self.events.get(command.event_id)
        if event is None:
            if command.operation in {Operation.ARCHIVE_EVENT, Operation.DELETE_EVENT}:
                return MutationResult("Игра уже отсутствует в постоянном списке")
            raise EventNotFound("Игра уже удалена или не существует")

        if command.operation is Operation.SCHEDULED_OPEN_CONFIRMATION:
            if command.platform is not Platform.SYSTEM:
                raise OperationNotAllowed("Запланированное открытие подтверждения выполняется только таймером")
            if (
                event.archived_at is not None
                or event.status is not EventStatus.CREATED
                or event.confirmation_open_at is None
                or event.confirmation_deadline is None
            ):
                return MutationResult(
                    "Запланированное открытие подтверждения уже отменено или выполнено",
                    notifications_enabled=False,
                )
            expected_id = ScheduledConfirmationOpeningDispatcher.operation_id(
                event.event_id,
                event.confirmation_open_at,
            )
            assert isinstance(command.payload, ConfirmationDeadlinePayload)
            if command.operation_id != expected_id or command.payload.deadline != event.confirmation_deadline:
                return MutationResult(
                    "Расписание открытия подтверждения было изменено",
                    notifications_enabled=False,
                )
            if not await self.events.open_scheduled_confirmation(
                event.event_id,
                event.confirmation_open_at,
                command.payload.deadline,
            ):
                return MutationResult(
                    "Расписание открытия подтверждения было изменено",
                    notifications_enabled=False,
                )
            return MutationResult(
                "Установлен Статус «Подтверждение» по расписанию",
                notifications_enabled=True,
            )

        if command.operation is Operation.UNARCHIVE_EVENT:
            await self._require_admin(command)
            if event.archived_at is None:
                await self._refresh_daily_pass_table(event.game_date)
                return MutationResult("Игра уже разархивирована")
            await self.events.unarchive(event.event_id, datetime.now(UTC))
            await self._refresh_daily_pass_table(event.game_date)
            return MutationResult("Игра разархивирована; регистрация остаётся закрытой")

        if command.operation is Operation.DELETE_EVENT:
            await self._require_admin(command)
            if event.archived_at is None:
                raise OperationNotAllowed("Удалить можно только архивированную игру")
            await self.catalog.delete(event)
            return MutationResult("Игра, записи и XLSX-таблицы удалены")

        if event.archived_at is not None:
            if command.operation is Operation.ARCHIVE_EVENT:
                await self._refresh_daily_pass_table(event.game_date)
                return MutationResult("Игра уже архивирована")
            if command.operation in {
                Operation.ENLIST,
                Operation.CONFIRM,
                Operation.UPDATE_CHARACTER_WISH,
                Operation.UPDATE_CO_PLAYER_WISH,
                Operation.CANCEL,
            }:
                raise OperationNotAllowed("Игра больше не активна; запись и изменение данных недоступны.")
            raise OperationNotAllowed("Игра уже архивирована")

        administrative_operations = {
            Operation.OPEN_REGISTRATION,
            Operation.OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.SEND_ALL_PLAYERS_NOTIFICATION,
            Operation.UPDATE_EVENT_DETAILS,
            Operation.UPDATE_PLAYER_AMOUNT,
            Operation.REMOVE_PARTICIPANT,
            Operation.REMOVE_WAITING_PARTICIPANTS,
            Operation.PROMOTE_RESERVE_PARTICIPANT,
            Operation.CLOSE_EVENT,
            Operation.ARCHIVE_EVENT,
        }
        if command.operation in administrative_operations:
            await self._require_event_leader(command)

        status_operations = {
            Operation.OPEN_REGISTRATION: (EventStatus.CREATED, "Установлен Статус «Регистрация»"),
            Operation.CLOSE_EVENT: (EventStatus.CLOSED, "Установлен Статус «Закрытие регистрации»"),
        }
        if command.operation is Operation.OPEN_CONFIRMATION:
            assert isinstance(command.payload, ConfirmationDeadlinePayload)
            status_changed = event.status is not EventStatus.CONFIRMATION_OPEN
            await self.events.open_confirmation(event.event_id, command.payload.deadline)
            return MutationResult(
                "Установлен Статус «Подтверждение»",
                notifications_enabled=status_changed,
            )
        if command.operation is Operation.SEND_CONFIRMATION_REMINDER:
            if event.status is not EventStatus.CONFIRMATION_OPEN:
                raise OperationNotAllowed("Подтверждение участия для этой игры не открыто")
            if event.confirmation_deadline is None:
                raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
            return MutationResult("Напоминание о подтверждении отправлено")
        if command.operation is Operation.SEND_CONFIRMED_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            await self.events.append_confirmed_notification(
                event.event_id,
                command.payload.text,
                command.operation_id,
            )
            return MutationResult("Уведомление подтвердившим участие отправлено")
        if command.operation is Operation.SEND_ALL_PLAYERS_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            return MutationResult("Уведомление всем игрокам отправлено")
        if command.operation is Operation.UPDATE_EVENT_DETAILS:
            assert isinstance(command.payload, EventDetailsPayload)
            if (
                event.scheduled_close_at is not None
                and event.scheduled_close_at.astimezone(MOSCOW_TIMEZONE).date() <= command.payload.game_date
            ):
                raise OperationNotAllowed(
                    "Новая дата игры должна быть раньше запланированного автоматического закрытия"
                )
            await self.events.set_details(
                event.event_id,
                command.payload.name,
                command.payload.game_date,
            )
            await self._refresh_daily_pass_table(event.game_date)
            if command.payload.game_date != event.game_date:
                await self._refresh_daily_pass_table(command.payload.game_date)
            return MutationResult(
                f"Название и дата игры изменены: {event_reference(command.payload.name, command.payload.game_date)}"
            )
        if command.operation is Operation.UPDATE_PLAYER_AMOUNT:
            assert isinstance(command.payload, PlayerAmountPayload)
            event = await self.catalog.ensure_migrated(event)
            if event.last_participant_removal_operation_id != command.operation_id:
                promoted_keys: list[str] = []
                if command.payload.player_amount > event.player_amount:
                    registrations = [
                        registration
                        for registration in await self.catalog.registrations.list_for_event(event.event_id)
                        if registration.attendance_status is not AttendanceStatus.CANCELLED
                    ]
                    registrations.sort(key=lambda row: (row.created_at, row.participant_key))
                    promoted_keys = [
                        registration.participant_key
                        for registration in registrations[event.player_amount : command.payload.player_amount]
                    ]
                # Reuse the per-event retry snapshot used by other multi-recipient
                # transitions. FIFO ordering prevents another event command from
                # replacing it before every notification attempt has completed.
                await self.events.set_participant_removal(
                    event.event_id,
                    command.operation_id,
                    promoted_keys,
                )
            await self.events.set_player_amount(event.event_id, command.payload.player_amount)
            event.player_amount = command.payload.player_amount
            await self.catalog.refresh(event)
            return MutationResult(f"Количество игроков изменено: {event.player_amount}")
        if command.operation in status_operations:
            status, message = status_operations[command.operation]
            status_changed = await self.events.set_status(event.event_id, status)
            return MutationResult(message, notifications_enabled=status_changed)
        if command.operation is Operation.ARCHIVE_EVENT:
            await self.catalog.archive(event)
            await self._refresh_daily_pass_table(event.game_date)
            return MutationResult("Игра архивирована; таблицы и записи сохранены")

        event = await self.catalog.ensure_migrated(event)
        if command.operation is Operation.REMOVE_WAITING_PARTICIPANTS:
            registrations = [
                registration
                for registration in await self.catalog.registrations.list_for_event(event.event_id)
                if registration.attendance_status is not AttendanceStatus.CANCELLED
            ]
            registrations.sort(key=lambda row: (row.created_at, row.participant_key))
            reordered = sorted(
                registrations,
                key=lambda row: (
                    row.attendance_status is not AttendanceStatus.CONFIRMED,
                    row.created_at,
                    row.participant_key,
                ),
            )
            if [row.participant_key for row in registrations] != [row.participant_key for row in reordered]:
                before_main = {row.participant_key for row in registrations[: event.player_amount]}
                after_main = {row.participant_key for row in reordered[: event.player_amount]}
                moves = [
                    *(
                        _sift_snapshot_entry(SIFT_PROMOTED_PREFIX, row.participant_key)
                        for row in reordered
                        if row.participant_key in after_main - before_main
                    ),
                    *(
                        _sift_snapshot_entry(SIFT_DEMOTED_PREFIX, row.participant_key)
                        for row in registrations
                        if row.participant_key in before_main - after_main
                    ),
                ]
                # Save the moves before making them so notifications survive a retry.
                # A retry lands here again when an earlier attempt failed before the
                # sift, or when a player confirmed after it; add this attempt's moves
                # without resetting the notifications that were already delivered.
                if event.last_participant_removal_operation_id == command.operation_id:
                    await self.events.add_participant_removal_keys(event.event_id, command.operation_id, moves)
                else:
                    await self.events.set_participant_removal(event.event_id, command.operation_id, moves)
                await self.catalog.registrations.sift_waiting_to_end(event.event_id)
            elif event.last_participant_removal_operation_id != command.operation_id:
                await self.events.set_participant_removal(event.event_id, command.operation_id, [])
            event, registration_rows = await self.catalog.refresh_with_rows(event)
            stored_event = await self.events.get(event.event_id)
            current_entries = _current_sift_entries(
                stored_event.participant_removal_keys if stored_event is not None else [],
                registration_rows,
                event.player_amount,
            )
            promoted_count = sum(entry.startswith(SIFT_PROMOTED_PREFIX) for entry in current_entries)
            demoted_count = sum(entry.startswith(SIFT_DEMOTED_PREFIX) for entry in current_entries)
            return MutationResult(
                f"Заявки пересортированы: в основной состав перемещено {promoted_count}, в запас — {demoted_count}"
            )

        if command.operation is Operation.PROMOTE_RESERVE_PARTICIPANT:
            assert command.participant_key is not None
            registrations = [
                registration
                for registration in await self.catalog.registrations.list_for_event(event.event_id)
                if registration.attendance_status is not AttendanceStatus.CANCELLED
            ]
            registrations.sort(key=lambda row: (row.created_at, row.participant_key))
            promoted_entry = _sift_snapshot_entry(SIFT_PROMOTED_PREFIX, command.participant_key)
            if (
                event.last_participant_removal_operation_id == command.operation_id
                and promoted_entry
                in _current_sift_entries(
                    event.participant_removal_keys,
                    registrations,
                    event.player_amount,
                )
            ):
                await self.catalog.refresh(event)
                return MutationResult("Игрок уже перемещён в основной состав")

            selected_index = next(
                (
                    index
                    for index, registration in enumerate(registrations)
                    if registration.participant_key == command.participant_key
                ),
                None,
            )
            if selected_index is None or selected_index < event.player_amount:
                raise OperationNotAllowed("Выбранный игрок уже не находится в запасе")
            promoted = registrations[selected_index]
            if promoted.attendance_status is not AttendanceStatus.CONFIRMED:
                raise OperationNotAllowed("Выбранный игрок уже не подтвердил участие")
            demoted = next(
                (
                    registration
                    for registration in reversed(registrations[: event.player_amount])
                    if registration.attendance_status is AttendanceStatus.WAITING
                ),
                None,
            )
            if demoted is None:
                raise OperationNotAllowed(
                    "В основном составе нет неподтверждённой заявки, которую можно переместить в запас"
                )

            moves = [
                promoted_entry,
                _sift_snapshot_entry(SIFT_DEMOTED_PREFIX, demoted.participant_key),
            ]
            if event.last_participant_removal_operation_id == command.operation_id:
                await self.events.add_participant_removal_keys(event.event_id, command.operation_id, moves)
            else:
                await self.events.set_participant_removal(event.event_id, command.operation_id, moves)
            await self.catalog.registrations.swap_queue_positions(
                event.event_id,
                operation_id=command.operation_id,
                promoted_participant_key=promoted.participant_key,
                promoted_created_at=promoted.created_at,
                demoted_participant_key=demoted.participant_key,
                demoted_created_at=demoted.created_at,
            )
            await self.catalog.refresh(event)
            return MutationResult(
                f"Игрок «{promoted.display_name}» перемещён в основной состав, игрок «{demoted.display_name}» — в запас"
            )

        assert command.participant_key is not None
        if command.operation is Operation.UPDATE_PROFILE:
            if self.users is None or self.participant_secret is None:
                raise RuntimeError("profile update requires users and participant secret")
            expected_key = participant_key(
                self.participant_secret,
                command.platform,
                command.platform_user_id,
                event.event_id,
            )
            if command.participant_key != expected_key:
                raise OperationNotAllowed("Некорректная запись участника")
            user = await self.users.get(command.platform, command.platform_user_id)
            if user is None or not user.profile_complete or user.full_name is None:
                raise OperationNotAllowed("Сначала полностью заполните профиль")
            registration, leaders = await asyncio.gather(
                self.catalog.registrations.get(event.event_id, command.participant_key),
                self.events.list_leaders(event.event_id),
            )
            identity = BotIdentity(
                platform=command.platform,
                platform_user_id=command.platform_user_id,
            )
            if registration is None and identity not in leaders:
                return MutationResult("Запись игрока уже отсутствует", notifications_enabled=False)
            if isinstance(user, TelegramUser):
                vk_profile = user.vk_url or ""
                telegram_handle = user.telegram_handle
            else:
                vk_profile = f"https://vk.com/id{user.vk_id}"
                telegram_handle = user.telegram_handle
            telegram_profile = None if telegram_handle is None else f"https://t.me/{telegram_handle.removeprefix('@')}"
            refreshed_rows: Sequence[Registration] | None
            if registration is not None:
                await self.catalog.registrations.update_profile(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_key=command.participant_key,
                    display_name=surname_and_name(user.full_name),
                    nickname=user.nickname,
                    larp_experience=bool(user.larp_experience),
                    crossplay=bool(user.crossplay),
                    vk_profile=vk_profile,
                    telegram_profile=telegram_profile,
                )
                event, refreshed_rows = await self.catalog.refresh_with_rows(event)
            else:
                refreshed_rows = None
            await self._refresh_pass_table(event, refreshed_rows)
            return MutationResult("Профиль игрока обновлён", notifications_enabled=False)
        if command.operation is Operation.ENLIST:
            if event.status is EventStatus.CLOSED:
                raise OperationNotAllowed("Регистрация на эту игру закрыта")
            if not event.registration_is_open():
                raise OperationNotAllowed("Регистрация на эту игру ещё не открыта")
            assert isinstance(command.payload, EnlistPayload)
            identities, existing_registrations = await self._actor_registrations(event, command)
            existing = _effective_registration(existing_registrations)
            if (
                existing is not None
                and existing.attendance_status is not AttendanceStatus.CANCELLED
                and existing.last_operation_id != command.operation_id
            ):
                raise OperationNotAllowed(f"Вы уже записаны на игру {event_reference(event.name, event.game_date)}")
            larp_experience = command.payload.larp_experience
            crossplay = command.payload.crossplay
            nickname = command.payload.nickname
            vk_profile = command.payload.vk_profile
            telegram_profile = command.payload.telegram_profile
            if self.users is not None:
                user = await self.users.get(command.platform, command.platform_user_id)
                if user is None or not user.profile_complete:
                    raise OperationNotAllowed("Сначала полностью заполните профиль")
                nickname = user.nickname
                if larp_experience is None or crossplay is None or not vk_profile:
                    larp_experience = user.larp_experience
                    crossplay = user.crossplay
                    if isinstance(user, TelegramUser):
                        vk_profile = user.vk_url or ""
                    else:
                        vk_profile = f"https://vk.com/id{user.vk_id}"
                        if telegram_profile is None and user.telegram_handle is not None:
                            telegram_profile = f"https://t.me/{user.telegram_handle.removeprefix('@')}"
            await self.catalog.registrations.enlist(
                event.event_id,
                operation_id=command.operation_id,
                participant_key=command.participant_key,
                display_name=surname_and_name(command.payload.display_name),
                nickname=nickname,
                wish_play=command.payload.wish_play,
                larp_experience=larp_experience,
                crossplay=crossplay,
                vk_profile=vk_profile,
                telegram_profile=telegram_profile,
            )
            event, registration_rows = await self.catalog.refresh_with_rows(event)
            await self._refresh_pass_table(event, registration_rows)
            active = [row for row in registration_rows if row.attendance_status is not AttendanceStatus.CANCELLED]
            active.sort(key=lambda row: (row.created_at, row.participant_key))
            queue_position = next(
                index for index, row in enumerate(active, start=1) if row.participant_key == command.participant_key
            )
            same_day_warning = False
            if event.game_date is not None and not command.payload.same_day_warning_shown:
                if self.participant_secret is None:
                    raise RuntimeError("same-day signup validation requires participant secret")
                same_day_warning = bool(
                    await player_registrations_on_date(
                        self.events,
                        self.catalog,
                        self.participant_secret,
                        command.platform,
                        command.platform_user_id,
                        event.game_date,
                        statuses={AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED},
                        exclude_event_id=event.event_id,
                        identities=identities,
                        conflicts_with=event,
                    )
                )
            buttons = (confirmation_button(event.event_id),) if event.status is EventStatus.CONFIRMATION_OPEN else None
            return MutationResult(
                enlistment_success_text(
                    event,
                    queue_position,
                    masters=await event_master_records(self.events, self.users, event.event_id),
                    same_day_warning=same_day_warning,
                ),
                buttons=buttons,
            )

        if command.operation is Operation.CONFIRM and event.status is not EventStatus.CONFIRMATION_OPEN:
            if event.status is EventStatus.CREATED:
                raise OperationNotAllowed("Подтверждение участия в этой игре ещё не открыто")
            raise OperationNotAllowed("Подтверждение участия в этой игре закрыто")

        identities, actor_registrations = await self._actor_registrations(event, command)
        identity_keys = (
            {
                participant_key(
                    self.participant_secret,
                    identity.platform,
                    identity.platform_user_id,
                    event.event_id,
                )
                for identity in identities
            }
            if self.participant_secret is not None
            else {command.participant_key}
        )
        if command.operation is Operation.REMOVE_PARTICIPANT:
            registration = await self.catalog.registrations.get(event.event_id, command.participant_key)
            if registration is not None and registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Запись этого игрока уже отменена")
            if event.last_participant_removal_operation_id != command.operation_id:
                await self.events.set_participant_removal(
                    event.event_id,
                    command.operation_id,
                    [command.participant_key] if registration is not None else [],
                )
            await self._record_promotions(event, command, registration)
            if registration is not None:
                await self.catalog.registrations.remove(
                    event.event_id,
                    participant_key=command.participant_key,
                )
            # Refresh even when the row is already absent. The first attempt may
            # have deleted it before a transient workbook or pass-table failure.
            event, registration_rows = await self.catalog.refresh_with_rows(event)
            await self._refresh_pass_table(event, registration_rows)
            return MutationResult("Игрок удалён" if registration is not None else "Игрок уже удалён")
        if command.participant_key not in identity_keys:
            raise OperationNotAllowed("Некорректная запись участника")
        registration = _effective_registration(actor_registrations)
        if registration is None:
            raise RegistrationNotFound("Сначала запишитесь на эту игру")
        actor_keys = [registration.participant_key for registration in actor_registrations]
        linked = len(identities) > 1
        if command.operation is Operation.CONFIRM:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова запишитесь на эту игру")
            assert isinstance(command.payload, CharacterWishPayload)
            already_applied = (
                registration.last_operation_id == command.operation_id
                and registration.attendance_status is AttendanceStatus.CONFIRMED
            )
            replaced_event: Event | None = None
            replaced_registration: Registration | None = None
            replaced_participant_key: str | None = None
            replaced_participant_keys: list[str] = []
            if event.game_date is not None:
                if self.participant_secret is None:
                    raise RuntimeError("same-day confirmation validation requires participant secret")
                conflicts = await player_registrations_on_date(
                    self.events,
                    self.catalog,
                    self.participant_secret,
                    command.platform,
                    command.platform_user_id,
                    event.game_date,
                    statuses={AttendanceStatus.CONFIRMED},
                    exclude_event_id=event.event_id,
                    identities=identities,
                    conflicts_with=event,
                )
                conflict_ids = {conflict.event_id for conflict, _ in conflicts}
                if conflict_ids and conflict_ids != {command.payload.replaced_event_id}:
                    raise OperationNotAllowed(
                        "Вы уже подтвердили участие в другой игре этого дня. "
                        "Начните подтверждение заново и подтвердите замену."
                    )
                if not conflict_ids and command.payload.replaced_event_id is not None and not already_applied:
                    raise OperationNotAllowed(
                        "Игры этого дня больше не пересекаются по времени. Начните подтверждение заново."
                    )
                if command.payload.replaced_event_id is not None:
                    replaced_event = await self.events.get(command.payload.replaced_event_id)
                    if replaced_event is None or replaced_event.event_id == event.event_id:
                        raise OperationNotAllowed("Игра, участие в которой нужно заменить, не найдена")
                    if replaced_event.game_date != event.game_date:
                        raise OperationNotAllowed("Игры проводятся в разные дни")
                    replaced_registrations = await asyncio.gather(
                        *(
                            self.catalog.get(
                                replaced_event,
                                participant_key(
                                    self.participant_secret,
                                    identity.platform,
                                    identity.platform_user_id,
                                    replaced_event.event_id,
                                ),
                            )
                            for identity in identities
                        )
                    )
                    existing_replacements = [item for item in replaced_registrations if item is not None]
                    replaced_registration = _effective_registration(existing_replacements)
                    replaced_participant_keys = [item.participant_key for item in existing_replacements]
                    replaced_participant_key = (
                        replaced_registration.participant_key if replaced_registration is not None else None
                    )
                    if not linked:
                        replaced_participant_key = participant_key(
                            self.participant_secret,
                            command.platform,
                            command.platform_user_id,
                            replaced_event.event_id,
                        )
                    if (
                        replaced_registration is None
                        or replaced_registration.attendance_status is not AttendanceStatus.CONFIRMED
                    ) and not already_applied:
                        replaced_event = None
                        replaced_participant_key = None
                        replaced_participant_keys = []
            elif command.payload.replaced_event_id is not None:
                raise OperationNotAllowed("Для этой игры не задана дата")
            if replaced_event is not None:
                await self._record_promotions(replaced_event, command, replaced_registration)
            if linked:
                await self.catalog.registrations.confirm_linked(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_keys=actor_keys,
                    character_wish=command.payload.character_wish,
                    replaced_event_id=replaced_event.event_id if replaced_event is not None else None,
                    replaced_participant_keys=replaced_participant_keys,
                )
            else:
                await self.catalog.registrations.confirm(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_key=command.participant_key,
                    character_wish=command.payload.character_wish,
                    replaced_event_id=replaced_event.event_id if replaced_event is not None else None,
                    replaced_participant_key=replaced_participant_key,
                )
            if replaced_event is not None:
                replaced_event, replaced_rows = await self.catalog.refresh_with_rows(replaced_event)
                await self._refresh_pass_table(replaced_event, replaced_rows)
            event, registration_rows = await self.catalog.refresh_with_rows(event)
            await self._refresh_pass_table(event, registration_rows)
            main_roster_keys = {
                row.participant_key
                for row in _active_main_roster(
                    registration_rows,
                    event.player_amount,
                )
            }
            return MutationResult(
                confirmation_success_text(
                    event,
                    command.payload.character_wish,
                    in_main_roster=any(key in main_roster_keys for key in actor_keys),
                    masters=await event_master_records(self.events, self.users, event.event_id),
                ),
                replaced_event_id=replaced_event.event_id if replaced_event is not None else None,
            )
        if command.operation is Operation.UPDATE_CHARACTER_WISH:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова запишитесь на эту игру")
            if registration.attendance_status is AttendanceStatus.WAITING and not registration.character_wish:
                raise OperationNotAllowed("Впервые укажите пожелания при подтверждении участия")
            assert isinstance(command.payload, CharacterWishPayload)
            if linked:
                await self.catalog.registrations.update_linked_character_wish(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_keys=actor_keys,
                    character_wish=command.payload.character_wish,
                )
            else:
                await self.catalog.registrations.update_character_wish(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_key=command.participant_key,
                    character_wish=command.payload.character_wish,
                )
            await self.catalog.refresh(event)
            return MutationResult("Пожелания по персонажу обновлены")
        if command.operation is Operation.UPDATE_CO_PLAYER_WISH:
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                raise OperationNotAllowed("Сначала снова запишитесь на эту игру")
            assert isinstance(command.payload, CoPlayerWishPayload)
            if linked:
                await self.catalog.registrations.update_linked_co_player_wish(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_keys=actor_keys,
                    wish_play=command.payload.wish_play,
                )
            else:
                await self.catalog.registrations.update_co_player_wish(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_key=command.participant_key,
                    wish_play=command.payload.wish_play,
                )
            await self.catalog.refresh(event)
            return MutationResult("Пожелания по соигрокам обновлены")
        if command.operation is Operation.CANCEL:
            if event.last_participant_removal_operation_id != command.operation_id:
                # Save the status before cancelling so a retry still tells the
                # masters whether the player had confirmed.
                await self.events.set_participant_removal(
                    event.event_id,
                    command.operation_id,
                    []
                    if registration.attendance_status is AttendanceStatus.CANCELLED
                    else [_cancellation_snapshot_entry(registration)],
                )
            await self._record_promotions(event, command, registration)
            if linked:
                await self.catalog.registrations.cancel_linked(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_keys=actor_keys,
                )
            else:
                await self.catalog.registrations.cancel(
                    event.event_id,
                    operation_id=command.operation_id,
                    participant_key=command.participant_key,
                )
            event, registration_rows = await self.catalog.refresh_with_rows(event)
            await self._refresh_pass_table(event, registration_rows)
            return MutationResult("Участие отменено")
        raise AssertionError(f"unhandled operation: {command.operation}")
