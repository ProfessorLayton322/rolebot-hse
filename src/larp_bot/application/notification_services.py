"""Participant and master notification delivery services."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    Button,
    Event,
    EventStatus,
    NotificationPayload,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    Registration,
    TelegramUser,
    UnreachableRecipient,
    User,
    event_reference,
    surname_and_name,
)
from larp_bot.domain.security import participant_key

from .deadlines import format_confirmation_deadline
from .identity import global_users, identities_for_local_user
from .navigation import confirmation_button, main_menu_button
from .notification_helpers import (
    _active_main_roster,
    _chat_link_recipient_keys,
    is_plain_chat_link,
)
from .ports import DeferredTransport, EventRepository, TelegramChatGateway, UserRepository
from .registration_services import (
    RegistrationCatalog,
)
from .service_support import (
    CANCELLATION_MASTER_PREFIX,
    LOGGER,
    SIFT_PROMOTED_PREFIX,
    EventNotFound,
    OperationNotAllowed,
    _parse_cancellation_snapshot_entry,
    _parse_sift_snapshot_entry,
    same_day_signup_warning,
)
from .table_links import public_table_links


@dataclass(frozen=True)
class NotificationRecipient:
    platform: Platform
    user_id: int
    user: User


def player_notification_text(event_name: str, message: str, game_date: date | None = None) -> str:
    return f"Сообщение от мастеров игры {event_reference(event_name, game_date)}:\n\n{message.strip()}"


def confirmed_notification_text(event_name: str, message: str, game_date: date | None = None) -> str:
    clean_message = message.strip()
    if not is_plain_chat_link(clean_message):
        return player_notification_text(event_name, clean_message, game_date)
    reference = event_reference(event_name, game_date)
    return f"Вы в основном составе игры {reference}. Вступите в чат: {clean_message}"


def cancellation_master_text(
    event: Event,
    player_name: str,
    nickname: str | None,
    *,
    confirmed: bool,
    same_day_replacement: bool = False,
) -> str:
    lines = [
        f"Отмена участия в игре {event_reference(event.name, event.game_date)}",
        "",
        f"Игрок: {player_name}",
    ]
    if nickname is not None:
        lines.append(f"Никнейм: {nickname}")
    lines.append(f"Статус участия: {'подтверждено' if confirmed else 'не подтверждено'}")
    if same_day_replacement:
        lines.extend(["", "Причина: игрок подтвердил участие в другой игре в тот же день."])
    return "\n".join(lines)


def _current_sift_entries(
    snapshot: Sequence[str],
    registrations: Sequence[Registration],
    player_amount: int,
) -> set[str]:
    """Keep the saved confirmation-sift moves that still match the current roster.

    Confirmations use per-player FIFO groups, so they can change attendance while
    a sift retry waits in its per-event group, which makes an older move untrue.
    """

    statuses = {registration.participant_key: registration.attendance_status for registration in registrations}
    main_roster_keys = {
        registration.participant_key for registration in _active_main_roster(registrations, player_amount)
    }
    current: set[str] = set()
    for entry in snapshot:
        parsed = _parse_sift_snapshot_entry(entry)
        if parsed is None:
            continue
        transition, key = parsed
        if transition == SIFT_PROMOTED_PREFIX:
            still_true = statuses.get(key) is AttendanceStatus.CONFIRMED and key in main_roster_keys
        else:
            still_true = statuses.get(key) is AttendanceStatus.WAITING and key not in main_roster_keys
        if still_true:
            current.add(entry)
    return current


MAX_MESSAGE_LENGTH = 4096


MASTER_NO_SHOW_WARNING = (
    "Если вы не придете на игру, ничего нам не сообщив, к вам могут быть применены санкции Ролевого Клуба"
)


def _message_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def event_master_records(
    events: EventRepository,
    users: UserRepository | None,
    event_id: str,
) -> list[tuple[BotIdentity, User | None]]:
    """Load a game's masters with their bot profiles, ordered by name."""

    identities = list(await events.list_leaders(event_id))
    profiles: Sequence[User | None] = (
        await asyncio.gather(*(users.get(identity.platform, identity.platform_user_id) for identity in identities))
        if users is not None
        else [None] * len(identities)
    )
    records = list(zip(identities, profiles, strict=True))
    return sorted(
        records,
        key=lambda record: (
            ((record[1].full_name or "") if record[1] is not None else "").casefold(),
            record[0].platform.value,
            record[0].platform_user_id,
        ),
    )


