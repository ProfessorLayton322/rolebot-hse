from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date

from larp_bot.domain.models import (
    BotIdentity,
    BotResponse,
    Button,
    Event,
    InboundMessage,
    Platform,
    PrivilegedRole,
    TelegramUser,
    User,
    VkUser,
    event_reference,
    format_game_date,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    ADD_EVENT_LEADER,
    ADD_SELF_EVENT_LEADER,
    BACK,
    DAILY_PASSES_ACTION,
    DAILY_PASSES_DATE_PREFIX,
    DAILY_PASSES_PAGE_PREFIX,
    GET_DAILY_PASSES,
    GRANT_GAMEMASTER,
    GRANT_GAMEMASTER_VIEWER,
    GRANT_PASS_MANAGER,
    LIST_EVENT_LEADERS,
    LIST_GAMEMASTERS,
    MANAGE_GAMES,
    MANAGE_MODERATORS,
    MANAGE_STATISTICS,
    PAGE_BACK,
    PAGE_FORWARD,
    PAGE_SIZE,
    PRIVILEGED_ROLE_LABELS,
    REMOVE_EVENT_LEADER,
    REVOKE_GAMEMASTER,
    _pagination_buttons,
    game_management_button,
    user_id,
)
from .identity import global_identities, global_users, local_identity
from .navigation import ADMIN_MENU, admin_menu_button
from .services import OperationNotAllowed, event_master_records


