from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from larp_bot.domain.models import (
    AttendanceStatus,
    BotResponse,
    Button,
    CharacterWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    PassDetails,
    Platform,
    ReplyContext,
    TelegramUser,
    User,
    normalize_telegram_handle,
    normalize_vk_url,
)

from .ports import AdminConfigProvider, EventRepository, UserRepository, new_user
from .services import EventAdministrationService, RegistrationService

PROFILE = "📝 Профиль"
ENLIST = "🎮 Записаться на игру"
CONFIRM = "✅ Подтвердить участие"
CHARACTER = "🎭 Пожелания по персонажу"
CANCEL = "❌ Отменить участие"
ADMIN = "🛠 Администрирование"
BACK = "⬅️ Назад"
CANCEL_DIALOG = "Отмена"
NO_WISHES = "Без пожеланий"
KEEP = "Оставить без изменений"


def yes_no_buttons() -> list[Button]:
    return [Button(label="Да", value="Да"), Button(label="Нет", value="Нет")]


def main_buttons(is_admin: bool) -> list[Button]:
    labels = [PROFILE, ENLIST, CONFIRM, CHARACTER, CANCEL]
    if is_admin:
        labels.append(ADMIN)
    return [Button(label=label, value=label) for label in labels]


def user_id(user: User) -> int:
    return user.tg_id if isinstance(user, TelegramUser) else user.vk_id


def is_profile_complete(user: User) -> bool:
    return user.profile_complete


def _bool_text(value: bool | None) -> str:
    return "Да" if value else "Нет"


def _event_buttons(events: list[Event], flow: str) -> list[Button]:
    return [Button(label=event.name, value=f"select:{flow}:{event.event_id}") for event in events]


