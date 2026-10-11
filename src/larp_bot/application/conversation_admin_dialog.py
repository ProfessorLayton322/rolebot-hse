from __future__ import annotations

from datetime import date, datetime

from larp_bot.domain.models import (
    MAX_PLAYER_AMOUNT,
    BotIdentity,
    BotResponse,
    Button,
    ConfirmationDeadlinePayload,
    EmptyPayload,
    EventDetailsPayload,
    EventStatus,
    InboundMessage,
    NotificationPayload,
    Operation,
    Platform,
    PlayerAmountPayload,
    PrivilegedRole,
    ReplyContext,
    User,
    event_reference,
    normalize_telegram_post_url,
    normalize_vk_post_url,
    parse_game_date,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    ADMIN_STATUS_CHOICES,
    ARCHIVED_GAMES,
    BACK,
    GAMEMASTER_REVOKED_NOTIFICATION,
    MANAGE_GAMES,
    MANAGE_MODERATORS,
    PRIVILEGED_ROLE_LABELS,
    SCHEDULED_TIME_INVALID,
    SEND_CONFIRMED_LINK,
    STATUS_CONFIRMATION,
    _admin_status_response,
    _confirmation_deadline_response,
    _confirmed_notification_preview,
    _confirmed_notification_prompt,
    _context_event_reference,
    _game_date_response,
    _game_details_date_response,
    _scheduled_close_response,
    _scheduled_confirmation_deadline_response,
    edit_game_button,
    game_management_button,
    notification_management_button,
)
from .deadlines import (
    MOSCOW_TIMEZONE,
    closest_thursday_19,
    format_confirmation_deadline,
    next_night_03,
    next_saturday_game_date,
    parse_confirmation_deadline,
    parse_scheduled_action_time,
)
from .identity import global_users
from .services import confirmed_notification_text, is_plain_chat_link, player_notification_text


