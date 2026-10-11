from __future__ import annotations

import asyncio
import re
from datetime import date, datetime

from larp_bot.domain.models import (
    BotIdentity,
    BotResponse,
    Button,
    Event,
    InboundMessage,
    Platform,
    User,
    event_reference,
    format_game_date,
    normalize_vk_url,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    BACK,
    CREATE_AS_MASTER,
    CREATE_WITH_OTHER_MASTER,
    CREATE_WITHOUT_MASTER,
    CREATION_MASTER_QUESTION,
    MANAGE_GAMES,
    PAGE_SIZE,
    SCHEDULE_CONFIRMATION_QUESTION,
    SCHEDULE_REGISTRATION_QUESTION,
    SCHEDULED_TIME_INVALID,
    STATUS_REGISTRATION,
    _pagination_buttons,
    _registration_schedule_response,
    _scheduled_confirmation_deadline_response,
    _scheduled_confirmation_time_response,
    event_table_links,
    game_management_button,
    user_id,
)
from .deadlines import (
    MOSCOW_TIMEZONE,
    closest_friday_18,
    format_confirmation_deadline,
    parse_confirmation_deadline,
    parse_scheduled_action_time,
    tuesday_before_game_18,
)


class AdministrationCreationMixin(ConversationContext):
    """Game creation and initial master assignment flows."""

    @staticmethod
    def _creation_master_question(user: User) -> BotResponse:
        user.dialog_state = "ADMIN_CREATE_MASTER_CHOICE"
        return BotResponse(
            text=CREATION_MASTER_QUESTION,
            buttons=[
                Button(label=CREATE_AS_MASTER, value="create:master-self"),
                Button(label=CREATE_WITH_OTHER_MASTER, value="create:master-pick"),
                Button(label=CREATE_WITHOUT_MASTER, value="create:master-later"),
            ],
        )

    def _creation_schedule_question(
        self,
        user: User,
        master: User | None = None,
        *,
        assign_master_later: bool = False,
    ) -> BotResponse:
        if master is not None:
            identity = self._identity(master)
            user.dialog_context["master_platform"] = identity.platform.value
            user.dialog_context["master_user_id"] = identity.platform_user_id
            user.dialog_context.pop("assign_master_later", None)
        elif assign_master_later:
            user.dialog_context.pop("master_platform", None)
            user.dialog_context.pop("master_user_id", None)
            user.dialog_context["assign_master_later"] = True
        user.dialog_state = "ADMIN_CREATE_SCHEDULE_CHOICE"
        return BotResponse(
            text=SCHEDULE_REGISTRATION_QUESTION,
            buttons=[
                Button(label="Да", value="create:schedule:yes"),
                Button(label="Нет", value="create:schedule:no"),
            ],
        )

    @staticmethod
    def _creation_confirmation_schedule_question(user: User) -> BotResponse:
        user.dialog_state = "ADMIN_CREATE_CONFIRMATION_SCHEDULE_CHOICE"
        return BotResponse(
            text=SCHEDULE_CONFIRMATION_QUESTION,
            buttons=[
                Button(label="Да", value="create:confirmation:yes"),
                Button(
                    label="Не хочу планировать открытие подтверждения",
                    value="create:confirmation:no",
                ),
            ],
        )

    async def _show_creation_master_candidates(self, user: User, *, offset: int = 0, notice: str = "") -> BotResponse:
        creator = self._identity(user)
        candidates = [profile for profile in await self._master_candidates() if self._identity(profile) != creator]
        if offset >= len(candidates) and offset:
            offset = max(0, ((len(candidates) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = candidates[offset : offset + PAGE_SIZE]
        user.dialog_state = "ADMIN_CREATE_MASTER_PICK"
        user.dialog_context["master_offset"] = offset
        buttons = [
            Button(
                label=self._person_label(self._identity(profile), profile),
                value=f"create:master:{self._platform_code(self._identity(profile).platform)}:{user_id(profile)}",
            )
            for profile in page
        ]
        buttons.append(Button(label="🔗 Ввести профиль Telegram или VK", value="create:master-direct"))
        buttons.extend(
            _pagination_buttons(
                previous=f"create:master-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"create:master-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(candidates) else None,
                cancel="create:master-choice",
            )
        )
        reference = event_reference(
            str(user.dialog_context["event_name"]),
            date.fromisoformat(str(user.dialog_context["game_date"])),
        )
        text = (
            f"Выберите привилегированного пользователя, который станет первым мастером игры {reference}:"
            if page
            else (
                "Нет других привилегированных пользователей, которых можно выбрать мастером. "
                "Можно ввести Telegram- или VK-профиль напрямую."
            )
        )
        return BotResponse(text=f"{notice}{text}", buttons=buttons)

    @staticmethod
    def _creation_master_platform_prompt() -> BotResponse:
        return BotResponse(
            text="Выберите бот, которым пользуется будущий мастер:",
            buttons=[
                Button(label="Telegram", value="create:master-platform:telegram"),
                Button(label="VK", value="create:master-platform:vk"),
                Button(label=BACK, value="create:master-pick"),
            ],
        )

    def _master_promotion_question(
        self,
        user: User,
        target: User,
        *,
        state: str,
        reference: str,
        confirm_value: str,
        decline_value: str,
    ) -> BotResponse:
        identity = self._identity(target)
        user.dialog_state = state
        user.dialog_context["promotion_target_platform"] = identity.platform.value
        user.dialog_context["promotion_target_user_id"] = identity.platform_user_id
        name = target.full_name or f"ID {identity.platform_user_id}"
        return BotResponse(
            text=(
                f"«{name}» пока не является модератором. Назначить этого пользователя "
                f"модератором и добавить мастером игры {reference}?"
            ),
            buttons=[
                Button(label="Да, назначить", value=confirm_value),
                Button(label="Нет, выбрать другого", value=decline_value),
            ],
        )

    async def _find_master_profile(self, platform: Platform, value: str) -> User | None:
        if platform is Platform.TELEGRAM:
            return await self._find_telegram_target(value)
        normalized = normalize_vk_url(value)
        if self.vk_user_ids is None:
            raise RuntimeError("VK user ID resolver is not configured")
        target_id = await self.vk_user_ids.resolve_user_id(normalized)
        return None if target_id is None else await self.users.get(Platform.VK, target_id)

    async def _select_creation_master_profile(self, user: User, value: str) -> BotResponse:
        target_platform = Platform(str(user.dialog_context["master_profile_platform"]))
        try:
            target = await self._find_master_profile(target_platform, value)
        except ValueError as exc:
            return BotResponse(
                text=f"❌ {exc}. Попробуйте ещё раз:",
                buttons=[Button(label=BACK, value="create:master-pick")],
            )
        if target is None:
            platform_name = "Telegram" if target_platform is Platform.TELEGRAM else "VK"
            return BotResponse(
                text=(
                    f"Пользователь {platform_name} не найден. Убедитесь, что он уже написал этому боту "
                    "и полностью заполнил профиль, затем попробуйте ещё раз."
                ),
                buttons=[Button(label=BACK, value="create:master-pick")],
            )
        if not target.profile_complete:
            return BotResponse(
                text=("Профиль этого пользователя заполнен не полностью. Попросите его завершить профиль и повторите."),
                buttons=[Button(label=BACK, value="create:master-pick")],
            )
        if await self._has_admin_access(target):
            return self._creation_schedule_question(user, target)
        reference = event_reference(
            str(user.dialog_context["event_name"]),
            date.fromisoformat(str(user.dialog_context["game_date"])),
        )
        return self._master_promotion_question(
            user,
            target,
            state="ADMIN_CREATE_MASTER_PROMOTE_CONFIRM",
            reference=reference,
            confirm_value="create:master-promote:yes",
            decline_value="create:master-promote:no",
        )

    async def _creation_master_promotion_step(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse:
        if value == "create:master-promote:no":
            return await self._show_creation_master_candidates(
                user,
                offset=int(user.dialog_context.get("master_offset", 0)),
                notice="Добавление мастера отменено. Выберите другого пользователя.\n\n",
            )
        platform = Platform(str(user.dialog_context["promotion_target_platform"]))
        target = await self.users.get(platform, int(user.dialog_context["promotion_target_user_id"]))
        if target is None:
            return await self._show_creation_master_candidates(
                user,
                notice="Пользователь больше не найден. Выберите другого.\n\n",
            )
        reference = event_reference(
            str(user.dialog_context["event_name"]),
            date.fromisoformat(str(user.dialog_context["game_date"])),
        )
        if value != "create:master-promote:yes":
            return self._master_promotion_question(
                user,
                target,
                state="ADMIN_CREATE_MASTER_PROMOTE_CONFIRM",
                reference=reference,
                confirm_value="create:master-promote:yes",
                decline_value="create:master-promote:no",
            )
        target = await self._promote_master_target(user, message, target)
        return self._creation_schedule_question(user, target)

    async def _creation_master_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._is_admin(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        if value == "create:master-self":
            return self._creation_schedule_question(user, user)
        if value == "create:master-pick":
            return await self._show_creation_master_candidates(user)
        if value == "create:master-later":
            return self._creation_schedule_question(user, assign_master_later=True)
        if user.dialog_state == "ADMIN_CREATE_MASTER_PLATFORM":
            platforms = {
                "create:master-platform:telegram": Platform.TELEGRAM,
                "create:master-platform:vk": Platform.VK,
            }
            target_platform = platforms.get(value)
            if target_platform is None:
                return self._creation_master_platform_prompt()
            user.dialog_state = "ADMIN_CREATE_MASTER_PROFILE"
            user.dialog_context["master_profile_platform"] = target_platform.value
            prompt = (
                "Отправьте ссылку на профиль Telegram или @username будущего мастера:"
                if target_platform is Platform.TELEGRAM
                else "Отправьте ссылку на профиль VK будущего мастера:"
            )
            return BotResponse(text=prompt, buttons=[Button(label=BACK, value="create:master-pick")])
        if user.dialog_state == "ADMIN_CREATE_MASTER_PROFILE":
            return await self._select_creation_master_profile(user, value)
        if user.dialog_state == "ADMIN_CREATE_MASTER_PROMOTE_CONFIRM":
            return await self._creation_master_promotion_step(user, message, value)
        if user.dialog_state != "ADMIN_CREATE_MASTER_PICK" or value == "create:master-choice":
            return self._creation_master_question(user)
        offset = int(user.dialog_context.get("master_offset", 0))
        parts = value.split(":")
        if value.startswith("create:master-page:") and len(parts) == 3 and parts[2].isdecimal():
            return await self._show_creation_master_candidates(user, offset=int(parts[2]))
        if value == "create:master-direct":
            user.dialog_state = "ADMIN_CREATE_MASTER_PLATFORM"
            return self._creation_master_platform_prompt()
        if value.startswith("create:master:") and len(parts) == 4 and parts[2] in {"t", "v"} and parts[3].isdecimal():
            target = await self.users.get(self._platform_from_code(parts[2]), int(parts[3]))
            if target is not None and await self._has_admin_access(target):
                return self._creation_schedule_question(user, target)
            if target is not None:
                reference = event_reference(
                    str(user.dialog_context["event_name"]),
                    date.fromisoformat(str(user.dialog_context["game_date"])),
                )
                return self._master_promotion_question(
                    user,
                    target,
                    state="ADMIN_CREATE_MASTER_PROMOTE_CONFIRM",
                    reference=reference,
                    confirm_value="create:master-promote:yes",
                    decline_value="create:master-promote:no",
                )
            return await self._show_creation_master_candidates(
                user,
                offset=offset,
                notice="Мастером можно выбрать только привилегированного пользователя.\n\n",
            )
        return await self._show_creation_master_candidates(user, offset=offset)

    async def _creation_schedule_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if "created_event_id" in user.dialog_context:
            return await self._finish_game_creation(user)
        if not await self._has_admin_access(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        if user.dialog_state == "ADMIN_CREATE_SCHEDULE_CHOICE":
            if value == "create:schedule:yes":
                user.dialog_state = "ADMIN_CREATE_SCHEDULE_TIME"
                return _registration_schedule_response()
            if value == "create:schedule:no":
                user.dialog_context.pop("registration_open_at", None)
                return self._creation_confirmation_schedule_question(user)
            return self._creation_schedule_question(user)
        if value == "create:schedule:nearest-friday":
            registration_open_at = closest_friday_18()
        else:
            try:
                registration_open_at = parse_scheduled_action_time(value)
            except ValueError:
                return _registration_schedule_response(invalid=True)
            if registration_open_at <= datetime.now(MOSCOW_TIMEZONE):
                return _registration_schedule_response(past=True)
        scheduled_close_at = (
            datetime.fromisoformat(str(user.dialog_context["scheduled_close_at"]))
            if user.dialog_context.get("scheduled_close_at") is not None
            else None
        )
        if scheduled_close_at is not None and registration_open_at >= scheduled_close_at:
            return _registration_schedule_response(after_close=True)
        user.dialog_context["registration_open_at"] = registration_open_at.isoformat()
        return self._creation_confirmation_schedule_question(user)

    async def _creation_confirmation_schedule_step(
        self,
        user: User,
        message: InboundMessage,
        value: str,
    ) -> BotResponse:
        context = user.dialog_context
        if "created_event_id" in context:
            return await self._finish_game_creation(user)
        if value == "create:confirmation:no":
            context.pop("confirmation_open_at", None)
            context.pop("confirmation_deadline", None)
            return await self._create_game(user, message)
        if user.dialog_state == "ADMIN_CREATE_CONFIRMATION_SCHEDULE_CHOICE":
            if value != "create:confirmation:yes":
                return self._creation_confirmation_schedule_question(user)
            user.dialog_state = "ADMIN_CREATE_CONFIRMATION_SCHEDULE_TIME"
            return _scheduled_confirmation_time_response(
                tuesday_value="create:confirmation:tuesday",
                back=Button(label="Не хочу планировать открытие подтверждения", value="create:confirmation:no"),
            )
        if user.dialog_state == "ADMIN_CREATE_CONFIRMATION_SCHEDULE_TIME":
            if value == "create:confirmation:tuesday":
                opens_at = tuesday_before_game_18(date.fromisoformat(str(context["game_date"])))
            else:
                try:
                    opens_at = parse_scheduled_action_time(value)
                except ValueError:
                    return _scheduled_confirmation_time_response(
                        back=Button(
                            label="Не хочу планировать открытие подтверждения",
                            value="create:confirmation:no",
                        ),
                        tuesday_value="create:confirmation:tuesday",
                        invalid=SCHEDULED_TIME_INVALID,
                    )
            now = datetime.now(MOSCOW_TIMEZONE)
            registration_open_at = (
                datetime.fromisoformat(str(context["registration_open_at"]))
                if context.get("registration_open_at") is not None
                else None
            )
            scheduled_close_at = (
                datetime.fromisoformat(str(context["scheduled_close_at"]))
                if context.get("scheduled_close_at") is not None
                else None
            )
            if opens_at <= now:
                invalid = "Открытие подтверждения должно быть запланировано на будущее время."
            elif registration_open_at is not None and opens_at <= registration_open_at:
                invalid = "Подтверждение должно открываться строго после регистрации."
            elif scheduled_close_at is not None and opens_at >= scheduled_close_at:
                invalid = "Подтверждение должно открываться строго до закрытия игры."
            else:
                context["confirmation_open_at"] = opens_at.isoformat()
                user.dialog_state = "ADMIN_CREATE_CONFIRMATION_DEADLINE"
                return _scheduled_confirmation_deadline_response(
                    back=Button(
                        label="Не хочу планировать открытие подтверждения",
                        value="create:confirmation:no",
                    )
                )
            return _scheduled_confirmation_time_response(
                back=Button(
                    label="Не хочу планировать открытие подтверждения",
                    value="create:confirmation:no",
                ),
                tuesday_value="create:confirmation:tuesday",
                invalid=invalid,
            )
        try:
            deadline = parse_confirmation_deadline(value)
        except ValueError:
            return _scheduled_confirmation_deadline_response(
                back=Button(
                    label="Не хочу планировать открытие подтверждения",
                    value="create:confirmation:no",
                ),
                invalid="Некорректная дата и время. Попробуйте ещё раз.",
            )
        opens_at = datetime.fromisoformat(str(context["confirmation_open_at"]))
        scheduled_close_at = (
            datetime.fromisoformat(str(context["scheduled_close_at"]))
            if context.get("scheduled_close_at") is not None
            else None
        )
        if deadline <= opens_at:
            invalid = "Дедлайн должен быть строго позже открытия подтверждения."
        elif scheduled_close_at is not None and deadline >= scheduled_close_at:
            invalid = "Дедлайн подтверждения должен быть строго раньше закрытия игры."
        else:
            context["confirmation_deadline"] = deadline.isoformat()
            return await self._create_game(user, message)
        return _scheduled_confirmation_deadline_response(
            back=Button(
                label="Не хочу планировать открытие подтверждения",
                value="create:confirmation:no",
            ),
            invalid=invalid,
        )

    async def _creation_master(self, user: User) -> User | None:
        context = user.dialog_context
        if context.get("assign_master_later") is True:
            return None
        master = await self.users.get(
            Platform(str(context["master_platform"])),
            int(context["master_user_id"]),
        )
        if master is None or not await self._has_admin_access(master):
            raise ValueError("selected master is no longer privileged")
        return master

    async def _create_game(
        self,
        user: User,
        message: InboundMessage,
    ) -> BotResponse:
        context = user.dialog_context
        game_date = date.fromisoformat(str(context["game_date"]))
        try:
            master = await self._creation_master(user)
        except ValueError:
            await self._clear(user)
            return BotResponse(text="Выбранный мастер больше не является привилегированным пользователем.")
        master_identity = None if master is None else self._identity(master)
        requester = self._identity(user)
        masters_pending = master_identity is None
        master_label = (
            self._person_label(master_identity, master)
            if master_identity is not None and master_identity != requester
            else None
        )
        pending_buttons = [Button(label=BACK, value=MANAGE_GAMES)]
        before_invoke = self._upload_pending_callback(
            user,
            message,
            text="⏳ Файлы игры создаются. Бот пришлёт ссылки, когда загрузка завершится.",
            buttons=pending_buttons,
            operation="event",
        )
        event = await self.administration.create_event(
            str(context["event_name"]),
            game_date,
            int(context["player_amount"]),
            [] if master_identity is None else [master_identity],
            registration_open_at=(
                datetime.fromisoformat(str(context["registration_open_at"]))
                if context.get("registration_open_at") is not None
                else None
            ),
            confirmation_open_at=(
                datetime.fromisoformat(str(context["confirmation_open_at"]))
                if context.get("confirmation_open_at") is not None
                else None
            ),
            confirmation_deadline=(
                datetime.fromisoformat(str(context["confirmation_deadline"]))
                if context.get("confirmation_deadline") is not None
                else None
            ),
            scheduled_close_at=(
                datetime.fromisoformat(str(context["scheduled_close_at"]))
                if context.get("scheduled_close_at") is not None
                else None
            ),
            requester=requester,
            reply_user_id=message.chat_id or message.peer_id or requester.platform_user_id,
            master_label=master_label,
            masters_pending=masters_pending,
            before_invoke=before_invoke,
        )
        if event is None:
            await self._clear(user)
            return BotResponse(
                text="",
                buttons=pending_buttons,
                deferred=requester.platform is Platform.TELEGRAM,
                silent=True,
            )
        if master_identity is None:
            await self._clear(user)
            return await self._game_created_response(event, game_date, masters_pending=True)
        if master_identity == self._identity(user):
            await self._clear(user)
            return await self._game_created_response(event, game_date)
        # Persist the game before notifying, so a retried update finishes it instead of creating another.
        context["created_event_id"] = event.event_id
        context["master_platform"] = master_identity.platform.value
        context["master_user_id"] = master_identity.platform_user_id
        context["master_grant_request"] = (
            f"leader-grant:{event.event_id}:{master_identity.platform.value}:"
            f"{master_identity.platform_user_id}:{message.update_id}"
        )
        await self.users.save(user)
        return await self._finish_game_creation(user)

    async def _finish_game_creation(self, user: User) -> BotResponse:
        context = user.dialog_context
        event = await self.events.get(str(context["created_event_id"]))
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.")
        identity = BotIdentity(
            platform=Platform(str(context["master_platform"])),
            platform_user_id=int(context["master_user_id"]),
        )
        master = await self.users.get(identity.platform, identity.platform_user_id)
        request_id = str(context["master_grant_request"])
        if master is not None and master.last_delivery_operation_id != request_id:
            await self._notify_status_change(
                user,
                master,
                request_id=request_id,
                text=f"Вы добавлены мастером игры {event_reference(event.name, event.game_date)}.",
                buttons=[Button(label="Управление игрой", value=f"ag:manage:{event.event_id}")],
            )
        game_date = date.fromisoformat(str(context["game_date"]))
        await self._clear(user)
        return await self._game_created_response(event, game_date, master_label=self._person_label(identity, master))

    async def _game_created_response(
        self,
        event: Event,
        game_date: date,
        *,
        master_label: str | None = None,
        masters_pending: bool = False,
    ) -> BotResponse:
        if event.signup_token is None:
            raise RuntimeError("new game has no signup token")
        telegram_username, vk_group_id = await asyncio.gather(
            self.admins.get_secret("TG_BOT_USERNAME"),
            self.admins.get_secret("VK_GROUP_ID"),
        )
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", telegram_username) is None:
            raise RuntimeError("TG_BOT_USERNAME is invalid")
        if not vk_group_id.isdecimal() or int(vk_group_id) <= 0:
            raise RuntimeError("VK_GROUP_ID is invalid")
        telegram_start = f"https://t.me/{telegram_username}?start={event.signup_token}"
        vk_start = f"https://vk.me/club{vk_group_id}"
        if masters_pending:
            master_line = "Мастера: пока не назначены\n\n"
        else:
            master_line = "" if master_label is None else f"Мастер: {master_label}\n\n"
        if event.registration_open_at is None:
            registration_line = (
                f"Статус: {STATUS_REGISTRATION}. Игроки уже могут записываться, но пока не могут подтверждать участие."
            )
        else:
            registration_line = (
                "Открытие регистрации запланировано на "
                f"{format_confirmation_deadline(event.registration_open_at)} (МСК). "
                "До этого времени игра не будет видна игрокам."
            )
        close_line = (
            ""
            if event.scheduled_close_at is None
            else (
                "\n\nАвтоматическое закрытие запланировано на "
                f"{format_confirmation_deadline(event.scheduled_close_at)} (МСК)."
            )
        )
        confirmation_line = (
            ""
            if event.confirmation_open_at is None or event.confirmation_deadline is None
            else (
                "\n\nОткрытие подтверждения участия запланировано на "
                f"{format_confirmation_deadline(event.confirmation_open_at)} (МСК). "
                f"Дедлайн: {format_confirmation_deadline(event.confirmation_deadline)} (МСК)."
            )
        )
        return BotResponse(
            text=(
                f"✅ Игра создана.\n\nНазвание:\n{event.name}\n\n"
                f"Дата игры: {format_game_date(game_date)}\n\n"
                f"Количество игроков: {event.player_amount}\n\n"
                f"{master_line}"
                f"{registration_line}{confirmation_line}{close_line}\n\n"
                f"Telegram — начать запись:\n{telegram_start}\n\n"
                f"VK — открыть чат с ботом:\n{vk_start}\n\n"
                f"{event_table_links(event)}"
            ),
            buttons=[game_management_button(event.event_id)],
        )

    async def _show_event_masters(
        self,
        user: User,
        event: Event,
        mode: str,
        *,
        offset: int = 0,
    ) -> BotResponse:
        if offset < 0:
            return BotResponse(text="Некорректная страница.")
        records = await self._event_master_records(event.event_id)
        if mode == "remove":
            records = [record for record in records if record[1] is not None]
        if offset >= len(records) and offset:
            offset = max(0, ((len(records) - 1) // PAGE_SIZE) * PAGE_SIZE)
        page = records[offset : offset + PAGE_SIZE]
        user.dialog_state = "ADMIN_EVENT_LEADER_REMOVE" if mode == "remove" else "ADMIN_EVENT_LEADER_LIST"
        user.dialog_context = {
            "event_id": event.event_id,
            "event_name": event.name,
            "master_offset": offset,
        }
        action = "remove-pick" if mode == "remove" else "view"
        buttons = [
            Button(
                label=self._person_label(identity, profile),
                value=(f"master:{action}:{self._platform_code(identity.platform)}:{identity.platform_user_id}"),
            )
            for identity, profile in page
        ]
        buttons.extend(
            _pagination_buttons(
                previous=f"master:{mode}-page:{max(0, offset - PAGE_SIZE)}" if offset else None,
                following=f"master:{mode}-page:{offset + PAGE_SIZE}" if offset + PAGE_SIZE < len(records) else None,
                cancel=f"ag:masters:{event.event_id}",
            )
        )
        if mode == "remove":
            text = f"Выберите мастера, которого нужно удалить из игры {event_reference(event.name, event.game_date)}:"
            empty = "В этой игре нет назначенных мастеров, которых можно удалить."
        else:
            text = f"Мастера игры {event_reference(event.name, event.game_date)}:"
            empty = "У этой игры пока нет мастеров."
        return BotResponse(text=text if page else empty, buttons=buttons)