class ConversationEngine:
    """One platform-neutral state machine rendered by Telegram and VK adapters."""

    def __init__(
        self,
        users: UserRepository,
        events: EventRepository,
        registrations: RegistrationService,
        administration: EventAdministrationService,
        admins: AdminConfigProvider,
    ) -> None:
        self.users = users
        self.events = events
        self.registrations = registrations
        self.administration = administration
        self.admins = admins

    async def _main(self, user: User, text: str = "Выберите действие:") -> BotResponse:
        admin = await self.admins.is_admin(
            Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK, user_id(user)
        )
        return BotResponse(text=text, buttons=main_buttons(admin))

    async def _save(self, user: User, message: InboundMessage) -> None:
        user.last_update_id = message.update_id
        user.last_update_at = datetime.now(UTC)
        user.updated_at = datetime.now(UTC)
        await self.users.save(user)

    async def _clear(self, user: User) -> None:
        user.dialog_state = "IDLE"
        user.dialog_context = {}

    async def handle(self, message: InboundMessage) -> BotResponse:
        platform = message.identity.platform
        uid = message.identity.platform_user_id
        user = await self.users.get(platform, uid) or new_user(platform, uid)
        if user.last_update_id == message.update_id:
            return BotResponse(text="", silent=True, deferred=platform is Platform.TELEGRAM)

        value = (message.callback or message.text).strip()
        if value in {"/start", "/menu", BACK}:
            await self._clear(user)
            response = await self._main(user, "Привет! Этот бот поможет зарегистрироваться на LARP-игры.")
        elif value == CANCEL_DIALOG:
            await self._clear(user)
            response = await self._main(user, "Действие отменено.")
        elif user.dialog_state != "IDLE":
            response = await self._continue(user, message, value)
        else:
            response = await self._start(user, message, value)
        await self._save(user, message)
        return response

    async def _start(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if value == PROFILE:
            if is_profile_complete(user):
                contact = user.vk_url if isinstance(user, TelegramUser) else user.telegram_handle
                contact_label = "VK" if isinstance(user, TelegramUser) else "Telegram"
                return BotResponse(
                    text=(
                        f"Профиль\n\nИмя: {user.full_name}\n{contact_label}: {contact or 'не указан'}\n"
                        f"Кроссплей: {_bool_text(user.crossplay)}\n"
                        f"Опыт LARP: {_bool_text(user.larp_experience)}\n"
                        f"Нужен пропуск: {_bool_text(user.needs_pass)}"
                    ),
                    buttons=[
                        Button(label="Изменить профиль", value="profile:edit"),
                        Button(label=BACK, value=BACK),
                    ],
                )
            return await self._begin_profile(user)
        if value == "profile:edit":
            return await self._begin_profile(user)
        if value == ENLIST:
            if not is_profile_complete(user):
                return BotResponse(
                    text="Сначала зарегистрируйте профиль.",
                    buttons=[Button(label=PROFILE, value=PROFILE)],
                )
            return await self._show_open_events("enlist")
        if value == CONFIRM:
            return await self._show_registered(user, "confirm")
        if value == CHARACTER:
            return await self._show_registered(user, "character")
        if value == CANCEL:
            return await self._show_registered(user, "cancel")
        if value == ADMIN:
            return await self._admin_menu(user)
        if value.startswith("select:"):
            return await self._select_event(user, message, value)
        if value.startswith("page:"):
            return await self._page(user, value)
        admin_response = await self.dispatch_idle_admin(user, value)
        if admin_response is not None:
            return admin_response
        return await self._main(user)

    async def _begin_profile(self, user: User) -> BotResponse:
        user.dialog_state = "PROFILE_NAME"
        user.dialog_context = {}
        return BotResponse(
            text="Введите ваши Фамилию и Имя:",
            buttons=[Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)],
        )

    async def _continue(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        state = user.dialog_state
        if state.startswith("PROFILE_"):
            return await self._profile_step(user, value)
        if state.startswith("ENLIST_"):
            return await self._enlist_step(user, message, value)
        if state == "CONFIRM_WISH":
            return await self._character_command(user, message, value, Operation.CONFIRM)
        if state == "CHARACTER_EDIT":
            return await self._character_command(user, message, value, Operation.UPDATE_CHARACTER_WISH)
        if state == "CANCEL_CONFIRM":
            return await self._cancel_step(user, message, value)
        if state.startswith("ADMIN_"):
            return await self._admin_step(user, message, value)
        await self._clear(user)
        return await self._main(user, "Диалог сброшен. Выберите действие:")

    async def _profile_step(self, user: User, value: str) -> BotResponse:
        context = user.dialog_context
        state = user.dialog_state
        if state == "PROFILE_NAME":
            if len(value) < 2:
                return BotResponse(text="Введите непустые Фамилию и Имя:")
            context["full_name"] = value[:300]
            user.dialog_state = "PROFILE_CONTACT"
            if isinstance(user, TelegramUser):
                return BotResponse(text="Введите обязательную ссылку на вашу страницу VK:")
            return BotResponse(
                text="Ваш Telegram username, если он есть:",
                buttons=[Button(label="Пропустить", value="Пропустить")],
            )
        if state == "PROFILE_CONTACT":
            try:
                context["contact"] = (
                    normalize_vk_url(value) if isinstance(user, TelegramUser) else normalize_telegram_handle(value)
                )
            except ValueError as exc:
                return BotResponse(text=f"❌ {exc}. Попробуйте ещё раз:")
            user.dialog_state = "PROFILE_CROSSPLAY"
            return BotResponse(text="Готовы ли вы кроссплею?", buttons=yes_no_buttons())
        boolean_states = {
            "PROFILE_CROSSPLAY": ("crossplay", "PROFILE_EXPERIENCE", "Играли ли вы в LARP до этого?"),
            "PROFILE_EXPERIENCE": (
                "larp_experience",
                "PROFILE_NEEDS_PASS",
                "Нужен ли вам пропуск на локацию?",
            ),
        }
        if state in boolean_states:
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            key, next_state, prompt = boolean_states[state]
            context[key] = value == "Да"
            user.dialog_state = next_state
            return BotResponse(text=prompt, buttons=yes_no_buttons())
        if state == "PROFILE_NEEDS_PASS":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["needs_pass"] = value == "Да"
            if value == "Да":
                user.dialog_state = "PROFILE_PASS_CYRILLIC"
                return BotResponse(text="Введите полное ФИО кириллицей:")
            return await self._finish_profile(user)
        prompts = {
            "PROFILE_PASS_CYRILLIC": ("legal_name_cyrillic", "PROFILE_PASS_LATIN", "Введите полное ФИО латиницей:"),
            "PROFILE_PASS_LATIN": ("legal_name_latin", "PROFILE_PASS_EMAIL", "Введите email:"),
        }
        if state in prompts:
            if len(value) < 2:
                return BotResponse(text="Значение слишком короткое. Попробуйте ещё раз:")
            key, next_state, prompt = prompts[state]
            context[key] = value[:300]
            user.dialog_state = next_state
            return BotResponse(text=prompt)
        if state == "PROFILE_PASS_EMAIL":
            context["email"] = value
            user.dialog_state = "PROFILE_PASS_CITIZEN"
            return BotResponse(text="Являетесь ли вы гражданином РФ?", buttons=yes_no_buttons())
        if state == "PROFILE_PASS_CITIZEN":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=yes_no_buttons())
            context["russian_citizen"] = value == "Да"
            try:
                return await self._finish_profile(user)
            except ValidationError:
                user.dialog_state = "PROFILE_PASS_EMAIL"
                return BotResponse(text="Некорректный email. Введите email ещё раз:")
        raise AssertionError(f"unknown profile state: {state}")

    async def _finish_profile(self, user: User) -> BotResponse:
        context = user.dialog_context
        user.full_name = str(context["full_name"])
        user.crossplay = bool(context["crossplay"])
        user.larp_experience = bool(context["larp_experience"])
        user.needs_pass = bool(context["needs_pass"])
        if user.needs_pass:
            user.pass_details = PassDetails(
                legal_name_cyrillic=str(context["legal_name_cyrillic"]),
                legal_name_latin=str(context["legal_name_latin"]),
                email=str(context["email"]),
                russian_citizen=bool(context["russian_citizen"]),
            )
        else:
            user.pass_details = None
        if isinstance(user, TelegramUser):
            user.vk_url = str(context["contact"])
        else:
            contact = context.get("contact")
            user.telegram_handle = None if contact is None else str(contact)
        await self._clear(user)
        return await self._main(user, "✅ Профиль сохранён.")

    async def _show_open_events(self, flow: str) -> BotResponse:
        events = list(await self.events.list_page(status=EventStatus.OPEN, limit=10))
        if not events:
            return BotResponse(text="Сейчас нет открытых игр.", buttons=[Button(label=BACK, value=BACK)])
        buttons = _event_buttons(events, flow)
        if len(events) == 10:
            last = events[-1]
            more = await self.events.list_page(status=EventStatus.OPEN, after=(last.created_at, last.event_id), limit=1)
            if more:
                buttons.append(Button(label="➡️ Далее", value=self._cursor(flow, last)))
        buttons.append(Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG))
        return BotResponse(text="🎮 Выберите игру:", buttons=buttons)

    @staticmethod
    def _cursor(flow: str, event: Event) -> str:
        micros = int(event.created_at.timestamp() * 1_000_000)
        return f"page:{flow}:{micros}:{event.event_id}"

    async def _page(self, user: User, value: str) -> BotResponse:
        parts = value.split(":", 3)
        if len(parts) != 4 or not parts[2].isdigit():
            return BotResponse(text="Некорректная страница.")
        _, flow, micros, event_id = parts
        after = (datetime.fromtimestamp(int(micros) / 1_000_000, UTC), event_id)
        if flow in {"enlist", "admin-close", "admin-delete"}:
            status = EventStatus.OPEN if flow in {"enlist", "admin-close"} else None
            events = list(await self.events.list_page(status=status, after=after, limit=10))
            buttons = _event_buttons(events, flow)
            if len(events) == 10:
                buttons.append(Button(label="➡️ Далее", value=self._cursor(flow, events[-1])))
            buttons.append(Button(label=BACK, value=BACK))
            return BotResponse(text="Выберите игру:", buttons=buttons)
        if flow == "admin-list":
            return await self._admin_list(after)
        return await self._show_registered(user, flow, after=after)

    async def _show_registered(
        self,
        user: User,
        flow: str,
        *,
        after: tuple[datetime, str] | None = None,
    ) -> BotResponse:
        if not is_profile_complete(user):
            return BotResponse(
                text="Сначала зарегистрируйте профиль.",
                buttons=[Button(label=PROFILE, value=PROFILE)],
            )
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        matches, cursor = await self.registrations.registered_games_page(platform, user_id(user), after=after)
        buttons = [Button(label=event.name, value=f"select:{flow}:{event.event_id}") for event, _ in matches]
        if cursor is not None:
            micros = int(cursor[0].timestamp() * 1_000_000)
            buttons.append(Button(label="➡️ Далее", value=f"page:{flow}:{micros}:{cursor[1]}"))
        buttons.append(Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG))
        if not matches and cursor is None:
            return BotResponse(text="У вас пока нет записей на игры.", buttons=buttons)
        return BotResponse(text="🎭 Выберите игру:", buttons=buttons)

    async def _select_event(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        try:
            _, flow, event_id = value.split(":", 2)
        except ValueError:
            return BotResponse(text="Некорректный выбор.")
        event = await self.events.get(event_id)
        if event is None:
            return BotResponse(text="Игра не найдена.")
        platform = message.identity.platform
        uid = message.identity.platform_user_id
        if flow == "enlist":
            if event.status is EventStatus.CLOSED:
                return BotResponse(text="Регистрация на эту игру уже закрыта.")
            user.dialog_context = {"event_id": event_id, "event_name": event.name}
            user.dialog_state = "ENLIST_WISH_PLAY"
            return BotResponse(text="С кем бы вы ХОТЕЛИ играть?")
        if flow in {"admin-close", "admin-delete"}:
            return await self._admin_select(user, event, flow)
        registration = await self.registrations.get_registration(event_id, platform, uid)
        if registration is None:
            return BotResponse(text="Сначала запишитесь на эту игру.")
        user.dialog_context = {
            "event_id": event_id,
            "event_name": event.name,
            "existing_character_wish": registration.character_wish,
        }
        if flow == "confirm":
            user.dialog_state = "CONFIRM_WISH"
            current = registration.character_wish or "ещё не указаны"
            buttons = [Button(label=NO_WISHES, value=NO_WISHES), Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)]
            if registration.character_wish:
                buttons.insert(0, Button(label=KEEP, value=KEEP))
            return BotResponse(
                text=(
                    f"Игра: «{event.name}»\nСтатус: {registration.attendance_status.value}\n\n"
                    f"Текущие пожелания: {current}\n\n"
                    "Отправьте пожелания по персонажу для этой игры."
                ),
                buttons=buttons,
            )
        if flow == "character":
            if registration.attendance_status is AttendanceStatus.CANCELLED:
                await self._clear(user)
                return BotResponse(text="Запись отменена. Сначала снова подтвердите участие.")
            if registration.attendance_status is AttendanceStatus.WAITING and not registration.character_wish:
                await self._clear(user)
                return BotResponse(
                    text=("Пожелания ещё не вводились. Впервые укажите их через «✅ Подтвердить участие».")
                )
            user.dialog_state = "CHARACTER_EDIT"
            return BotResponse(
                text=(
                    f"Игра: «{event.name}»\nСтатус: {registration.attendance_status.value}\n\n"
                    f"Текущие пожелания:\n{registration.character_wish}\n\n"
                    "Отправьте новый вариант."
                ),
                buttons=[Button(label=NO_WISHES, value=NO_WISHES), Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG)],
            )
        if flow == "cancel":
            user.dialog_state = "CANCEL_CONFIRM"
            return BotResponse(
                text=f"Отменить участие в игре «{event.name}»?",
                buttons=[
                    Button(label="Да, отменить", value="cancel:yes"),
                    Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                ],
            )
        return BotResponse(text="Некорректное действие.")

    async def _enlist_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not value:
            return BotResponse(text="Введите ответ текстом.")
        if user.dialog_state == "ENLIST_WISH_PLAY":
            user.dialog_context["wish_play"] = value[:2000]
            user.dialog_state = "ENLIST_DONT_WISH"
            return BotResponse(text="С кем бы вы НЕ ХОТЕЛИ играть?")
        if user.dialog_state == "ENLIST_DONT_WISH":
            user.dialog_context["dont_wish_play"] = value[:2000]
            user.dialog_state = "ENLIST_CONFIRM"
            return BotResponse(
                text=(
                    f"Игра: «{user.dialog_context['event_name']}»\n\n"
                    f"Хочу играть: {user.dialog_context['wish_play']}\n"
                    f"Не хочу играть: {value}\n\nПодтвердить запись?"
                ),
                buttons=[
                    Button(label="Подтвердить", value="enlist:confirm"),
                    Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                ],
            )
        if user.dialog_state == "ENLIST_CONFIRM":
            if value != "enlist:confirm":
                return BotResponse(text="Нажмите «Подтвердить» или «Отмена».")
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.ENLIST,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EnlistPayload(
                    display_name=user.full_name or "",
                    wish_play=str(context["wish_play"]),
                    dont_wish_play=str(context["dont_wish_play"]),
                ),
                reply_context=ReplyContext(
                    chat_id=message.chat_id,
                    peer_id=message.peer_id,
                    text_success=(
                        f"🎲 Вы записаны на игру «{context['event_name']}».\n\n"
                        "Когда придёт время окончательно подтвердить участие, выберите "
                        "«✅ Подтвердить участие». Тогда бот попросит пожелания по персонажу."
                    ),
                ),
                idempotency_key=f"{message.update_id}:ENLIST",
            )
            return BotResponse(text="⏳ Запись принята в обработку.", deferred=True, command_enqueued=True)
        raise AssertionError("unknown enlist state")

    async def _character_command(
        self,
        user: User,
        message: InboundMessage,
        value: str,
        operation: Operation,
    ) -> BotResponse:
        context = user.dialog_context.copy()
        if value == KEEP:
            existing = str(context.get("existing_character_wish", ""))
            if not existing:
                return BotResponse(text="Сохранённых пожеланий пока нет. Введите текст или выберите «Без пожеланий».")
            wish = existing
        elif value == NO_WISHES:
            wish = NO_WISHES
        else:
            wish = value.strip()
            if not wish:
                return BotResponse(text="Введите текст или выберите «Без пожеланий».")
        await self._clear(user)
        await self.registrations.enqueue(
            operation=operation,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=CharacterWishPayload(character_wish=wish),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=(
                    f"✅ Участие в игре «{context['event_name']}» подтверждено.\n\n"
                    f"Пожелания по персонажу:\n{wish}\n\n"
                    "Позже их можно изменить через «🎭 Пожелания по персонажу»."
                    if operation is Operation.CONFIRM
                    else f"🎭 Пожелания для игры «{context['event_name']}» обновлены."
                ),
            ),
            idempotency_key=f"{message.update_id}:{operation.value}",
        )
        return BotResponse(text="⏳ Изменение принято в обработку.", deferred=True, command_enqueued=True)

    async def _cancel_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if value != "cancel:yes":
            return BotResponse(text="Подтвердите отмену кнопкой или выберите «Отмена».")
        context = user.dialog_context.copy()
        await self._clear(user)
        await self.registrations.enqueue(
            operation=Operation.CANCEL,
            event_id=str(context["event_id"]),
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=EmptyPayload(),
            reply_context=ReplyContext(
                chat_id=message.chat_id,
                peer_id=message.peer_id,
                text_success=f"❌ Участие в игре «{context['event_name']}» отменено.",
            ),
            idempotency_key=f"{message.update_id}:CANCEL",
        )
        return BotResponse(text="⏳ Отмена принята в обработку.", deferred=True, command_enqueued=True)

    async def _admin_menu(self, user: User) -> BotResponse:
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        if not await self.admins.is_admin(platform, user_id(user)):
            return BotResponse(text="Недостаточно прав.")
        labels = ["➕ Создать игру", "🔒 Закрыть регистрацию", "🗑 Удалить игру", "📋 Список игр", BACK]
        return BotResponse(text="🛠 Администрирование", buttons=[Button(label=x, value=x) for x in labels])

    async def _require_admin(self, user: User) -> bool:
        platform = Platform.TELEGRAM if isinstance(user, TelegramUser) else Platform.VK
        return await self.admins.is_admin(platform, user_id(user))

    async def _admin_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        if not await self._require_admin(user):
            await self._clear(user)
            return BotResponse(text="Недостаточно прав.")
        if user.dialog_state == "ADMIN_CREATE_NAME":
            event = await self.administration.create_event(value)
            await self._clear(user)
            return BotResponse(
                text=(
                    f"✅ Игра создана.\n\nНазвание:\n{event.name}\n\n"
                    f"Таблица регистрации:\n{event.public_registration_url}"
                )
            )
        if user.dialog_state == "ADMIN_CLOSE_CONFIRM":
            if value != "admin:close:yes":
                return BotResponse(text="Подтвердите закрытие кнопкой или выберите «Отмена».")
            context = user.dialog_context.copy()
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.CLOSE_EVENT,
                event_id=str(context["event_id"]),
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(text_success=f"🔒 Регистрация на «{context['event_name']}» закрыта."),
                idempotency_key=f"{message.update_id}:CLOSE_EVENT",
            )
            return BotResponse(text="⏳ Закрытие принято в обработку.", deferred=True, command_enqueued=True)
        if user.dialog_state == "ADMIN_DELETE_NAME":
            expected = str(user.dialog_context["event_name"])
            if value.strip() != expected:
                return BotResponse(text=f"Название не совпало. Введите точно:\n\n{expected}")
            event_id = str(user.dialog_context["event_id"])
            await self._clear(user)
            await self.registrations.enqueue(
                operation=Operation.DELETE_EVENT,
                event_id=event_id,
                platform=message.identity.platform,
                user_id=message.identity.platform_user_id,
                payload=EmptyPayload(),
                reply_context=ReplyContext(text_success=f"🗑 Игра «{expected}» удалена."),
                idempotency_key=f"{message.update_id}:DELETE_EVENT",
            )
            return BotResponse(text="⏳ Удаление принято в обработку.", deferred=True, command_enqueued=True)
        await self._clear(user)
        return BotResponse(text="Административный диалог сброшен.")

    async def _admin_select(self, user: User, event: Event, flow: str) -> BotResponse:
        if not await self._require_admin(user):
            return BotResponse(text="Недостаточно прав.")
        user.dialog_context = {"event_id": event.event_id, "event_name": event.name}
        if flow == "admin-close":
            user.dialog_state = "ADMIN_CLOSE_CONFIRM"
            return BotResponse(
                text=f"Закрыть регистрацию на игру «{event.name}»?",
                buttons=[
                    Button(label="Да, закрыть", value="admin:close:yes"),
                    Button(label=CANCEL_DIALOG, value=CANCEL_DIALOG),
                ],
            )
        user.dialog_state = "ADMIN_DELETE_NAME"
        return BotResponse(
            text=(
                "⚠️ Это действие необратимо. Будут удалены игра, публичная таблица и все записи.\n\n"
                f"Для подтверждения введите точное название:\n\n{event.name}"
            )
        )

    async def _admin_list(self, after: tuple[datetime, str] | None = None) -> BotResponse:
        events = list(await self.events.list_page(after=after, limit=10))
        if not events:
            return BotResponse(text="Игр нет.", buttons=[Button(label=BACK, value=BACK)])
        lines = []
        for event in events:
            lines.append(
                f"{event.name}\n{event.status.value} · {event.created_at:%d.%m.%Y}\n{event.public_registration_url}"
            )
        buttons: list[Button] = []
        if len(events) == 10:
            last = events[-1]
            more = await self.events.list_page(after=(last.created_at, last.event_id), limit=1)
            if more:
                buttons.append(Button(label="➡️ Далее", value=self._cursor("admin-list", last)))
        buttons.append(Button(label=BACK, value=BACK))
        return BotResponse(text="📋 Список игр\n\n" + "\n\n".join(lines), buttons=buttons)

    async def _admin_start(self, user: User, value: str) -> BotResponse:
        if not await self._require_admin(user):
            return BotResponse(text="Недостаточно прав.")
        if value == "➕ Создать игру":
            user.dialog_state = "ADMIN_CREATE_NAME"
            return BotResponse(text="Введите название игры:")
        if value == "🔒 Закрыть регистрацию":
            return await self._show_open_events("admin-close")
        if value == "🗑 Удалить игру":
            events = list(await self.events.list_page(limit=10))
            return BotResponse(text="Выберите игру:", buttons=_event_buttons(events, "admin-delete"))
        if value == "📋 Список игр":
            return await self._admin_list()
        return await self._admin_menu(user)

    # Admin submenu commands arrive while the user is IDLE, so extend the root dispatcher.
    async def dispatch_idle_admin(self, user: User, value: str) -> BotResponse | None:
        if value in {"➕ Создать игру", "🔒 Закрыть регистрацию", "🗑 Удалить игру", "📋 Список игр"}:
            return await self._admin_start(user, value)
        return None