def master_profile_url(identity: BotIdentity, profile: User | None) -> str | None:
    """Link the profile on the platform where the master uses the bot."""

    if identity.platform is Platform.VK:
        return f"https://vk.com/id{identity.platform_user_id}"
    if isinstance(profile, TelegramUser) and profile.telegram_handle is not None:
        return f"https://t.me/{profile.telegram_handle.removeprefix('@')}"
    # A Telegram account without a public username has no plain-text link.
    return None


def event_masters_text(masters: Sequence[tuple[BotIdentity, User | None]]) -> str:
    lines = []
    for identity, profile in masters:
        name = surname_and_name(profile.full_name) if profile is not None and profile.full_name else None
        parts = [part for part in (name, master_profile_url(identity, profile)) if part is not None]
        if parts:
            lines.append(" — ".join(parts))
    return "Мастера игры:\n" + "\n".join(lines) if lines else ""


def master_contacts_section(masters: Sequence[tuple[BotIdentity, User | None]]) -> str:
    """Name the masters and ask players to warn them before skipping the game."""

    profiles = [url for identity, profile in masters if (url := master_profile_url(identity, profile)) is not None]
    contacts = f": {' '.join(profiles)} ." if profiles else "."
    disclaimer = (
        f"Если вы решите не участвовать в игре, пожалуйста сообщите об этом мастерам{contacts} {MASTER_NO_SHOW_WARNING}"
    )
    masters_text = event_masters_text(masters)
    return f"{masters_text}\n\n{disclaimer}" if masters_text else disclaimer


def confirmation_success_text(
    event: Event,
    character_wish: str,
    *,
    in_main_roster: bool,
    masters: Sequence[tuple[BotIdentity, User | None]],
) -> str:
    link_text = "Мастера скоро отправят вам ссылку на чат игры!\n\n" if in_main_roster else ""

    def compose(wish: str) -> str:
        return (
            f"✅ Участие в игре {event_reference(event.name, event.game_date)} подтверждено.\n\n"
            f"{link_text}"
            f"Пожелания по персонажу:\n{wish}\n\n"
            "Позже их можно изменить через «🎭 Пожелания по игре».\n\n"
            f"{master_contacts_section(masters)}"
        )

    text = compose(character_wish)
    excess = _message_length(text) - MAX_MESSAGE_LENGTH
    if excess <= 0:
        return text
    # Only the echo is shortened; the stored wish stays complete and the master contacts still fit.
    budget = _message_length(character_wish) - excess - 1
    kept: list[str] = []
    for character in character_wish:
        budget -= _message_length(character)
        if budget < 0:
            break
        kept.append(character)
    return compose("".join(kept).rstrip() + "…")


def enlistment_success_text(
    event: Event,
    queue_position: int,
    *,
    masters: Sequence[tuple[BotIdentity, User | None]],
    same_day_warning: bool = False,
) -> str:
    if event.public_table_public_url is None:
        raise RuntimeError("public game table is not initialized")
    if queue_position <= event.player_amount:
        position_text = (
            f"Вы записались на игру {event_reference(event.name, event.game_date)}!\n\n"
            "Сейчас вы в основном составе.\n\n"
            "Актуальный список записавшихся и состав можно посмотреть здесь: "
            f"{public_table_links(event).for_player()}"
        )
    else:
        cancellations_needed = queue_position - event.player_amount
        position_text = (
            f"Вы записались на игру {event_reference(event.name, event.game_date)}!\n\n"
            f"Сейчас вы в запасе. Перед вами: {cancellations_needed}.\n\n"
            "Если вы перейдёте в основной состав, бот сообщит об этом отдельным сообщением.\n\n"
            "Актуальный список записавшихся и состав можно посмотреть здесь: "
            f"{public_table_links(event).for_player()}"
        )
    if event.status is EventStatus.CONFIRMATION_OPEN:
        deadline_text = (
            f" до {format_confirmation_deadline(event.confirmation_deadline)} (мск)"
            if event.confirmation_deadline is not None
            else ""
        )
        confirmation_text = f"Когда будете уверены, что сможете прийти на игру, подтвердите участие{deadline_text}."
    else:
        confirmation_text = (
            "Подтверждение участия пока не открыто. Мы обязательно пришлём вам уведомление, "
            "когда можно будет подтвердиться."
        )
    text = (
        f"{position_text}\n\n{confirmation_text}\n\n"
        "Пожелания по соигрокам всегда можно изменить позже через «🎭 Пожелания по игре»."
    )
    if same_day_warning and event.game_date is not None:
        text = f"{text}\n\n{same_day_signup_warning(event.game_date)}"
    masters_text = event_masters_text(masters)
    return f"{text}\n\n{masters_text}" if masters_text else text


