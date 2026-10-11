from __future__ import annotations

import asyncio

from larp_bot.domain.models import (
    BotResponse,
    Button,
    Event,
    InboundMessage,
    Platform,
    PrivilegedRole,
    TelegramUser,
    User,
    event_reference,
    normalize_telegram_profile,
    normalize_vk_url,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    ASSIGNABLE_PRIVILEGED_ROLES,
    BACK,
    GAMEMASTER_NOTIFICATION,
    MANAGE_MODERATORS,
    PRIVILEGED_ROLE_LABELS,
    user_id,
)
from .identity import global_identities, global_users
from .navigation import admin_menu_button


class AdministrationRolesMixin(ConversationContext):
    """Moderator and event-master role mutations."""

    async def _grant_gamemaster(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._is_admin(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        target_platform = Platform(str(user.dialog_context["gamemaster_platform"]))
        role = PrivilegedRole(str(user.dialog_context.get("privileged_role", PrivilegedRole.GAMEMASTER.value)))
        if role not in ASSIGNABLE_PRIVILEGED_ROLES:
            await self._clear(user)
            return BotResponse(text="Некорректная роль.")
        target: User | None
        try:
            if target_platform is Platform.TELEGRAM:
                target = await self._find_telegram_target(value)
            else:
                normalized = normalize_vk_url(value)
                if self.vk_user_ids is None:
                    raise RuntimeError("VK user ID resolver is not configured")
                target_id = await self.vk_user_ids.resolve_user_id(normalized)
                target = None if target_id is None else await self.users.get(Platform.VK, target_id)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:", buttons=[Button(label=BACK, value=MANAGE_MODERATORS)]
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
            )
        if not target.profile_complete:
            return BotResponse(
                text="Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите.",
                buttons=[Button(label=BACK, value=MANAGE_MODERATORS)],
            )
        target_id = user_id(target)
        request_id = (
            f"gamemaster-grant:{target_platform.value}:{target_id}:{message.update_id}"
            if role is PrivilegedRole.GAMEMASTER
            else f"privileged-role-grant:{role.value}:{target_platform.value}:{target_id}:{message.update_id}"
        )
        target_profiles = await global_users(self.users, target)
        current_role = await self._privileged_role(target)
        if current_role is PrivilegedRole.ADMIN:
            await self._clear(user)
            return BotResponse(text="Администратору нельзя назначить другую роль.")
        same_operation = target.effective_privileged_role is role and target.gamemaster_grant_operation_id == request_id
        if current_role is role and not same_operation:
            await self._clear(user)
            return BotResponse(text=f"У этого пользователя уже роль «{PRIVILEGED_ROLE_LABELS[role]}».")

        profiles_to_update = [profile for profile in target_profiles if profile.effective_privileged_role is not role]
        if not same_operation or profiles_to_update:
            ordered_profiles = [target, *profiles_to_update]
            seen: set[tuple[Platform, int]] = set()
            for profile in ordered_profiles:
                identity = self._identity(profile)
                identity_key = (identity.platform, identity.platform_user_id)
                if identity_key in seen or (same_operation and profile.effective_privileged_role is role):
                    continue
                seen.add(identity_key)
                await self.users.set_privileged_role(
                    identity.platform,
                    identity.platform_user_id,
                    role,
                    request_id,
                )
            refreshed = await self.users.get(target_platform, target_id)
            if refreshed is None or refreshed.effective_privileged_role is not role:
                raise RuntimeError("privileged role grant was not persisted")
            target = refreshed
        if target.gamemaster_grant_operation_id != request_id:
            await self._clear(user)
            return BotResponse(text=f"У этого пользователя уже роль «{PRIVILEGED_ROLE_LABELS[role]}».")
        if target.last_delivery_operation_id != request_id:
            await self._notify_status_change(
                user,
                target,
                request_id=request_id,
                text=(
                    GAMEMASTER_NOTIFICATION
                    if role is PrivilegedRole.GAMEMASTER
                    else f"Вам назначена роль «{PRIVILEGED_ROLE_LABELS[role]}»! 🎉"
                ),
                buttons=[admin_menu_button()],
            )

        if message.identity.platform is target_platform and message.identity.platform_user_id == target_id:
            user.privileged_role = role
            user.is_gamemaster = True
            user.gamemaster_grant_operation_id = request_id
            user.last_delivery_operation_id = request_id
        await self._clear(user)
        platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
        if role is PrivilegedRole.GAMEMASTER:
            return BotResponse(text=f"✅ Пользователь назначен модератором в {platform_name}.")
        return BotResponse(text=f"✅ Пользователю назначена роль «{PRIVILEGED_ROLE_LABELS[role]}» в {platform_name}.")

    async def _find_telegram_target(self, value: str) -> TelegramUser | None:
        handle = normalize_telegram_profile(value)
        target = await self.users.find_telegram_by_handle(handle)
        if target is not None:
            return target
        target = await self.administration.find_telegram_user_by_profile(handle)
        if target is not None:
            await self.users.update_telegram_handle(target.tg_id, handle)
            target.telegram_handle = handle
        return target

    async def _promote_master_target(
        self,
        user: User,
        message: InboundMessage,
        target: User,
    ) -> User:
        identity = self._identity(target)
        request_id = f"gamemaster-grant:{identity.platform.value}:{identity.platform_user_id}:{message.update_id}"
        if user.dialog_context.get("completed_master_promotion") == request_id:
            refreshed = await self.users.get(identity.platform, identity.platform_user_id)
            return refreshed or target

        same_operation = (
            target.effective_privileged_role is PrivilegedRole.GAMEMASTER
            and target.gamemaster_grant_operation_id == request_id
        )
        if not same_operation and await self._has_admin_access(target):
            user.dialog_context["completed_master_promotion"] = request_id
            await self.users.save(user)
            return target

        if target.effective_privileged_role is None:
            await self.users.set_privileged_role(
                identity.platform,
                identity.platform_user_id,
                PrivilegedRole.GAMEMASTER,
                request_id,
            )
        refreshed = await self.users.get(identity.platform, identity.platform_user_id)
        if refreshed is None or refreshed.effective_privileged_role is None:
            raise RuntimeError("gamemaster grant was not persisted")
        target = refreshed
        if target.gamemaster_grant_operation_id == request_id:
            if target.last_delivery_operation_id != request_id:
                await self._notify_status_change(
                    user,
                    target,
                    request_id=request_id,
                    text=GAMEMASTER_NOTIFICATION,
                    buttons=[admin_menu_button()],
                )
        elif not await self._has_admin_access(target):
            raise RuntimeError("gamemaster grant was not persisted")

        user.dialog_context["completed_master_promotion"] = request_id
        await self.users.save(user)
        return target

    @staticmethod
    def _event_leader_platform_prompt(event_id: str) -> BotResponse:
        return BotResponse(
            text="Выберите бот, которым пользуется будущий мастер:",
            buttons=[
                Button(label="Telegram", value="admin:event-leader:telegram"),
                Button(label="VK", value="admin:event-leader:vk"),
                Button(label=BACK, value=f"ag:masters:{event_id}"),
            ],
        )

    async def _add_event_leader(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        target_platform = Platform(str(user.dialog_context["gamemaster_platform"]))
        try:
            target = await self._find_master_profile(target_platform, value)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:",
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )
        if not target.profile_complete:
            return BotResponse(
                text="Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите.",
                buttons=[Button(label=BACK, value=f"ag:masters:{user.dialog_context['event_id']}")],
            )

        event_id = str(user.dialog_context["event_id"])
        event = await self.events.get(event_id)
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.")
        return await self._assign_event_leader(user, message, event, target)

    async def _assign_event_leader(
        self,
        user: User,
        message: InboundMessage,
        event: Event,
        target: User,
    ) -> BotResponse:
        identity = self._identity(target)
        event_id = event.event_id
        request_id = (
            f"leader-grant:{event_id}:{identity.platform.value}:{identity.platform_user_id}:{message.update_id}"
        )
        target_role, has_admin_access = await asyncio.gather(
            self._privileged_role(target),
            self._has_admin_access(target),
        )
        if not has_admin_access:
            return self._master_promotion_question(
                user,
                target,
                state="ADMIN_EVENT_LEADER_PROMOTE_CONFIRM",
                reference=event_reference(event.name, event.game_date),
                confirm_value="master:promote:yes",
                decline_value="master:promote:no",
            )
        assert target_role is not None
        role_name = PRIVILEGED_ROLE_LABELS[target_role]
        reference = event_reference(event.name, event.game_date)
        notification_button = Button(label="Управление игрой", value=f"ag:manage:{event_id}")
        target_identities, leaders = await asyncio.gather(
            global_identities(self.users, target),
            self.events.list_leaders(event_id),
        )
        if any(target_identity in leaders for target_identity in target_identities):
            if (
                user.dialog_context.get("pending_leader_grant") == request_id
                and target.last_delivery_operation_id != request_id
            ):
                await self.administration.refresh_pass_table(event_id)
                await self._notify_status_change(
                    user,
                    target,
                    request_id=request_id,
                    text=f"Вы добавлены мастером игры {reference}.",
                    buttons=[notification_button],
                )
                await self._clear(user)
                return BotResponse(
                    text=f"✅ {role_name} добавлен мастером игры {reference}.",
                    buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
                )
            await self._clear(user)
            return BotResponse(
                text=f"Этот пользователь уже является мастером игры {reference}.",
                buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
            )

        user.dialog_context["pending_leader_grant"] = request_id
        await self.users.save(user)
        await self.events.add_leader(event_id, identity)
        if identity not in await self.events.list_leaders(event_id):
            raise RuntimeError("event leader grant was not persisted")
        await self.administration.refresh_pass_table(event_id)
        await self._notify_status_change(
            user,
            target,
            request_id=request_id,
            text=f"Вы добавлены мастером игры {reference}.",
            buttons=[notification_button],
        )
        await self._clear(user)
        return BotResponse(
            text=f"✅ {role_name} добавлен мастером игры {reference}.",
            buttons=[Button(label=BACK, value=f"ag:masters:{event_id}")],
        )