class AdministrationDialogMixin(ConversationContext):
    """Administration callback and free-text dialog routing."""

    async def _admin_callback(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse | None:
        parts = value.split(":")
        try:
            if value.startswith("master:self-add:") and len(parts) == 3:
                if not await self._is_admin(user):
                    return BotResponse(text="Недостаточно прав.")
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                return await self._assign_event_leader(user, message, event, user)
            if value.startswith("mod:list-page:"):
                return await self._show_moderators(user, "list", offset=int(parts[2]))
            if value.startswith("mod:remove-page:"):
                return await self._show_moderators(user, "remove", offset=int(parts[2]))
            if value.startswith("mod:view:") and len(parts) == 4:
                if not await self._is_admin(user) or user.dialog_state != "ADMIN_MODERATOR_LIST":
                    return BotResponse(text="Недостаточно прав.")
                platform = self._platform_from_code(parts[2])
                target = await self.users.get(platform, int(parts[3]))
                if target is None:
                    return BotResponse(text="Пользователь не найден.")
                role = await self._privileged_role(target)
                if role is None:
                    return await self._show_moderators(user, "list")
                offset = int(user.dialog_context.get("moderator_offset", 0))
                return BotResponse(
                    text=self._profile_text(PRIVILEGED_ROLE_LABELS[role], self._identity(target), target),
                    buttons=[Button(label=BACK, value=f"mod:list-page:{offset}")],
                )
            if value.startswith("mod:remove-pick:") and len(parts) == 4:
                if not await self._is_admin(user) or user.dialog_state != "ADMIN_MODERATOR_REMOVE":
                    return BotResponse(text="Недостаточно прав.")
                platform = self._platform_from_code(parts[2])
                target = await self.users.get(platform, int(parts[3]))
                if target is None:
                    return await self._show_moderators(user, "remove")
                role = target.effective_privileged_role
                if role is None or await self._is_admin(target):
                    return await self._show_moderators(user, "remove")
                user.dialog_state = "ADMIN_MODERATOR_REMOVE_CONFIRM"
                user.dialog_context["target_platform"] = platform.value
                user.dialog_context["target_user_id"] = int(parts[3])
                user.dialog_context["target_role"] = role.value
                confirmation = (
                    f"Снять полномочия модератора с пользователя «{target.full_name}»?"
                    if role is PrivilegedRole.GAMEMASTER
                    else f"Снять роль «{PRIVILEGED_ROLE_LABELS[role]}» с пользователя «{target.full_name}»?"
                )
                return BotResponse(
                    text=confirmation,
                    buttons=[
                        Button(label="Да, снять", value="mod:remove-confirm"),
                        Button(label=BACK, value=f"mod:remove-page:{user.dialog_context['moderator_offset']}"),
                    ],
                )
            if value == "mod:remove-confirm":
                if not await self._is_admin(user) or user.dialog_state != "ADMIN_MODERATOR_REMOVE_CONFIRM":
                    return BotResponse(text="Недостаточно прав.")
                platform = Platform(str(user.dialog_context["target_platform"]))
                target_id = int(user.dialog_context["target_user_id"])
                target = await self.users.get(platform, target_id)
                removed_role = PrivilegedRole(str(user.dialog_context["target_role"]))
                request_id = (
                    f"gamemaster-revoke:{platform.value}:{target_id}:{message.update_id}"
                    if removed_role is PrivilegedRole.GAMEMASTER
                    else f"privileged-role-revoke:{removed_role.value}:{platform.value}:{target_id}:{message.update_id}"
                )
                if target is None:
                    await self._clear(user)
                    return BotResponse(
                        text="У этого пользователя уже нет назначенной роли.",
                        buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
                    )
                target_profiles = await global_users(self.users, target)
                profiles_with_role = [
                    profile for profile in target_profiles if profile.effective_privileged_role is not None
                ]
                if profiles_with_role:
                    for linked_profile in profiles_with_role:
                        identity = self._identity(linked_profile)
                        await self.users.clear_privileged_role(
                            identity.platform,
                            identity.platform_user_id,
                            request_id,
                        )
                    refreshed = await self.users.get(platform, target_id)
                    if refreshed is None or refreshed.effective_privileged_role is not None:
                        raise RuntimeError("privileged role revocation was not persisted")
                    target = refreshed
                elif target.gamemaster_grant_operation_id != request_id:
                    await self._clear(user)
                    return BotResponse(
                        text="У этого пользователя уже нет назначенной роли.",
                        buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
                    )
                if target.last_delivery_operation_id != request_id:
                    await self._notify_status_change(
                        user,
                        target,
                        request_id=request_id,
                        text=(
                            GAMEMASTER_REVOKED_NOTIFICATION
                            if removed_role is PrivilegedRole.GAMEMASTER
                            else "С вас снята привилегированная роль."
                        ),
                    )
                if self._identity(user) == self._identity(target):
                    user.privileged_role = None
                    user.is_gamemaster = False
                    user.gamemaster_grant_operation_id = request_id
                await self._clear(user)
                return BotResponse(
                    text=(
                        "✅ Полномочия модератора сняты."
                        if removed_role is PrivilegedRole.GAMEMASTER
                        else "✅ Назначенная роль снята."
                    ),
                    buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
                )
            if value.startswith("master:add:") and len(parts) == 3:
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                denied = await self._event_leader_required(user, event.event_id)
                return denied or await self._show_event_master_candidates(user, event)
            if value.startswith("master:remove:") and len(parts) == 3:
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                denied = await self._event_leader_required(user, event.event_id)
                return denied or await self._show_event_masters(user, event, "remove")
            if value.startswith("master:list:") and len(parts) == 3:
                event = await self.events.get(parts[2])
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                if event.archived_at is not None:
                    return BotResponse(text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.")
                if not await self._can_view_event_details(user, event.event_id):
                    return BotResponse(text="Недостаточно прав.")
                return await self._show_event_masters(user, event, "list")
            if value.startswith("master:add-page:"):
                event = await self.events.get(str(user.dialog_context["event_id"]))
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                return await self._show_event_master_candidates(user, event, offset=int(parts[2]))
            if value.startswith("master:add-pick:") and len(parts) == 4:
                if user.dialog_state != "ADMIN_EVENT_LEADER_ADD_PICK":
                    return BotResponse(text="Некорректное действие.")
                event = await self.events.get(str(user.dialog_context["event_id"]))
                platform = self._platform_from_code(parts[2])
                target = await self.users.get(platform, int(parts[3]))
                if event is None or target is None:
                    return BotResponse(text="Пользователь или игра не найдены.")
                return await self._assign_event_leader(user, message, event, target)
            if value in {"master:promote:yes", "master:promote:no"}:
                if user.dialog_state != "ADMIN_EVENT_LEADER_PROMOTE_CONFIRM":
                    return BotResponse(text="Некорректное действие.")
                event = await self.events.get(str(user.dialog_context["event_id"]))
                if event is None:
                    await self._clear(user)
                    return BotResponse(text="Игра не найдена.")
                if value == "master:promote:no":
                    return await self._show_event_master_candidates(
                        user,
                        event,
                        offset=int(user.dialog_context.get("master_offset", 0)),
                        notice=("Добавление мастера отменено. Выберите другого пользователя.\n\n"),
                    )
                platform = Platform(str(user.dialog_context["promotion_target_platform"]))
                target = await self.users.get(platform, int(user.dialog_context["promotion_target_user_id"]))
                if target is None:
                    return await self._show_event_master_candidates(
                        user,
                        event,
                        notice="Пользователь больше не найден. Выберите другого.\n\n",
                    )
                target = await self._promote_master_target(user, message, target)
                return await self._assign_event_leader(user, message, event, target)
            if value == "master:add-direct":
                if user.dialog_state != "ADMIN_EVENT_LEADER_ADD_PICK":
                    return BotResponse(text="Некорректное действие.")
                user.dialog_state = "ADMIN_EVENT_LEADER_PLATFORM"
                return self._event_leader_platform_prompt(str(user.dialog_context["event_id"]))
            if value.startswith("master:remove-page:") or value.startswith("master:list-page:"):
                mode = "remove" if parts[1] == "remove-page" else "list"
                event = await self.events.get(str(user.dialog_context["event_id"]))
                if event is None:
                    return BotResponse(text="Игра не найдена.")
                return await self._show_event_masters(user, event, mode, offset=int(parts[2]))
            if value.startswith("master:view:") and len(parts) == 4:
                if user.dialog_state != "ADMIN_EVENT_LEADER_LIST":
                    return BotResponse(text="Некорректное действие.")
                platform = self._platform_from_code(parts[2])
                identity = BotIdentity(platform=platform, platform_user_id=int(parts[3]))
                profile = await self.users.get(platform, identity.platform_user_id)
                offset = int(user.dialog_context.get("master_offset", 0))
                return BotResponse(
                    text=self._profile_text("Мастер", identity, profile),
                    buttons=[Button(label=BACK, value=f"master:list-page:{offset}")],
                )
            if value.startswith("master:remove-pick:") and len(parts) == 4:
                if user.dialog_state != "ADMIN_EVENT_LEADER_REMOVE":
                    return BotResponse(text="Некорректное действие.")
                platform = self._platform_from_code(parts[2])
                target_id = int(parts[3])
                target = await self.users.get(platform, target_id)
                event_id = str(user.dialog_context["event_id"])
                identity = BotIdentity(platform=platform, platform_user_id=target_id)
                if target is None or identity not in await self.events.list_leaders(event_id):
                    event = await self.events.get(event_id)
                    if event is None:
                        return BotResponse(text="Игра не найдена.")
                    return await self._show_event_masters(user, event, "remove")
                user.dialog_state = "ADMIN_EVENT_LEADER_REMOVE_CONFIRM"
                user.dialog_context["target_platform"] = platform.value
                user.dialog_context["target_user_id"] = target_id
                return BotResponse(
                    text=f"Удалить мастера «{target.full_name}» из этой игры?",
                    buttons=[
                        Button(label="Да, удалить", value="master:remove-confirm"),
                        Button(
                            label=BACK,
                            value=f"master:remove-page:{user.dialog_context['master_offset']}",
                        ),
                    ],
                )
            if value == "master:remove-confirm":
                if user.dialog_state != "ADMIN_EVENT_LEADER_REMOVE_CONFIRM":
                    return BotResponse(text="Некорректное действие.")
                event_id = str(user.dialog_context["event_id"])
                event_name = str(user.dialog_context["event_name"])
                event = await self.events.get(event_id)
                if event is None:
                    await self._clear(user)
                    return BotResponse(text="Игра не найдена.")
                reference = event_reference(event_name, event.game_date)
                platform = Platform(str(user.dialog_context["target_platform"]))
                target_id = int(user.dialog_context["target_user_id"])
                identity = BotIdentity(platform=platform, platform_user_id=target_id)
                target = await self.users.get(platform, target_id)
                request_id = f"leader-revoke:{event_id}:{platform.value}:{target_id}:{message.update_id}"
                pending = user.dialog_context.get("pending_leader_revoke") == request_id
                if target is None:
                    await self._clear(user)
                    return BotResponse(
                        text="Этот пользователь уже не является мастером этой игры.",
                        buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                    )
                if identity in await self.events.list_leaders(event_id):
                    user.dialog_context["pending_leader_revoke"] = request_id
                    await self.users.save(user)
                    if not await self.events.remove_leader(event_id, identity):
                        raise RuntimeError("event leader revocation was not persisted")
                    pending = True
                elif not pending:
                    await self._clear(user)
                    return BotResponse(
                        text="Этот пользователь уже не является мастером этой игры.",
                        buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                    )
                await self.administration.refresh_pass_table(event_id)
                if target.last_delivery_operation_id != request_id:
                    await self._notify_status_change(
                        user,
                        target,
                        request_id=request_id,
                        text=f"Вы больше не являетесь мастером игры {reference}.",
                    )
                await self._clear(user)
                return BotResponse(
                    text=f"✅ Мастер удалён из игры {reference}.",
                    buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                )
        except (KeyError, TypeError, ValueError):
            return BotResponse(text="Некорректное действие.")
        return None

    async def _admin_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._has_admin_access(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        event_bound_states = {
            "ADMIN_STATUS_SELECT",
            "ADMIN_UPDATE_GAME_NAME",
            "ADMIN_UPDATE_GAME_DATE",
            "ADMIN_UPDATE_PLAYER_AMOUNT",
            "ADMIN_POST_LINK_TELEGRAM",
            "ADMIN_POST_LINK_VK",
            "ADMIN_CONFIRMATION_DEADLINE",
            "ADMIN_SCHEDULE_REGISTRATION_TIME",
            "ADMIN_SCHEDULE_CONFIRMATION_TIME",
            "ADMIN_SCHEDULE_CONFIRMATION_DEADLINE",
            "ADMIN_SCHEDULE_CLOSE_TIME",
            "ADMIN_NOTIFICATION_LINK_TEXT",
            "ADMIN_NOTIFICATION_MESSAGE_TEXT",
            "ADMIN_NOTIFICATION_CONFIRM",
            "ADMIN_NOTIFICATION_TEXT",
            "ADMIN_ARCHIVE_NAME",
            "ADMIN_DELETE_NAME",
            "ADMIN_EVENT_LEADER_PLATFORM",
            "ADMIN_EVENT_LEADER_PROFILE",
            "ADMIN_EVENT_LEADER_ADD_PICK",
            "ADMIN_EVENT_LEADER_PROMOTE_CONFIRM",
            "ADMIN_EVENT_LEADER_REMOVE",
            "ADMIN_EVENT_LEADER_REMOVE_CONFIRM",
            "ADMIN_EVENT_LEADER_LIST",
            "ADMIN_REMOVE_PICK",
            "ADMIN_REMOVE_CONFIRM",
            "ADMIN_REMOVE_WAITING_CONFIRM",
            "ADMIN_WAITING_PICK",
            "ADMIN_PROFILES_PICK",
            "ADMIN_RESERVE_PROMOTION_PICK",
            "ADMIN_RESERVE_PROMOTION_CONFIRM",
        }
        view_only_event_states = {
            "ADMIN_EVENT_LEADER_LIST",
            "ADMIN_WAITING_PICK",
            "ADMIN_PROFILES_PICK",
        }
        if user.dialog_state in event_bound_states:
            event_id = str(user.dialog_context["event_id"])
            event = await self.events.get(event_id)
            deleting_archived = (
                user.dialog_state == "ADMIN_DELETE_NAME" and event is not None and event.archived_at is not None
            )
            if user.dialog_state == "ADMIN_DELETE_NAME" and event is None:
                await self._clear(user)
                return BotResponse(
                    text="Игра уже удалена.",
                    buttons=[Button(label=BACK, value=ARCHIVED_GAMES)],
                )
            if event is not None and event.archived_at is not None and not deleting_archived:
                await self._clear(user)
                if not await self._is_admin(user):
                    return BotResponse(text="Игра не найдена.")
                return BotResponse(
                    text=f"Игра {event_reference(event.name, event.game_date)} уже архивирована.",
                    buttons=[Button(label=BACK, value=MANAGE_GAMES)],
                )
            pending_self_removal = (
                user.dialog_state == "ADMIN_EVENT_LEADER_REMOVE_CONFIRM"
                and "pending_leader_revoke" in user.dialog_context
                and user.dialog_context.get("target_platform") == self._identity(user).platform.value
                and user.dialog_context.get("target_user_id") == self._identity(user).platform_user_id
            )
            if deleting_archived and not await self._is_admin(user):
                await self._clear(user)
                return BotResponse(text="Недостаточно прав.")
            if not deleting_archived and not pending_self_removal:
                if user.dialog_state in view_only_event_states:
                    if not await self._can_view_event_details(user, event_id):
                        await self._clear(user)
                        return BotResponse(text="Недостаточно прав.")
                else:
                    denied = await self._event_leader_required(user, event_id)
                    if denied is not None:
                        return denied
        if value.startswith(("mod:", "master:")):
            admin_response = await self._admin_callback(user, message, value)
            if admin_response is not None:
                return admin_response
        if user.dialog_state == "ADMIN_GAMEMASTER_PLATFORM":
            if not await self._is_admin(user):
                await self._clear(user)
                return BotResponse(text="Недостаточно прав.")
            platforms = {
                "admin:gamemaster:telegram": Platform.TELEGRAM,
                "admin:gamemaster:vk": Platform.VK,
            }
            target_platform = platforms.get(value)
            role = PrivilegedRole(str(user.dialog_context.get("privileged_role", PrivilegedRole.GAMEMASTER.value)))
            if target_platform is None:
                return BotResponse(
                    text=f"Выберите бот, которым пользуется будущий {PRIVILEGED_ROLE_LABELS[role].casefold()}:",
                    buttons=[
                        Button(label="Telegram", value="admin:gamemaster:telegram"),
                        Button(label="VK", value="admin:gamemaster:vk"),
                        Button(label=BACK, value=MANAGE_MODERATORS),
                    ],
                )
            user.dialog_state = "ADMIN_GAMEMASTER_PROFILE"
            user.dialog_context["gamemaster_platform"] = target_platform.value
            role_label = PRIVILEGED_ROLE_LABELS[role].casefold()
            if target_platform is Platform.TELEGRAM:
                prompt = f"Отправьте ссылку на профиль Telegram или @username будущего {role_label}:"
            else:
                prompt = f"Отправьте ссылку на профиль VK будущего {role_label}:"
            return BotResponse(text=prompt, buttons=[Button(label=BACK, value=MANAGE_MODERATORS)])
        if user.dialog_state == "ADMIN_GAMEMASTER_PROFILE":
            return await self._grant_gamemaster(user, message, value)
        if user.dialog_state == "ADMIN_EVENT_LEADER_PLATFORM":
            platforms = {
                "admin:event-leader:telegram": Platform.TELEGRAM,
                "admin:event-leader:vk": Platform.VK,
            }
            target_platform = platforms.get(value)
            if target_platform is None:
                return self._event_leader_platform_prompt(str(user.dialog_context["event_id"]))
            user.dialog_state = "ADMIN_EVENT_LEADER_PROFILE"
            user.dialog_context["gamemaster_platform"] = target_platform.value
            if target_platform is Platform.TELEGRAM:
                prompt = "Отправьте ссылку на профиль Telegram или @username будущего мастера:"
            else:
                prompt = "Отправьте ссылку на профиль VK будущего мастера:"
            return BotResponse(
                text=prompt,
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )
        if user.dialog_state == "ADMIN_EVENT_LEADER_PROFILE":
            return await self._add_event_leader(user, message, value)
        if user.dialog_state == "ADMIN_EVENT_LEADER_PROMOTE_CONFIRM":
            platform = Platform(str(user.dialog_context["promotion_target_platform"]))
            target = await self.users.get(platform, int(user.dialog_context["promotion_target_user_id"]))
            event = await self.events.get(str(user.dialog_context["event_id"]))
            if target is None or event is None:
                await self._clear(user)
                return BotResponse(text="Пользователь или игра не найдены.")
            return self._master_promotion_question(
                user,
                target,
                state="ADMIN_EVENT_LEADER_PROMOTE_CONFIRM",
                reference=event_reference(event.name, event.game_date),
                confirm_value="master:promote:yes",
                decline_value="master:promote:no",
            )
        if user.dialog_state == "ADMIN_REMOVE_PICK":
            return await self._admin_remove_pick(user, message, value)
        if user.dialog_state == "ADMIN_REMOVE_CONFIRM":
            return await self._admin_remove_confirm(user, message, value)
        if user.dialog_state == "ADMIN_REMOVE_WAITING_CONFIRM":
            return await self._admin_remove_waiting_confirm(user, message, value)
        if user.dialog_state == "ADMIN_WAITING_PICK":
            return await self._admin_waiting_pick(user, value)
        if user.dialog_state == "ADMIN_PROFILES_PICK":
            return await self._admin_profiles_pick(user, value)
        if user.dialog_state == "ADMIN_RESERVE_PROMOTION_PICK":
            return await self._admin_reserve_promotion_pick(user, value)
        if user.dialog_state == "ADMIN_RESERVE_PROMOTION_CONFIRM":
            return await self._admin_reserve_promotion_confirm(user, message, value)
        if user.dialog_state in {
            "ADMIN_CREATE_MASTER_CHOICE",
            "ADMIN_CREATE_MASTER_PICK",
            "ADMIN_CREATE_MASTER_PLATFORM",
            "ADMIN_CREATE_MASTER_PROFILE",
            "ADMIN_CREATE_MASTER_PROMOTE_CONFIRM",
        }:
            return await self._creation_master_step(user, message, value)
        if user.dialog_state in {"ADMIN_CREATE_SCHEDULE_CHOICE", "ADMIN_CREATE_SCHEDULE_TIME"}:
            return await self._creation_schedule_step(user, message, value)
        if user.dialog_state in {
            "ADMIN_CREATE_CONFIRMATION_SCHEDULE_CHOICE",
            "ADMIN_CREATE_CONFIRMATION_SCHEDULE_TIME",
            "ADMIN_CREATE_CONFIRMATION_DEADLINE",
        }:
            return await self._creation_confirmation_schedule_step(user, message, value)
        if user.dialog_state == "ADMIN_CREATE_NAME":
            clean_name = value.strip()
            if not clean_name:
                return BotResponse(text="Название игры не может быть пустым. Введите название игры:")
            if len(clean_name) > 200:
                return BotResponse(text="Название игры не должно превышать 200 символов. Введите название игры:")
            user.dialog_state = "ADMIN_CREATE_DATE"
            user.dialog_context = {"event_name": clean_name}
            return _game_date_response()
        if user.dialog_state == "ADMIN_CREATE_DATE":
            if value == "admin:date:next-saturday":
                game_date = next_saturday_game_date()
            else:
                try:
                    game_date = parse_game_date(value)
                except ValueError:
                    return _game_date_response(invalid=True)
            user.dialog_state = "ADMIN_CREATE_CLOSE_TIME"
            user.dialog_context["game_date"] = game_date.isoformat()
            return _scheduled_close_response()
        if user.dialog_state == "ADMIN_CREATE_CLOSE_TIME":
            game_date = date.fromisoformat(str(user.dialog_context["game_date"]))
            if value == "create:close:cancel":
                scheduled_close_at = None
            elif value == "create:close:next-night":
                scheduled_close_at = next_night_03(game_date)
            else:
                try:
                    scheduled_close_at = parse_scheduled_action_time(value)
                except ValueError:
                    return _scheduled_close_response(invalid=True)
            if scheduled_close_at is not None and scheduled_close_at.date() <= game_date:
                return _scheduled_close_response(not_after_game=True)
            if scheduled_close_at is not None and scheduled_close_at <= datetime.now(MOSCOW_TIMEZONE):
                return _scheduled_close_response(past=True)
            if scheduled_close_at is None:
                user.dialog_context.pop("scheduled_close_at", None)
            else:
                user.dialog_context["scheduled_close_at"] = scheduled_close_at.isoformat()
            user.dialog_state = "ADMIN_CREATE_PLAYER_AMOUNT"
            return BotResponse(text=f"Введите количество игроков (от 1 до {MAX_PLAYER_AMOUNT}):")
        if user.dialog_state == "ADMIN_CREATE_PLAYER_AMOUNT":
            raw_amount = value.strip()
            if not raw_amount.isdecimal():
                return BotResponse(text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:")
            player_amount = int(raw_amount)
            if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
                return BotResponse(text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:")
            user.dialog_context["player_amount"] = player_amount
            if await self._is_admin(user):
                return self._creation_master_question(user)
            return self._creation_schedule_question(user, user)
        if user.dialog_state == "ADMIN_SCHEDULE_REGISTRATION_TIME":
            event_id = str(user.dialog_context["event_id"])
            event = await self.events.get(event_id)
            if event is None:
                await self._clear(user)
                return BotResponse(text="Игра не найдена.")
            try:
                opens_at = parse_scheduled_action_time(value)
            except ValueError:
                return self._registration_management_prompt(
                    event,
                    invalid=SCHEDULED_TIME_INVALID,
                )
            if opens_at <= datetime.now(MOSCOW_TIMEZONE):
                return self._registration_management_prompt(
                    event,
                    invalid="Открытие регистрации должно быть запланировано на будущее время.",
                )
            if event.confirmation_open_at is not None and opens_at >= event.confirmation_open_at:
                return self._registration_management_prompt(
                    event,
                    invalid="Регистрация должна открываться строго раньше подтверждения.",
                )
            if event.scheduled_close_at is not None and opens_at >= event.scheduled_close_at:
                return self._registration_management_prompt(
                    event,
                    invalid="Регистрация должна открываться строго до закрытия игры.",
                )
            if event.status is not EventStatus.CREATED:
                await self._clear(user)
                return self._scheduled_events_menu(
                    event,
                    notice="Регистрация или подтверждение уже открыты; это расписание изменить нельзя.\n\n",
                )
            await self.events.set_registration_open_at(event_id, opens_at)
            refreshed = await self.events.get(event_id)
            await self._clear(user)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.")
            return self._scheduled_events_menu(refreshed, notice="✅ Время открытия регистрации сохранено.\n\n")
        if user.dialog_state == "ADMIN_SCHEDULE_CONFIRMATION_TIME":
            event_id = str(user.dialog_context["event_id"])
            event = await self.events.get(event_id)
            if event is None:
                await self._clear(user)
                return BotResponse(text="Игра не найдена.")
            try:
                opens_at = parse_scheduled_action_time(value)
            except ValueError:
                return self._confirmation_management_prompt(
                    event,
                    invalid=SCHEDULED_TIME_INVALID,
                )
            if opens_at <= datetime.now(MOSCOW_TIMEZONE):
                return self._confirmation_management_prompt(
                    event,
                    invalid="Открытие подтверждения должно быть запланировано на будущее время.",
                )
            if event.registration_open_at is not None and opens_at <= event.registration_open_at:
                return self._confirmation_management_prompt(
                    event,
                    invalid="Подтверждение должно открываться строго после регистрации.",
                )
            if event.scheduled_close_at is not None and opens_at >= event.scheduled_close_at:
                return self._confirmation_management_prompt(
                    event,
                    invalid="Подтверждение должно открываться строго до закрытия игры.",
                )
            if event.status is not EventStatus.CREATED:
                await self._clear(user)
                return self._scheduled_events_menu(
                    event,
                    notice="Подтверждение уже открыто; расписание изменить нельзя.\n\n",
                )
            user.dialog_context["confirmation_open_at"] = opens_at.isoformat()
            user.dialog_state = "ADMIN_SCHEDULE_CONFIRMATION_DEADLINE"
            return _scheduled_confirmation_deadline_response(back=Button(label=BACK, value=f"ag:schedule:{event_id}"))
        if user.dialog_state == "ADMIN_SCHEDULE_CONFIRMATION_DEADLINE":
            event_id = str(user.dialog_context["event_id"])
            event = await self.events.get(event_id)
            if event is None:
                await self._clear(user)
                return BotResponse(text="Игра не найдена.")
            try:
                deadline = parse_confirmation_deadline(value)
            except ValueError:
                return _scheduled_confirmation_deadline_response(
                    back=Button(label=BACK, value=f"ag:schedule:{event_id}"),
                    invalid="Некорректная дата и время. Попробуйте ещё раз.",
                )
            opens_at = datetime.fromisoformat(str(user.dialog_context["confirmation_open_at"]))
            if deadline <= opens_at:
                return _scheduled_confirmation_deadline_response(
                    back=Button(label=BACK, value=f"ag:schedule:{event_id}"),
                    invalid="Дедлайн должен быть строго позже открытия подтверждения.",
                )
            if event.scheduled_close_at is not None and deadline >= event.scheduled_close_at:
                return _scheduled_confirmation_deadline_response(
                    back=Button(label=BACK, value=f"ag:schedule:{event_id}"),
                    invalid="Дедлайн подтверждения должен быть строго раньше закрытия игры.",
                )
            if event.status is not EventStatus.CREATED:
                await self._clear(user)
                return self._scheduled_events_menu(
                    event,
                    notice="Подтверждение уже открыто; расписание изменить нельзя.\n\n",
                )
            await self.events.set_confirmation_schedule(event_id, opens_at, deadline)
            refreshed = await self.events.get(event_id)
            await self._clear(user)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.")
            return self._scheduled_events_menu(
                refreshed,
                notice="✅ Время открытия подтверждения и дедлайн сохранены.\n\n",
            )
        if user.dialog_state == "ADMIN_SCHEDULE_CLOSE_TIME":
            event_id = str(user.dialog_context["event_id"])
            event = await self.events.get(event_id)
            if event is None:
                await self._clear(user)
                return BotResponse(text="Игра не найдена.")
            try:
                closes_at = parse_scheduled_action_time(value)
            except ValueError:
                return _scheduled_close_response(
                    invalid=True,
                    next_night_value=f"ag:schedule-close-next-night:{event_id}",
                    cancel_value=f"ag:schedule-clear-close:{event_id}",
                    back=Button(label=BACK, value=f"ag:schedule:{event_id}"),
                )
            error = self._scheduled_close_validation(event, closes_at)
            if error is not None:
                return _scheduled_close_response(
                    past=error == "past",
                    not_after_game=error == "game",
                    confirmation_conflict=error == "confirmation",
                    registration_conflict=error == "registration",
                    next_night_value=f"ag:schedule-close-next-night:{event_id}",
                    cancel_value=f"ag:schedule-clear-close:{event_id}",
                    back=Button(label=BACK, value=f"ag:schedule:{event_id}"),
                )
            if event.scheduled_close_operation_id is not None:
                await self._clear(user)
                return self._scheduled_events_menu(
                    event,
                    notice="Автоматическое закрытие уже началось; изменить его время нельзя.\n\n",
                )
            await self.events.set_scheduled_close_at(event_id, closes_at)
            refreshed = await self.events.get(event_id)
            await self._clear(user)
            if refreshed is None:
                return BotResponse(text="Игра не найдена.")
            return self._scheduled_events_menu(refreshed, notice="✅ Время закрытия сохранено.\n\n")
        if user.dialog_state == "ADMIN_UPDATE_GAME_NAME":
            clean_name = value.strip()
            event_id = str(user.dialog_context["event_id"])
            if not clean_name:
                return BotResponse(
                    text="Название игры не может быть пустым. Введите новое название:",
                    buttons=[edit_game_button(event_id)],
                )
            if len(clean_name) > 200:
                return BotResponse(
                    text="Название игры не должно превышать 200 символов. Введите новое название:",
                    buttons=[edit_game_button(event_id)],
                )
            user.dialog_state = "ADMIN_UPDATE_GAME_DATE"
            user.dialog_context["new_event_name"] = clean_name
            raw_date = user.dialog_context.get("game_date")
            current_date = date.fromisoformat(str(raw_date)) if raw_date is not None else None
            return _game_details_date_response(event_id, current_date)
        if user.dialog_state == "ADMIN_UPDATE_GAME_DATE":
            if value == "admin:details:next-saturday":
                game_date = next_saturday_game_date()
            else:
                try:
                    game_date = parse_game_date(value)
                except ValueError:
                    raw_date = user.dialog_context.get("game_date")
                    current_date = date.fromisoformat(str(raw_date)) if raw_date is not None else None
                    return _game_details_date_response(
                        str(user.dialog_context["event_id"]),
                        current_date,
                        invalid=True,
                    )
            context = user.dialog_context.copy()
            event_id = str(context["event_id"])
            new_name = str(context["new_event_name"])
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.UPDATE_EVENT_DETAILS,
                event_id=event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EventDetailsPayload(name=new_name, game_date=game_date),
                reply_context=ReplyContext(
                    text_success=(f"Название и дата игры изменены: {event_reference(new_name, game_date)}."),
                    buttons=[edit_game_button(event_id)],
                ),
                idempotency_key=f"{message.update_id}:{Operation.UPDATE_EVENT_DETAILS.value}",
            )
            return BotResponse(
                text="⏳ Изменение названия и даты принято в обработку.",
                buttons=[edit_game_button(event_id)],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state == "ADMIN_UPDATE_PLAYER_AMOUNT":
            raw_amount = value.strip()
            if not raw_amount.isdecimal():
                return BotResponse(
                    text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:",
                    buttons=[edit_game_button(str(user.dialog_context["event_id"]))],
                )
            player_amount = int(raw_amount)
            if not 1 <= player_amount <= MAX_PLAYER_AMOUNT:
                return BotResponse(
                    text=f"Введите целое количество игроков от 1 до {MAX_PLAYER_AMOUNT}:",
                    buttons=[edit_game_button(str(user.dialog_context["event_id"]))],
                )
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.UPDATE_PLAYER_AMOUNT,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=PlayerAmountPayload(player_amount=player_amount),
                reply_context=ReplyContext(
                    text_success=(
                        f"Количество игроков игры {_context_event_reference(context)} изменено: {player_amount}."
                    ),
                    buttons=[edit_game_button(str(context["event_id"]))],
                ),
                idempotency_key=f"{message.update_id}:{Operation.UPDATE_PLAYER_AMOUNT.value}",
            )
            return BotResponse(
                text="⏳ Изменение количества игроков принято в обработку.",
                buttons=[edit_game_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state == "ADMIN_POST_LINK_TELEGRAM":
            event_id = str(user.dialog_context["event_id"])
            if message.callback is not None:
                return BotResponse(
                    text="Отправьте ссылку на пост об игре в Telegram обычным текстовым сообщением:",
                    buttons=[edit_game_button(event_id)],
                )
            try:
                telegram_post_url = normalize_telegram_post_url(value)
            except ValueError as exc:
                return BotResponse(
                    text=f"❌ {exc}. Попробуйте ещё раз:",
                    buttons=[edit_game_button(event_id)],
                )
            user.dialog_context["telegram_post_url"] = telegram_post_url
            user.dialog_state = "ADMIN_POST_LINK_VK"
            return BotResponse(
                text="Теперь отправьте ссылку на пост об игре во VK:",
                buttons=[edit_game_button(event_id)],
            )
        if user.dialog_state == "ADMIN_POST_LINK_VK":
            event_id = str(user.dialog_context["event_id"])
            if message.callback is not None:
                return BotResponse(
                    text="Отправьте ссылку на пост об игре во VK обычным текстовым сообщением:",
                    buttons=[edit_game_button(event_id)],
                )
            try:
                vk_post_url = normalize_vk_post_url(value)
            except ValueError as exc:
                return BotResponse(
                    text=f"❌ {exc}. Попробуйте ещё раз:",
                    buttons=[edit_game_button(event_id)],
                )
            telegram_post_url = str(user.dialog_context["telegram_post_url"])
            await self.events.set_post_links(event_id, telegram_post_url, vk_post_url)
            event = await self.events.get(event_id)
            await self._clear(user)
            if event is None:
                return BotResponse(text="Игра не найдена.", buttons=[Button(label=BACK, value=MANAGE_GAMES)])
            if event.telegram_post_url != telegram_post_url or event.vk_post_url != vk_post_url:
                raise RuntimeError("game post links were not persisted")
            return BotResponse(
                text=(
                    f"✅ Ссылки на посты игры {event_reference(event.name, event.game_date)} сохранены.\n\n"
                    f"Telegram: {telegram_post_url}\n"
                    f"VK: {vk_post_url}"
                ),
                buttons=[edit_game_button(event_id)],
            )
        if user.dialog_state == "ADMIN_STATUS_SELECT":
            choice = ADMIN_STATUS_CHOICES.get(value)
            if choice is None:
                return _admin_status_response(
                    str(user.dialog_context["event_name"]),
                    EventStatus(str(user.dialog_context["event_status"])),
                    str(user.dialog_context["event_id"]),
                    (
                        date.fromisoformat(str(user.dialog_context["game_date"]))
                        if user.dialog_context.get("game_date") is not None
                        else None
                    ),
                )
            status_label, operation = choice
            if operation is Operation.OPEN_CONFIRMATION:
                user.dialog_state = "ADMIN_CONFIRMATION_DEADLINE"
                return _confirmation_deadline_response(str(user.dialog_context["event_id"]))
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=operation,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(
                    text_success=f"Статус игры {_context_event_reference(context)} изменён: {status_label}.",
                    buttons=[edit_game_button(str(context["event_id"]))],
                ),
                idempotency_key=f"{message.update_id}:{operation.value}",
            )
            return BotResponse(
                text="⏳ Изменение Статуса принято в обработку.",
                buttons=[edit_game_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state == "ADMIN_CONFIRMATION_DEADLINE":
            if value == "admin:deadline:nearest-thursday":
                deadline = closest_thursday_19()
            else:
                try:
                    deadline = parse_confirmation_deadline(value)
                except ValueError:
                    return _confirmation_deadline_response(str(user.dialog_context["event_id"]), invalid=True)
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.OPEN_CONFIRMATION,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=ConfirmationDeadlinePayload(deadline=deadline),
                reply_context=ReplyContext(
                    text_success=(f"Статус игры {_context_event_reference(context)} изменён: {STATUS_CONFIRMATION}."),
                    buttons=[edit_game_button(str(context["event_id"]))],
                ),
                idempotency_key=f"{message.update_id}:{Operation.OPEN_CONFIRMATION.value}",
            )
            return BotResponse(
                text=(
                    "⏳ Открытие подтверждения принято в обработку.\n\n"
                    f"Дедлайн: {format_confirmation_deadline(deadline)}"
                ),
                buttons=[edit_game_button(str(context["event_id"]))],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state in {
            "ADMIN_NOTIFICATION_LINK_TEXT",
            "ADMIN_NOTIFICATION_MESSAGE_TEXT",
            "ADMIN_NOTIFICATION_TEXT",
            "ADMIN_ALL_PLAYERS_NOTIFICATION_TEXT",
        }:
            event_name = str(user.dialog_context["event_name"])
            event_id = str(user.dialog_context["event_id"])
            raw_game_date = user.dialog_context.get("game_date")
            notification_game_date = date.fromisoformat(str(raw_game_date)) if raw_game_date is not None else None
            link = user.dialog_state == "ADMIN_NOTIFICATION_LINK_TEXT"
            all_players = user.dialog_state == "ADMIN_ALL_PLAYERS_NOTIFICATION_TEXT"
            if message.callback is not None:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=link,
                    all_players=all_players,
                    game_date=notification_game_date,
                    invalid="Отправьте содержимое обычным текстовым сообщением.",
                )
            if not value:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=link,
                    all_players=all_players,
                    game_date=notification_game_date,
                    invalid="Сообщение не может быть пустым.",
                )
            if link and not is_plain_chat_link(value):
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=True,
                    game_date=notification_game_date,
                    invalid="Пришлите только корректную ссылку-приглашение в чат Telegram или VK.",
                )
            if not link and not all_players and is_plain_chat_link(value):
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=False,
                    game_date=notification_game_date,
                    invalid=f"Для рассылки ссылки на чат используйте кнопку «{SEND_CONFIRMED_LINK}».",
                )
            final_text = (
                player_notification_text(event_name, value, notification_game_date)
                if all_players
                else confirmed_notification_text(event_name, value, notification_game_date)
            )
            if len(value) > 4000 or len(final_text) > 4000:
                return _confirmed_notification_prompt(
                    event_name,
                    event_id,
                    link=link,
                    all_players=all_players,
                    game_date=notification_game_date,
                    invalid="Итоговое сообщение не должно превышать 4000 символов.",
                )
            recipient_count = (
                await self.administration.active_registration_count(event_id)
                if all_players
                else await self.administration.confirmed_registration_count(
                    event_id,
                    main_roster_only=link,
                )
            )
            user.dialog_state = (
                "ADMIN_ALL_PLAYERS_NOTIFICATION_CONFIRM" if all_players else "ADMIN_NOTIFICATION_CONFIRM"
            )
            user.dialog_context["notification_text"] = value
            user.dialog_context["notification_recipient_count"] = recipient_count
            return _confirmed_notification_preview(
                event_name,
                event_id,
                value,
                recipient_count,
                notification_game_date,
                all_players=all_players,
            )
        if user.dialog_state in {"ADMIN_NOTIFICATION_CONFIRM", "ADMIN_ALL_PLAYERS_NOTIFICATION_CONFIRM"}:
            event_name = str(user.dialog_context["event_name"])
            event_id = str(user.dialog_context["event_id"])
            raw_game_date = user.dialog_context.get("game_date")
            notification_game_date = date.fromisoformat(str(raw_game_date)) if raw_game_date is not None else None
            notification_text = str(user.dialog_context["notification_text"])
            recipient_count = int(user.dialog_context["notification_recipient_count"])
            all_players = user.dialog_state == "ADMIN_ALL_PLAYERS_NOTIFICATION_CONFIRM"
            if value != "admin:notification:confirm":
                return _confirmed_notification_preview(
                    event_name,
                    event_id,
                    notification_text,
                    recipient_count,
                    notification_game_date,
                    all_players=all_players,
                )
            operation = (
                Operation.SEND_ALL_PLAYERS_NOTIFICATION if all_players else Operation.SEND_CONFIRMED_NOTIFICATION
            )
            await self._clear(user)
            await self.registrations.enqueue(
                operation=operation,
                event_id=event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=NotificationPayload(text=notification_text),
                reply_context=ReplyContext(buttons=[notification_management_button(event_id)]),
                idempotency_key=f"{message.update_id}:{operation.value}",
            )
            return BotResponse(
                text="⏳ Уведомление принято в обработку.",
                buttons=[notification_management_button(event_id)],
                deferred=True,
                command_enqueued=True,
            )
        if user.dialog_state in {"ADMIN_ARCHIVE_NAME", "ADMIN_DELETE_NAME"}:
            context = user.dialog_context.copy()
            expected = str(context["event_name"])
            if value.strip() != expected:
                back = (
                    game_management_button(str(user.dialog_context["event_id"]))
                    if user.dialog_state == "ADMIN_DELETE_NAME"
                    else edit_game_button(str(user.dialog_context["event_id"]))
                )
                return BotResponse(
                    text=f"Название не совпало. Введите точно:\n\n{expected}",
                    buttons=[back],
                )
            event_id = str(user.dialog_context["event_id"])
            delete_archived = (
                user.dialog_state == "ADMIN_DELETE_NAME" and event is not None and event.archived_at is not None
            )
            operation = Operation.DELETE_EVENT if delete_archived else Operation.ARCHIVE_EVENT
            await self._clear(user)
            await self.registrations.enqueue(
                operation=operation,
                event_id=event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(
                    text_success=(
                        f"🗑 Игра {_context_event_reference(context)}, все записи и XLSX-таблицы удалены."
                        if delete_archived
                        else (
                            f"📦 Игра {_context_event_reference(context)} архивирована. "
                            "Игра, записи и XLSX-таблицы сохранены в постоянном списке."
                        )
                    ),
                    buttons=[Button(label=BACK, value=ARCHIVED_GAMES if delete_archived else MANAGE_GAMES)],
                ),
                idempotency_key=f"{message.update_id}:{operation.value}",
            )
            return BotResponse(
                text="⏳ Удаление принято в обработку." if delete_archived else "⏳ Архивация принята в обработку.",
                buttons=[Button(label=BACK, value=ARCHIVED_GAMES if delete_archived else MANAGE_GAMES)],
                deferred=True,
                command_enqueued=True,
            )
        await self._clear(user)
        return BotResponse(text="Административный диалог сброшен.")