class ConfirmationNotificationService:
    """Deliver event notifications to participants and game masters."""

    NOTIFICATION_OPERATIONS = frozenset(
        {
            Operation.OPEN_CONFIRMATION,
            Operation.SCHEDULED_OPEN_CONFIRMATION,
            Operation.SEND_CONFIRMATION_REMINDER,
            Operation.SEND_CONFIRMED_NOTIFICATION,
            Operation.SEND_ALL_PLAYERS_NOTIFICATION,
        }
    )

    def __init__(
        self,
        events: EventRepository,
        catalog: RegistrationCatalog,
        users: UserRepository,
        transport: DeferredTransport,
        participant_secret: str,
        *,
        telegram_chat: TelegramChatGateway | None = None,
    ) -> None:
        self.events = events
        self.catalog = catalog
        self.users = users
        self.transport = transport
        self.participant_secret = participant_secret
        self.telegram_chat = telegram_chat

    @staticmethod
    def _identity(user: User) -> tuple[Platform, int]:
        if isinstance(user, TelegramUser):
            return Platform.TELEGRAM, user.tg_id
        return Platform.VK, user.vk_id

    async def _deliver(
        self,
        *,
        platform: Platform,
        user_id: int,
        request_id: str,
        text: str,
        buttons: Sequence[Button],
    ) -> bool:
        """Send one notification, reporting whether the recipient can receive it.

        A recipient whose platform refuses the message permanently is treated as
        finished by the caller: retrying only repeats the rejection, and the
        command would hold its FIFO message and stall every later command for
        the same game. Every other failure still propagates and is retried.
        """

        try:
            await self.transport.send(
                platform=platform,
                user_id=user_id,
                request_id=request_id,
                text=text,
                buttons=buttons,
            )
        except UnreachableRecipient as exc:
            LOGGER.warning(
                "notification_recipient_unreachable",
                extra={"platform": platform.value, "recipient_id": user_id, "rejection": str(exc)},
            )
            return False
        return True

    async def _recipients(
        self,
        event: Event,
        attendance_statuses: Collection[AttendanceStatus],
        *,
        main_roster_only: bool = False,
    ) -> list[NotificationRecipient]:
        await self.catalog.ensure_migrated(event)
        all_registrations = list(await self.catalog.registrations.list_for_event(event.event_id))
        main_roster_keys = (
            {
                registration.participant_key
                for registration in _active_main_roster(all_registrations, event.player_amount)
            }
            if main_roster_only
            else None
        )
        registrations = [
            registration
            for registration in all_registrations
            if registration.attendance_status in attendance_statuses
            and (main_roster_keys is None or registration.participant_key in main_roster_keys)
        ]
        registrations.sort(key=lambda registration: (registration.created_at, registration.participant_key))
        participant_keys = {registration.participant_key for registration in registrations}
        recipients: list[NotificationRecipient] = []
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            if key in participant_keys:
                recipients.append(NotificationRecipient(platform, uid, user))
        return recipients

    async def notify(self, command: OrderedRegistrationCommand) -> int:
        if command.operation not in self.NOTIFICATION_OPERATIONS:
            raise ValueError("command does not send a participant notification")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if command.operation is Operation.SEND_CONFIRMED_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            text = confirmed_notification_text(event.name, command.payload.text, event.game_date)
            attendance_statuses = {AttendanceStatus.CONFIRMED}
            main_roster_only = is_plain_chat_link(command.payload.text)
            buttons = [main_menu_button()]
        elif command.operation is Operation.SEND_ALL_PLAYERS_NOTIFICATION:
            assert isinstance(command.payload, NotificationPayload)
            text = player_notification_text(event.name, command.payload.text, event.game_date)
            attendance_statuses = {AttendanceStatus.WAITING, AttendanceStatus.CONFIRMED}
            main_roster_only = False
            buttons = [main_menu_button()]
        else:
            if event.confirmation_deadline is None:
                raise OperationNotAllowed("Для игры не задан дедлайн подтверждения")
            deadline = format_confirmation_deadline(event.confirmation_deadline)
            reference = event_reference(event.name, event.game_date)
            if command.operation in {Operation.OPEN_CONFIRMATION, Operation.SCHEDULED_OPEN_CONFIRMATION}:
                text = f"Подтверждение участия в игре {reference} открыто! Подтвердите участие до {deadline} (мск)."
            else:
                text = f"Напоминаем: подтвердите участие в игре {reference} до {deadline} (мск)."
            attendance_statuses = {AttendanceStatus.WAITING}
            main_roster_only = False
            buttons = [confirmation_button(event.event_id)]

        delivered = 0
        for recipient in await self._recipients(
            event,
            attendance_statuses,
            main_roster_only=main_roster_only,
        ):
            request_id = (
                f"{command.operation_id}:confirmation-notification:{recipient.platform.value}:{recipient.user_id}"
            )
            if recipient.user.last_delivery_operation_id == request_id:
                continue
            reached = await self._deliver(
                platform=recipient.platform,
                user_id=recipient.user_id,
                request_id=request_id,
                text=text,
                buttons=buttons,
            )
            # The marker is also written for a refused recipient so the rest of
            # the fan-out is not repeated for them on a later attempt.
            await self.users.claim_delivery(recipient.platform, recipient.user_id, request_id, buttons)
            if reached:
                delivered += 1
        return delivered

    async def notify_confirming_participant(
        self,
        command: OrderedRegistrationCommand,
        confirmation_text: str,
        confirmation_buttons: Sequence[Button],
    ) -> int:
        """Deliver confirmation success and every earlier master notification as one retry unit."""

        if command.operation is not Operation.CONFIRM:
            raise ValueError("command does not confirm a participant")
        user = await self.users.get(command.platform, command.platform_user_id)
        if user is not None and user.last_delivery_operation_id == command.operation_id:
            return 0
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")

        if not await self._deliver(
            platform=command.platform,
            user_id=command.platform_user_id,
            request_id=f"{command.operation_id}:confirmation-result",
            text=confirmation_text,
            buttons=confirmation_buttons,
        ):
            # The player cannot receive bot messages at all, so the rest of the
            # bundle is pointless. The command counts as answered and completes.
            await self.users.claim_delivery(
                command.platform,
                command.platform_user_id,
                command.operation_id,
                confirmation_buttons,
            )
            return 0
        registrations = list(await self.catalog.registrations.list_for_event(event.event_id))
        link_recipients = _chat_link_recipient_keys(registrations, event.player_amount)
        delivered_notifications = [
            (index, stored_text)
            for index, stored_text in enumerate(event.confirmed_notifications)
            if not is_plain_chat_link(stored_text) or command.participant_key in link_recipients
        ]
        for index, stored_text in delivered_notifications:
            if not await self._deliver(
                platform=command.platform,
                user_id=command.platform_user_id,
                request_id=f"{command.operation_id}:past-confirmed-notification:{index}",
                text=confirmed_notification_text(event.name, stored_text, event.game_date),
                buttons=[main_menu_button()],
            ):
                break
        # One command-level marker is written only after the complete bundle is
        # accepted. No per-notification/per-recipient delivery state is stored.
        delivered_buttons = [main_menu_button()] if delivered_notifications else confirmation_buttons
        await self.users.claim_delivery(
            command.platform,
            command.platform_user_id,
            command.operation_id,
            delivered_buttons,
        )
        return len(delivered_notifications)

    async def notify_reserve_promotion(
        self,
        command: OrderedRegistrationCommand,
        *,
        event_id: str | None = None,
    ) -> int:
        """Notify the one reserve player promoted by this roster mutation."""

        if command.operation not in {Operation.CANCEL, Operation.CONFIRM, Operation.REMOVE_PARTICIPANT}:
            raise ValueError("command does not promote a reserve participant")
        event = await self.events.get(event_id or command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_reserve_promotion_operation_id != command.operation_id:
            return 0
        if event.last_reserve_promotion_delivered_operation_id == command.operation_id:
            return 0
        promoted_key = event.last_reserve_promotion_participant_key
        if promoted_key is None:
            return 0

        recipient: NotificationRecipient | None = None
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            if participant_key(self.participant_secret, platform, uid, event.event_id) == promoted_key:
                recipient = NotificationRecipient(platform, uid, user)
                break
        if recipient is None:
            LOGGER.warning(
                "reserve_promotion_recipient_missing",
                extra={"event_id": event.event_id, "operation_id": command.operation_id},
            )
            return 0

        request_id = f"{command.operation_id}:reserve-promotion"
        if recipient.user.last_delivery_operation_id == request_id:
            return 0
        reached = await self._deliver(
            platform=recipient.platform,
            user_id=recipient.user_id,
            request_id=request_id,
            text=(
                "Место освободилось: вы перешли из запаса в основной состав игры "
                f"{event_reference(event.name, event.game_date)}.\n\n"
                "После подтверждения участия мастера отправят вам ссылку на чат игры отдельным сообщением.\n\n"
                "Посмотрите актуальный состав игроков: "
                f"{public_table_links(event).for_player()}"
            ),
            buttons=[main_menu_button()],
        )
        await self.events.mark_reserve_promotion_delivered(event.event_id, command.operation_id)
        await self.users.claim_delivery(
            recipient.platform,
            recipient.user_id,
            request_id,
            [main_menu_button()],
        )
        return 1 if reached else 0

    async def notify_confirmed_link_promotion(
        self,
        command: OrderedRegistrationCommand,
        *,
        event_id: str | None = None,
    ) -> int:
        """Replay chat links when a confirmed reserve player enters the confirmed-player limit."""

        if command.operation not in {Operation.CANCEL, Operation.CONFIRM, Operation.REMOVE_PARTICIPANT}:
            raise ValueError("command does not promote a confirmed link recipient")
        event = await self.events.get(event_id or command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_confirmed_link_promotion_operation_id != command.operation_id:
            return 0
        if event.last_confirmed_link_promotion_delivered_operation_id == command.operation_id:
            return 0
        promoted_key = event.last_confirmed_link_promotion_participant_key
        if promoted_key is None:
            return 0

        recipient: NotificationRecipient | None = None
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            if participant_key(self.participant_secret, platform, uid, event.event_id) == promoted_key:
                recipient = NotificationRecipient(platform, uid, user)
                break
        if recipient is None:
            LOGGER.warning(
                "confirmed_link_promotion_recipient_missing",
                extra={"event_id": event.event_id, "operation_id": command.operation_id},
            )
            return 0

        links = [text for text in event.confirmed_notifications if is_plain_chat_link(text)]
        last_request_id: str | None = None
        reached = True
        for index, link in enumerate(links):
            last_request_id = f"{command.operation_id}:confirmed-link-promotion:{index}"
            reached = await self._deliver(
                platform=recipient.platform,
                user_id=recipient.user_id,
                request_id=last_request_id,
                text=confirmed_notification_text(event.name, link, event.game_date),
                buttons=[main_menu_button()],
            )
            if not reached:
                break
        await self.events.mark_confirmed_link_promotion_delivered(event.event_id, command.operation_id)
        if last_request_id is not None:
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                last_request_id,
                [main_menu_button()],
            )
        return len(links) if reached else 0

    async def notify_masters_of_participation(self, command: OrderedRegistrationCommand) -> int:
        """Report a persisted signup or confirmation once per operation and global master."""

        if command.operation not in {Operation.ENLIST, Operation.CONFIRM}:
            raise ValueError("command does not enlist or confirm participation")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if command.participant_key is None:
            raise ValueError("participation command has no participant key")
        registration = await self.catalog.registrations.get(event.event_id, command.participant_key)
        if registration is None or registration.last_operation_id != command.operation_id:
            return 0
        expected = AttendanceStatus.WAITING if command.operation is Operation.ENLIST else AttendanceStatus.CONFIRMED
        if registration.attendance_status is not expected:
            return 0
        rows = await self.catalog.registrations.list_for_event(event.event_id)
        active = sorted(
            (row for row in rows if row.attendance_status is not AttendanceStatus.CANCELLED),
            key=lambda row: (row.created_at, row.participant_key),
        )
        position = next(
            index for index, row in enumerate(active, 1) if row.participant_key == registration.participant_key
        )
        parts = registration.display_name.split()
        surname = parts.pop(0) if parts else "не указан"
        player_name = f"{parts[0]} {surname}" if parts else surname
        if registration.nickname:
            player_name += f" ({registration.nickname})"
        action = "📝" if command.operation is Operation.ENLIST else "✅"
        verb = "записался на игру" if command.operation is Operation.ENLIST else "подтвердил участие в игре"
        roster = "Основной состав" if position <= event.player_amount else "Запас"
        text = (
            f"{action} {player_name} {verb} {event_reference(event.name, event.game_date)}.\n"
            f"{roster}, место {position}."
        )
        buttons = [Button(label="Управление игрой", value=f"ag:manage:{event.event_id}")]
        actor_identities = {
            (item.platform, item.platform_user_id)
            for item in await identities_for_local_user(self.users, command.platform, command.platform_user_id)
        }
        seen = set(actor_identities)
        delivered = 0
        records = await event_master_records(self.events, self.users, event.event_id)
        records.sort(key=lambda record: (record[0].platform is not Platform.TELEGRAM, record[0].platform_user_id))
        for identity, _ in records:
            identities = {
                (item.platform, item.platform_user_id)
                for item in await identities_for_local_user(self.users, identity.platform, identity.platform_user_id)
            }
            if seen.intersection(identities):
                continue
            seen.update(identities)
            # Stable for linked accounts even if the selected local recipient changes on retry.
            recipient = min(f"{platform.value}:{user_id}" for platform, user_id in identities)
            request_id = f"{command.operation_id}:master-participation:{recipient}"
            if await self.catalog.registrations.participation_notification_delivered(
                event.event_id, command.participant_key, request_id
            ):
                continue
            reached = await self._deliver(
                platform=identity.platform,
                user_id=identity.platform_user_id,
                request_id=request_id,
                text=text,
                buttons=buttons,
            )
            await self.catalog.registrations.mark_participation_notification_delivered(
                event.event_id, command.participant_key, request_id
            )
            if reached:
                await self.users.claim_delivery(identity.platform, identity.platform_user_id, request_id, buttons)
                delivered += 1
        return delivered

    async def _cancellation_masters(
        self,
        event_id: str,
        command: OrderedRegistrationCommand,
    ) -> list[tuple[BotIdentity, User | None]]:
        """Every master of the game except the player who cancelled."""

        return [
            (identity, profile)
            for identity, profile in await event_master_records(self.events, self.users, event_id)
            if (identity.platform, identity.platform_user_id) != (command.platform, command.platform_user_id)
        ]

    async def _command_player_name(self, command: OrderedRegistrationCommand) -> tuple[str, str | None]:
        player = await self.users.get(command.platform, command.platform_user_id)
        if player is None:
            return "не указан", None
        name = surname_and_name(player.full_name) if player.full_name is not None else "не указан"
        return name, player.nickname

    async def notify_masters_of_cancellation(self, command: OrderedRegistrationCommand) -> int:
        """Tell every master of the game that a player cancelled and whether they had confirmed."""

        if command.operation is not Operation.CANCEL:
            raise ValueError("command does not cancel participation")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_participant_removal_operation_id != command.operation_id:
            return 0
        cancellation = next(
            (
                parsed
                for parsed in map(_parse_cancellation_snapshot_entry, event.participant_removal_keys)
                if parsed is not None
            ),
            None,
        )
        if cancellation is None:
            return 0
        confirmed, key = cancellation
        registration = await self.catalog.registrations.get(event.event_id, key)
        if registration is not None:
            player_name, nickname = registration.display_name, registration.nickname
        else:
            player_name, nickname = await self._command_player_name(command)
        text = cancellation_master_text(event, player_name, nickname, confirmed=confirmed)

        delivered = 0
        notified_keys = set(event.participant_removal_notified_keys)
        for identity, _ in await self._cancellation_masters(event.event_id, command):
            recipient = f"{identity.platform.value}:{identity.platform_user_id}"
            marker = f"{CANCELLATION_MASTER_PREFIX}{recipient}"
            if marker in notified_keys:
                continue
            request_id = f"{command.operation_id}:cancellation-master:{recipient}"
            reached = await self._deliver(
                platform=identity.platform,
                user_id=identity.platform_user_id,
                request_id=request_id,
                text=text,
                buttons=[main_menu_button()],
            )
            await self.events.mark_participant_removal_delivered(event.event_id, command.operation_id, marker)
            notified_keys.add(marker)
            await self.users.claim_delivery(
                identity.platform,
                identity.platform_user_id,
                request_id,
                [main_menu_button()],
            )
            if reached:
                delivered += 1
        return delivered

    async def kick_cancelled_participant(
        self,
        command: OrderedRegistrationCommand,
        *,
        event_id: str | None = None,
        confirmed: bool | None = None,
    ) -> bool:
        """Remove a formerly confirmed global user's Telegram profile from its game chat."""

        if command.operation not in {Operation.CANCEL, Operation.CONFIRM}:
            raise ValueError("command does not cancel confirmed participation")
        event = await self.events.get(event_id or command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if self.telegram_chat is None or event.telegram_chat_id is None:
            return False
        cancelled_participant_key: str | None = None
        if confirmed is None:
            if command.operation is not Operation.CANCEL:
                raise ValueError("replacement cancellation must state its prior status")
            if event.last_participant_removal_operation_id != command.operation_id:
                return False
            cancellation = next(
                (
                    parsed
                    for parsed in map(_parse_cancellation_snapshot_entry, event.participant_removal_keys)
                    if parsed is not None
                ),
                None,
            )
            confirmed = cancellation is not None and cancellation[0]
            cancelled_participant_key = cancellation[1] if cancellation is not None else None
        if not confirmed:
            return False

        telegram_user_id: int | None = None
        if command.platform is Platform.TELEGRAM:
            telegram_user_id = command.platform_user_id
        else:
            local_user = await self.users.get(command.platform, command.platform_user_id)
            if local_user is not None:
                for profile in await global_users(self.users, local_user):
                    if isinstance(profile, TelegramUser):
                        telegram_user_id = profile.tg_id
                        break
            if telegram_user_id is None:
                key = cancelled_participant_key or participant_key(
                    self.participant_secret,
                    command.platform,
                    command.platform_user_id,
                    event.event_id,
                )
                registration = await self.catalog.registrations.get(event.event_id, key)
                if registration is not None and registration.telegram_profile is not None:
                    telegram_user = await self.users.find_telegram_by_handle(registration.telegram_profile)
                    if telegram_user is not None:
                        telegram_user_id = telegram_user.tg_id
        if telegram_user_id is None:
            return False
        return await self.telegram_chat.kick_member(
            chat_id=event.telegram_chat_id,
            user_id=telegram_user_id,
            request_id=f"{command.operation_id}:kick:{event.event_id}:{telegram_user_id}",
        )

    async def notify_masters_of_replaced_participation(
        self,
        command: OrderedRegistrationCommand,
        *,
        event_id: str,
    ) -> int:
        """Tell the masters of a same-day game that its confirmed player switched to another game."""

        if command.operation is not Operation.CONFIRM:
            raise ValueError("command does not replace a confirmed participation")
        event = await self.events.get(event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        # The replaced registration is deleted by the confirmation, and only a
        # confirmed participation can be replaced.
        player_name, nickname = await self._command_player_name(command)
        text = cancellation_master_text(
            event,
            player_name,
            nickname,
            confirmed=True,
            same_day_replacement=True,
        )

        delivered = 0
        for identity, profile in await self._cancellation_masters(event.event_id, command):
            request_id = (
                f"{command.operation_id}:cancellation-master:{identity.platform.value}:{identity.platform_user_id}"
            )
            # Confirmations run in a per-player FIFO group, so the per-event retry
            # snapshot cannot be used; the delivery marker suppresses a repeat.
            if profile is not None and profile.last_delivery_operation_id == request_id:
                continue
            reached = await self._deliver(
                platform=identity.platform,
                user_id=identity.platform_user_id,
                request_id=request_id,
                text=text,
                buttons=[main_menu_button()],
            )
            await self.users.claim_delivery(
                identity.platform,
                identity.platform_user_id,
                request_id,
                [main_menu_button()],
            )
            if reached:
                delivered += 1
        return delivered

    async def notify_capacity_promotions(self, command: OrderedRegistrationCommand) -> int:
        """Notify every reserve participant moved into the enlarged main roster."""

        if command.operation is not Operation.UPDATE_PLAYER_AMOUNT:
            raise ValueError("command does not update player capacity")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_participant_removal_operation_id != command.operation_id:
            return 0

        promoted_keys = set(event.participant_removal_keys)
        notified_keys = set(event.participant_removal_notified_keys)
        registrations = {
            registration.participant_key: registration
            for registration in await self.catalog.registrations.list_for_event(event.event_id)
            if registration.participant_key in promoted_keys
        }
        recipients: dict[str, NotificationRecipient] = {}
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            if key in promoted_keys:
                recipients[key] = NotificationRecipient(platform, uid, user)

        delivered = 0
        chat_links = [text for text in event.confirmed_notifications if is_plain_chat_link(text)]
        for key in event.participant_removal_keys:
            if key in notified_keys:
                continue
            recipient = recipients.get(key)
            if recipient is None:
                LOGGER.warning(
                    "capacity_promotion_recipient_missing",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            messages = [
                (
                    "Количество мест увеличилось: вы перешли из запаса в основной состав игры "
                    f"{event_reference(event.name, event.game_date)}.\n\n"
                    "Посмотрите актуальный состав игроков: "
                    f"{public_table_links(event).for_player()}"
                )
            ]
            registration = registrations.get(key)
            if registration is not None and registration.attendance_status is AttendanceStatus.CONFIRMED:
                messages.extend(confirmed_notification_text(event.name, link, event.game_date) for link in chat_links)
            last_request_id = ""
            reached = True
            for index, text in enumerate(messages):
                last_request_id = (
                    f"{command.operation_id}:capacity-promotion:{recipient.platform.value}:{recipient.user_id}:{index}"
                )
                reached = await self._deliver(
                    platform=recipient.platform,
                    user_id=recipient.user_id,
                    request_id=last_request_id,
                    text=text,
                    buttons=[main_menu_button()],
                )
                if not reached:
                    break
            # A refused recipient is recorded like a notified one: the snapshot
            # exists to stop repeats, and retrying a permanent rejection would
            # keep this capacity change ahead of every later command.
            await self.events.mark_participant_removal_delivered(
                event.event_id,
                command.operation_id,
                key,
            )
            notified_keys.add(key)
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                last_request_id,
                [main_menu_button()],
            )
            if reached:
                delivered += 1
        return delivered

    async def notify_removed_participants(self, command: OrderedRegistrationCommand) -> int:
        if command.operation is not Operation.REMOVE_PARTICIPANT:
            raise ValueError("command does not remove participants")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_participant_removal_operation_id != command.operation_id:
            return 0

        recipients: dict[str, NotificationRecipient] = {}
        participant_keys = set(event.participant_removal_keys)
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            if key in participant_keys:
                recipients[key] = NotificationRecipient(platform, uid, user)

        text = f"Ваша запись на игру {event_reference(event.name, event.game_date)} удалена."

        delivered = 0
        notified_keys = set(event.participant_removal_notified_keys)
        for key in event.participant_removal_keys:
            if key in notified_keys:
                continue
            recipient = recipients.get(key)
            if recipient is None:
                LOGGER.warning(
                    "participant_removal_recipient_missing",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            request_id = f"{command.operation_id}:participant-removal:{recipient.platform.value}:{recipient.user_id}"
            reached = await self._deliver(
                platform=recipient.platform,
                user_id=recipient.user_id,
                request_id=request_id,
                text=text,
                buttons=[main_menu_button()],
            )
            await self.events.mark_participant_removal_delivered(
                event.event_id,
                command.operation_id,
                key,
            )
            notified_keys.add(key)
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                request_id,
                [main_menu_button()],
            )
            if reached:
                delivered += 1
        return delivered

    async def notify_sifted_participants(self, command: OrderedRegistrationCommand) -> int:
        if command.operation not in {
            Operation.REMOVE_WAITING_PARTICIPANTS,
            Operation.PROMOTE_RESERVE_PARTICIPANT,
        }:
            raise ValueError("command does not sift participants")
        event = await self.events.get(command.event_id)
        if event is None:
            raise EventNotFound("Игра не найдена")
        if event.last_participant_removal_operation_id != command.operation_id:
            return 0

        users_by_key: dict[str, NotificationRecipient] = {}
        for user in await self.users.list_all():
            platform, uid = self._identity(user)
            key = participant_key(self.participant_secret, platform, uid, event.event_id)
            users_by_key[key] = NotificationRecipient(platform, uid, user)

        delivered = 0
        notified_entries = set(event.participant_removal_notified_keys)
        chat_links = [text for text in event.confirmed_notifications if is_plain_chat_link(text)]
        current_entries = _current_sift_entries(
            event.participant_removal_keys,
            await self.catalog.registrations.list_for_event(event.event_id),
            event.player_amount,
        )
        for snapshot_entry in event.participant_removal_keys:
            if snapshot_entry in notified_entries:
                continue
            parsed = _parse_sift_snapshot_entry(snapshot_entry)
            if parsed is None:
                LOGGER.warning(
                    "confirmation_sift_snapshot_invalid",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            if snapshot_entry not in current_entries:
                LOGGER.info(
                    "confirmation_sift_move_outdated",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            transition, key = parsed
            recipient = users_by_key.get(key)
            if recipient is None:
                LOGGER.warning(
                    "confirmation_sift_recipient_missing",
                    extra={"event_id": event.event_id, "operation_id": command.operation_id},
                )
                continue
            if transition == SIFT_PROMOTED_PREFIX:
                messages = [
                    (
                        "Вы перешли из запаса в основной состав игры "
                        f"{event_reference(event.name, event.game_date)}.\n\n"
                        "Посмотрите актуальный состав игроков: "
                        f"{public_table_links(event).for_player()}"
                    ),
                    *(confirmed_notification_text(event.name, link, event.game_date) for link in chat_links),
                ]
                transition_name = "promotion"
            else:
                if command.operation is Operation.PROMOTE_RESERVE_PARTICIPANT:
                    demotion_text = (
                        "Мастера переместили вашу неподтверждённую заявку в запас игры "
                        f"{event_reference(event.name, event.game_date)}."
                    )
                else:
                    demotion_text = (
                        "Вы не успели подтвердить или отменить ваше участие в игре "
                        f"«{event.name}». Мы были вынуждены переместить вашу заявку в запас"
                    )
                messages = [demotion_text]
                transition_name = "demotion"
            last_request_id = ""
            reached = True
            for index, text in enumerate(messages):
                last_request_id = (
                    f"{command.operation_id}:confirmation-sift:{transition_name}:"
                    f"{recipient.platform.value}:{recipient.user_id}:{index}"
                )
                reached = await self._deliver(
                    platform=recipient.platform,
                    user_id=recipient.user_id,
                    request_id=last_request_id,
                    text=text,
                    buttons=[main_menu_button()],
                )
                if not reached:
                    break
            await self.events.mark_participant_removal_delivered(
                event.event_id,
                command.operation_id,
                snapshot_entry,
            )
            notified_entries.add(snapshot_entry)
            await self.users.claim_delivery(
                recipient.platform,
                recipient.user_id,
                last_request_id,
                [main_menu_button()],
            )
            if reached:
                delivered += 1
        return delivered