class AdministrationAccessMixin(ConversationContext):
    """Administration access checks, navigation, passes, and master discovery."""

    async def _admin_menu(self, user: User) -> BotResponse:
        role = await self._privileged_role(user)
        if role is None or not role.includes(PrivilegedRole.GAMEMASTER):
            return BotResponse(text="Недостаточно прав.")
        labels = [
            "➕ Создать игру",
            MANAGE_GAMES,
            BACK,
        ]
        if role in {PrivilegedRole.PASS_MANAGER, PrivilegedRole.ADMIN}:
            labels.insert(-1, GET_DAILY_PASSES)
        if await self._is_admin(user):
            labels.insert(-1, MANAGE_MODERATORS)
            labels.insert(-1, MANAGE_STATISTICS)
        buttons = [
            Button(label=label, value=DAILY_PASSES_ACTION if label == GET_DAILY_PASSES else label) for label in labels
        ]
        return BotResponse(text="🛠 Администрирование", buttons=buttons)

    async def _can_manage_daily_passes(self, user: User) -> bool:
        return await self._privileged_role(user) in {PrivilegedRole.PASS_MANAGER, PrivilegedRole.ADMIN}

    async def _daily_pass_table(self, user: User, message: InboundMessage, game_date: date) -> BotResponse:
        if not await self._can_manage_daily_passes(user):
            return BotResponse(text="Недостаточно прав.")
        requester = self._identity(user)
        pending_buttons = [admin_menu_button()]
        before_invoke = self._upload_pending_callback(
            user,
            message,
            text="⏳ Таблица пропусков создаётся. Бот пришлёт ссылку, когда загрузка завершится.",
            buttons=pending_buttons,
            operation=f"daily-passes:{game_date.isoformat()}",
        )
        try:
            export = await self.administration.create_daily_pass_table(
                game_date,
                requester=requester,
                reply_user_id=message.chat_id or message.peer_id or requester.platform_user_id,
                before_invoke=before_invoke,
            )
        except OperationNotAllowed:
            return BotResponse(
                text="На выбранную дату больше нет активных неархивированных игр.",
                buttons=pending_buttons,
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
                text="⏳ Таблица пропусков создаётся. Бот пришлёт ссылку, когда загрузка завершится.",
                buttons=pending_buttons,
                deferred=True,
            )
        return BotResponse(
            text=(
                f"Таблица пропусков на {format_game_date(game_date)}\n"
                f"Уникальных пользователей: {export.row_count}.\n\n{export.public_url}"
            ),
            buttons=pending_buttons,
        )

    async def _daily_passes_start(self, user: User, message: InboundMessage, *, page: int = 0) -> BotResponse:
        if not await self._can_manage_daily_passes(user):
            return BotResponse(text="Недостаточно прав.")
        game_dates = await self.administration.active_game_dates()
        if not game_dates:
            return BotResponse(
                text="Сейчас нет активных неархивированных игр с указанной датой.",
                buttons=[admin_menu_button()],
            )
        if len(game_dates) == 1:
            return await self._daily_pass_table(user, message, game_dates[0])
        page_count = (len(game_dates) + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(max(page, 0), page_count - 1)
        visible_dates = game_dates[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        navigation = []
        if page > 0:
            navigation.append(Button(label=PAGE_BACK, value=f"{DAILY_PASSES_PAGE_PREFIX}{page - 1}"))
        if page + 1 < page_count:
            navigation.append(Button(label=PAGE_FORWARD, value=f"{DAILY_PASSES_PAGE_PREFIX}{page + 1}"))
        return BotResponse(
            text="Выберите день, за который нужно получить все пропуска:",
            buttons=[
                *(
                    Button(
                        label=format_game_date(game_date),
                        value=f"{DAILY_PASSES_DATE_PREFIX}{game_date.isoformat()}",
                    )
                    for game_date in visible_dates
                ),
                *navigation,
                admin_menu_button(),
            ],
        )

    @staticmethod
    def _identity(user: User) -> BotIdentity:
        return local_identity(user)

    @staticmethod
    def _platform_code(platform: Platform) -> str:
        return "t" if platform is Platform.TELEGRAM else "v"

    @staticmethod
    def _platform_from_code(value: str) -> Platform:
        if value == "t":
            return Platform.TELEGRAM
        if value == "v":
            return Platform.VK
        raise ValueError("unknown platform")

    @classmethod
    def _person_label(cls, identity: BotIdentity, profile: User | None) -> str:
        platform_name = "TG" if identity.platform is Platform.TELEGRAM else "VK"
        name = profile.full_name if profile is not None and profile.full_name else f"ID {identity.platform_user_id}"
        return f"{name} · {platform_name}"

    @staticmethod
    def _profile_text(title: str, identity: BotIdentity, profile: User | None) -> str:
        name = profile.full_name if profile is not None and profile.full_name else "не указано"
        lines = [title, "", f"Имя: {name}"]
        if isinstance(profile, TelegramUser):
            telegram = (
                f"https://t.me/{profile.telegram_handle.removeprefix('@')}"
                if profile.telegram_handle is not None
                else "не указан"
            )
            lines.extend((f"Telegram: {telegram}", f"VK: {profile.vk_url or 'не указан'}"))
        elif isinstance(profile, VkUser):
            telegram = (
                f"https://t.me/{profile.telegram_handle.removeprefix('@')}"
                if profile.telegram_handle is not None
                else "не указан"
            )
            lines.extend((f"Telegram: {telegram}", f"VK: https://vk.com/id{profile.vk_id}"))
        elif identity.platform is Platform.VK:
            lines.extend(("Telegram: не указан", f"VK: https://vk.com/id{identity.platform_user_id}"))
        else:
            lines.extend((f"Telegram ID: {identity.platform_user_id}", "VK: не указан"))
        return "\n".join(lines)

    async def _moderator_users(self, *, removable: bool = False) -> list[User]:
        users, configured_moderators, configured_admins = await asyncio.gather(
            self.users.list_all(),
            self.admins.list_gamemasters(),
            self.admins.list_admins(),
        )
        configured_moderator_keys = {
            (identity.platform, identity.platform_user_id) for identity in configured_moderators
        }
        configured_admin_keys = {(identity.platform, identity.platform_user_id) for identity in configured_admins}
        matches = []
        for profile in users:
            identity = self._identity(profile)
            identity_key = (identity.platform, identity.platform_user_id)
            configured_moderator = identity_key in configured_moderator_keys
            configured_admin = identity_key in configured_admin_keys
            if removable:
                has_removable_role = profile.privileged_role is not None or (
                    profile.is_gamemaster and not configured_moderator
                )
                if has_removable_role and not configured_admin:
                    matches.append(profile)
            elif profile.effective_privileged_role is not None or configured_moderator or configured_admin:
                matches.append(profile)
        return sorted(
            matches,
            key=lambda profile: (
                (profile.full_name or "").casefold(),
                self._identity(profile).platform.value,
                user_id(profile),
            ),
        )

    async def _event_master_records(self, event_id: str) -> list[tuple[BotIdentity, User | None]]:
        return await event_master_records(self.events, self.users, event_id)

    async def _notify_status_change(
        self,
        actor: User,
        target: User,
        *,
        request_id: str,
        text: str,
        buttons: list[Button] | None = None,
    ) -> None:
        if self.transport is None:
            raise RuntimeError("deferred transport is not configured")
        buttons = buttons or []
        identity = self._identity(target)
        await self.transport.send(
            platform=identity.platform,
            user_id=identity.platform_user_id,
            request_id=request_id,
            text=text,
            buttons=buttons,
        )
        await self.users.claim_delivery(
            identity.platform,
            identity.platform_user_id,
            request_id,
            buttons,
        )
        if self._identity(actor) == identity:
            actor.last_delivery_operation_id = request_id
            actor.last_bot_buttons = buttons.copy()

    def _upload_pending_callback(
        self,
        user: User,
        message: InboundMessage,
        *,
        text: str,
        buttons: list[Button],
        operation: str,
    ) -> Callable[[], Awaitable[None]] | None:
        if not self.administration.deferred_uploads:
            return None
        identity = self._identity(user)
        request_id = (
            f"upload-pending:{operation}:{identity.platform.value}:{identity.platform_user_id}:{message.update_id}"
        )

        async def send() -> None:
            if user.last_delivery_operation_id == request_id:
                return
            if self.transport is None:
                raise RuntimeError("deferred transport is not configured")
            await self.transport.send(
                platform=identity.platform,
                user_id=message.chat_id or message.peer_id or identity.platform_user_id,
                request_id=request_id,
                text=text,
                buttons=buttons,
            )
            await self.users.claim_delivery(
                identity.platform,
                identity.platform_user_id,
                request_id,
                buttons,
            )
            user.last_delivery_operation_id = request_id
            user.last_bot_buttons = buttons.copy()

        return send

    async def _is_event_leader(self, user: User, event_id: str) -> bool:
        identities, leaders = await asyncio.gather(
            global_identities(self.users, user),
            self.events.list_leaders(event_id),
        )
        return any(identity in leaders for identity in identities)

    async def _event_leader_required(self, user: User, event_id: str) -> BotResponse | None:
        if await self._is_admin(user) or await self._is_event_leader(user, event_id):
            return None
        await self._clear(user)
        return BotResponse(text="Только мастера этой игры могут изменять её данные.")

    async def _can_view_event_details(self, user: User, event_id: str) -> bool:
        is_admin, is_leader, can_view_unassigned = await asyncio.gather(
            self._is_admin(user),
            self._is_event_leader(user, event_id),
            self._can_view_unassigned_games(user),
        )
        return is_admin or is_leader or can_view_unassigned

    async def _is_admin(self, user: User) -> bool:
        identities = await global_identities(self.users, user)
        configured = await asyncio.gather(
            *(self.admins.is_admin(identity.platform, identity.platform_user_id) for identity in identities)
        )
        return any(configured)

    async def _privileged_role(self, user: User) -> PrivilegedRole | None:
        profiles = await global_users(self.users, user)
        identities = [self._identity(profile) for profile in profiles]
        configured_admins, configured_moderators = await asyncio.gather(
            asyncio.gather(
                *(self.admins.is_admin(identity.platform, identity.platform_user_id) for identity in identities)
            ),
            asyncio.gather(
                *(self.admins.is_gamemaster(identity.platform, identity.platform_user_id) for identity in identities)
            ),
        )
        if any(configured_admins):
            return PrivilegedRole.ADMIN
        roles = [
            *(profile.effective_privileged_role for profile in profiles),
            *(PrivilegedRole.GAMEMASTER for configured in configured_moderators if configured),
        ]
        return max((role for role in roles if role is not None), key=lambda role: role.level, default=None)

    async def _user_role_label(self, user: User) -> str:
        role = await self._privileged_role(user)
        return "Игрок" if role is None else PRIVILEGED_ROLE_LABELS[role]

    async def _has_admin_access(self, user: User) -> bool:
        role = await self._privileged_role(user)
        return role is not None and role.includes(PrivilegedRole.GAMEMASTER)

    async def _can_view_unassigned_games(self, user: User) -> bool:
        role = await self._privileged_role(user)
        return role is not None and role.includes(PrivilegedRole.GAMEMASTER_VIEWER)

    async def _moderator_management_menu(self, user: User) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        await self._clear(user)
        return BotResponse(
            text="Управление ролями",
            buttons=[
                Button(label=GRANT_GAMEMASTER, value=GRANT_GAMEMASTER),
                Button(label=GRANT_GAMEMASTER_VIEWER, value=GRANT_GAMEMASTER_VIEWER),
                Button(label=GRANT_PASS_MANAGER, value=GRANT_PASS_MANAGER),
                Button(label=REVOKE_GAMEMASTER, value=REVOKE_GAMEMASTER),
                Button(label=LIST_GAMEMASTERS, value=LIST_GAMEMASTERS),
                Button(label=BACK, value=ADMIN_MENU),
            ],
        )

    async def _show_moderators(self, user: User, mode: str, *, offset: int = 0) -> BotResponse:
        if not await self._is_admin(user):
            return BotResponse(text="Недостаточно прав.")
        if offset < 0:
            return BotResponse(text="Некорректная страница.")
        profiles = await self._moderator_users(removable=mode == "remove")
        if offset >= len(profiles) and offset:
            offset = max(0, ((len(profiles) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = profiles[offset : offset + PAGE_SIZE]
        roles = await asyncio.gather(*(self._privileged_role(profile) for profile in page))
        user.dialog_state = "ADMIN_MODERATOR_REMOVE" if mode == "remove" else "ADMIN_MODERATOR_LIST"
        user.dialog_context = {"moderator_offset": offset}
        if mode == "remove":
            text = "Выберите пользователя, с которого нужно снять назначенную роль:"
            empty = "Нет назначенных в боте ролей, которые можно снять."
            action = "remove-pick"
        else:
            text = "Привилегированные пользователи:"
            empty = "Привилегированных пользователей пока нет."
            action = "view"
        buttons = [
            Button(
                label=(f"{self._person_label(self._identity(profile), profile)} · {PRIVILEGED_ROLE_LABELS[role]}"),
                value=(f"mod:{action}:{self._platform_code(self._identity(profile).platform)}:{user_id(profile)}"),
            )
            for profile, role in zip(page, roles, strict=True)
            if role is not None
        ]
        buttons.extend(
            _pagination_buttons(
                previous=f"mod:{mode}-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"mod:{mode}-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(profiles) else None,
                cancel=MANAGE_MODERATORS,
            )
        )
        return BotResponse(text=text if page else empty, buttons=buttons)

    async def _event_masters_menu(self, user: User, event: Event) -> BotResponse:
        can_manage = await self._event_leader_required(user, event.event_id) is None
        if not can_manage and not await self._can_view_event_details(user, event.event_id):
            return BotResponse(text="Недостаточно прав.")
        await self._clear(user)
        buttons = [Button(label=LIST_EVENT_LEADERS, value=f"master:list:{event.event_id}")]
        if can_manage:
            buttons[:0] = [
                Button(label=ADD_EVENT_LEADER, value=f"master:add:{event.event_id}"),
                Button(label=REMOVE_EVENT_LEADER, value=f"master:remove:{event.event_id}"),
            ]
        buttons.append(game_management_button(event.event_id))
        if can_manage and await self._is_admin(user) and not await self._is_event_leader(user, event.event_id):
            buttons.insert(0, Button(label=ADD_SELF_EVENT_LEADER, value=f"master:self-add:{event.event_id}"))
        return BotResponse(
            text=f"Мастера игры {event_reference(event.name, event.game_date)}",
            buttons=buttons,
        )

    async def _master_candidates(self) -> list[User]:
        moderators, admins = await asyncio.gather(self._moderator_users(), self.admins.list_admins())
        admin_profiles = await asyncio.gather(
            *(self.users.get(identity.platform, identity.platform_user_id) for identity in admins)
        )
        eligible = {(self._identity(profile).platform, user_id(profile)): profile for profile in moderators}
        eligible.update(
            {
                (identity.platform, identity.platform_user_id): profile
                for identity, profile in zip(admins, admin_profiles, strict=True)
                if profile is not None
            }
        )
        return sorted(
            eligible.values(),
            key=lambda profile: (
                (profile.full_name or "").casefold(),
                self._identity(profile).platform.value,
                user_id(profile),
            ),
        )

    async def _show_event_master_candidates(
        self,
        user: User,
        event: Event,
        *,
        offset: int = 0,
        notice: str = "",
    ) -> BotResponse:
        if offset < 0:
            return BotResponse(text="Некорректная страница.")
        eligible, current = await asyncio.gather(
            self._master_candidates(),
            self.events.list_leaders(event.event_id),
        )
        candidates = [profile for profile in eligible if self._identity(profile) not in current]
        if offset >= len(candidates) and offset:
            offset = max(0, ((len(candidates) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = candidates[offset : offset + PAGE_SIZE]
        user.dialog_state = "ADMIN_EVENT_LEADER_ADD_PICK"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "master_offset": offset,
        }
        buttons = [
            Button(
                label=self._person_label(self._identity(profile), profile),
                value=(f"master:add-pick:{self._platform_code(self._identity(profile).platform)}:{user_id(profile)}"),
            )
            for profile in page
        ]
        buttons.append(Button(label="🔗 Ввести профиль Telegram или VK", value="master:add-direct"))
        buttons.extend(
            _pagination_buttons(
                previous=f"master:add-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"master:add-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(candidates) else None,
                cancel=f"ag:masters:{event.event_id}",
            )
        )
        text = (
            "Выберите привилегированного пользователя, которого нужно добавить мастером игры "
            f"{event_reference(event.name, event.game_date)}:"
            if page
            else (
                "Нет привилегированных пользователей, которых можно выбрать. "
                "Можно ввести Telegram- или VK-профиль напрямую."
            )
        )
        return BotResponse(text=f"{notice}{text}", buttons=buttons)
