from __future__ import annotations

from pydantic import ValidationError

from larp_bot.domain.models import (
    BotResponse,
    Button,
    InboundMessage,
    Operation,
    PassDetails,
    ProfileSyncPayload,
    TelegramUser,
    User,
    normalize_mobile_phone,
    normalize_nickname,
    normalize_vk_url,
    parse_full_name,
    surname_and_name,
    validate_pass_email,
)

from .conversation_context import ConversationContext
from .conversation_support import (
    BACK,
    CYRILLIC_FULL_NAME_PROMPT,
    CYRILLIC_FULL_NAME_RETRY_PROMPT,
    EDIT_PROFILE,
    EMAIL_PROMPT,
    LATIN_FULL_NAME_PROMPT,
    LATIN_FULL_NAME_RETRY_PROMPT,
    NICKNAME_PROMPT,
    PHONE_PROMPT,
    PROFILE,
    PROFILE_EDIT_ACTIONS,
    PROFILE_EDITED,
    PROFILE_FORMAT,
    REFILL_PROFILE,
    SKIP,
    VENUE_ADDRESS,
    _bool_text,
    _normalize_vk_telegram_contact,
    _pass_name,
    nickname_buttons,
    profile_dialog_exit_button,
    profile_yes_no_buttons,
)
from .navigation import main_menu_button


class ProfileConversationMixin(ConversationContext):
    """Profile display and editing flows."""

    @staticmethod
    def _profile_lines(user: User, *, role: str, contacts: list[str]) -> list[str]:
        details = user.pass_details
        full_name = user.full_name or "не указано"
        if details is not None:
            full_name = _pass_name((details.surname_cyrillic, details.name_cyrillic, details.patronym_cyrillic))
        lines = [
            f"ФИО: {full_name}",
            f"Никнейм: {user.nickname or 'не указан'}",
            f"Статус: {role}",
            *contacts,
            f"Кросспол: {_bool_text(user.crossplay)}",
            f"Нужен пропуск: {_bool_text(user.needs_pass)}",
        ]
        if details is not None:
            lines.extend(
                [
                    f"Номер телефона: {details.mobile_phone}",
                    f"Email: {details.email}",
                    f"Российское гражданство: {_bool_text(not details.foreigner)}",
                ]
            )
            if details.foreigner:
                lines.append(
                    f"ФИО латиницей: {_pass_name((details.surname_latin, details.name_latin, details.patronym_latin))}"
                )
        return lines

    async def _profile_response(self, user: User, *, prefix: str | None = None) -> BotResponse:
        contact = user.vk_url if isinstance(user, TelegramUser) else user.telegram_handle
        contact_label = "Профиль ВКонтакте" if isinstance(user, TelegramUser) else "Профиль Telegram"
        role = await self._user_role_label(user)
        lines = [
            "Профиль",
            "",
            *self._profile_lines(user, role=role, contacts=[f"{contact_label}: {contact or 'не указан'}"]),
        ]
        text = "\n".join(lines)
        if prefix is not None:
            text = f"{prefix}\n\n{text}"
        return BotResponse(
            text=text,
            buttons=[
                main_menu_button(),
                Button(label=EDIT_PROFILE, value="profile:menu"),
            ],
        )

    @staticmethod
    def _profile_edit_menu(user: User) -> BotResponse:
        contact_label = "Профиль ВКонтакте" if isinstance(user, TelegramUser) else "Профиль Telegram"
        buttons = [
            Button(label=REFILL_PROFILE, value="profile:reset"),
            Button(label=PROFILE_EDIT_ACTIONS["profile:field:name"], value="profile:field:name"),
            Button(label=PROFILE_EDIT_ACTIONS["profile:field:nickname"], value="profile:field:nickname"),
            Button(label=contact_label, value="profile:field:contact"),
            Button(label=PROFILE_EDIT_ACTIONS["profile:field:crossplay"], value="profile:field:crossplay"),
            Button(label=PROFILE_EDIT_ACTIONS["profile:field:pass"], value="profile:field:pass"),
        ]
        details = user.pass_details
        if user.needs_pass and details is not None:
            buttons.extend(
                [
                    Button(label=PROFILE_EDIT_ACTIONS["profile:field:phone"], value="profile:field:phone"),
                    Button(label=PROFILE_EDIT_ACTIONS["profile:field:email"], value="profile:field:email"),
                    Button(
                        label=PROFILE_EDIT_ACTIONS["profile:field:citizenship"],
                        value="profile:field:citizenship",
                    ),
                ]
            )
            if details.foreigner:
                buttons.append(
                    Button(
                        label=PROFILE_EDIT_ACTIONS["profile:field:latin-name"],
                        value="profile:field:latin-name",
                    )
                )
        buttons.append(Button(label=BACK, value=PROFILE))
        return BotResponse(text="Что вы хотите изменить?", buttons=buttons)

    async def _begin_profile(
        self,
        user: User,
        *,
        editing_existing: bool = False,
        signup_event_id: str | None = None,
    ) -> BotResponse:
        user.dialog_state = "PROFILE_FULL_NAME_CYRILLIC"
        user.dialog_context = {
            "profile_format": PROFILE_FORMAT,
            "profile_flow": "reset" if editing_existing else "initial",
        }
        if signup_event_id is not None:
            user.dialog_context["signup_event_id"] = signup_event_id
        return BotResponse(
            text=(
                CYRILLIC_FULL_NAME_PROMPT
                if editing_existing
                else f"Перед записью на игру необходимо обязательно заполнить профиль.\n\n{CYRILLIC_FULL_NAME_PROMPT}"
            ),
            buttons=[profile_dialog_exit_button(user)],
        )

    def _begin_profile_field(self, user: User, field: str) -> BotResponse:
        context: dict[str, object] = {
            "profile_format": PROFILE_FORMAT,
            "profile_flow": "field",
            "profile_field": field,
        }
        details = user.pass_details
        if details is not None:
            context.update(details.model_dump())
        elif user.full_name is not None:
            surname, name = user.full_name.split()[:2]
            context.update(
                surname_cyrillic=surname,
                name_cyrillic=name,
                patronym_cyrillic="-",
            )
        user.dialog_context = context
        if field == "name":
            user.dialog_state = "PROFILE_FULL_NAME_CYRILLIC"
            return BotResponse(text=CYRILLIC_FULL_NAME_PROMPT, buttons=[profile_dialog_exit_button(user)])
        if field == "nickname":
            user.dialog_state = "PROFILE_NICKNAME"
            return BotResponse(text=NICKNAME_PROMPT, buttons=nickname_buttons(user))
        if field == "contact":
            return self._profile_contact_prompt(user)
        if field == "crossplay":
            user.dialog_state = "PROFILE_CROSSPLAY"
            return BotResponse(
                text="Готовы ли вы играть за персонажа противоположного пола?",
                buttons=profile_yes_no_buttons(user),
            )
        if field == "pass":
            user.dialog_state = "PROFILE_NEEDS_PASS"
            return BotResponse(
                text=f"Нужен ли вам пропуск для входа на место проведения игры ({VENUE_ADDRESS})?",
                buttons=profile_yes_no_buttons(user),
            )
        if details is None:
            return self._profile_edit_menu(user)
        if field == "phone":
            user.dialog_state = "PROFILE_PASS_PHONE"
            return BotResponse(text=PHONE_PROMPT, buttons=[profile_dialog_exit_button(user)])
        if field == "email":
            user.dialog_state = "PROFILE_PASS_EMAIL_ADDRESS"
            return BotResponse(text=EMAIL_PROMPT, buttons=[profile_dialog_exit_button(user)])
        if field == "citizenship":
            user.dialog_state = "PROFILE_PASS_FOREIGNER"
            return BotResponse(text="Есть ли у вас российское гражданство?", buttons=profile_yes_no_buttons(user))
        if field == "latin-name" and details.foreigner:
            user.dialog_state = "PROFILE_PASS_FULL_NAME_LATIN"
            return BotResponse(text=LATIN_FULL_NAME_PROMPT, buttons=[profile_dialog_exit_button(user)])
        return self._profile_edit_menu(user)

    @staticmethod
    def _profile_contact_prompt(user: User) -> BotResponse:
        user.dialog_state = "PROFILE_CONTACT"
        if isinstance(user, TelegramUser):
            return BotResponse(
                text="Введите обязательную ссылку на вашу страницу VK:",
                buttons=[profile_dialog_exit_button(user)],
            )
        return BotResponse(
            text=(
                "Укажите имя пользователя Telegram в формате @username или ссылку https://t.me/username. "
                "Если Telegram нет, нажмите «Пропустить»."
            ),
            buttons=[Button(label=SKIP, value=SKIP), profile_dialog_exit_button(user)],
        )

    async def _continue(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        state = user.dialog_state
        if state.startswith("PROFILE_"):
            return await self._profile_step(user, message, value)
        if state.startswith("ENLIST_"):
            return await self._enlist_step(user, message, value)
        if state == "CONFIRM_REPLACE":
            return await self._confirm_replacement_step(user, message, value)
        if state == "CONFIRM_WISH":
            return await self._character_command(user, message, value, Operation.CONFIRM)
        if state == "CHARACTER_EDIT":
            return await self._character_command(user, message, value, Operation.UPDATE_CHARACTER_WISH)
        if state == "CO_PLAYER_EDIT":
            return await self._co_player_wish_command(user, message, value)
        if state == "CANCEL_CONFIRM":
            return await self._cancel_step(user, message, value)
        if state.startswith("ADMIN_"):
            return await self._admin_step(user, message, value)
        await self._clear(user)
        return await self._main(user, "Предыдущее действие прервано. Выберите действие:")

    async def _profile_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        context = user.dialog_context
        state = user.dialog_state
        if context.get("profile_format") != PROFILE_FORMAT:
            response = await self._begin_profile(user)
            response.text = f"Формат профиля обновился. {response.text}"
            return response
        if context.get("profile_flow") == "field":
            return await self._profile_field_step(user, message, value)
        if state == "PROFILE_FULL_NAME_CYRILLIC":
            try:
                name_parts = parse_full_name(value, alphabet="cyrillic")
            except ValueError:
                return BotResponse(
                    text=CYRILLIC_FULL_NAME_RETRY_PROMPT,
                    buttons=[profile_dialog_exit_button(user)],
                )
            surname, name = name_parts[:2]
            patronym = name_parts[2] if len(name_parts) == 3 else "-"
            context["surname_cyrillic"] = surname
            context["name_cyrillic"] = name
            context["patronym_cyrillic"] = patronym
            context["full_name"] = surname_and_name(value)
            user.dialog_state = "PROFILE_NEEDS_PASS"
            return BotResponse(
                text=f"Нужен ли вам пропуск для входа на место проведения игры ({VENUE_ADDRESS})?",
                buttons=profile_yes_no_buttons(user),
            )
        if state == "PROFILE_NEEDS_PASS":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=profile_yes_no_buttons(user))
            context["needs_pass"] = value == "Да"
            if value == "Да":
                user.dialog_state = "PROFILE_PASS_FOREIGNER"
                return BotResponse(
                    text="Есть ли у вас российское гражданство?",
                    buttons=profile_yes_no_buttons(user),
                )
            return self._profile_contact_prompt(user)
        if state == "PROFILE_CONTACT":
            if isinstance(user, TelegramUser):
                try:
                    context["contact"] = normalize_vk_url(value)
                except ValueError as exc:
                    return BotResponse(
                        text=f"❌ {exc}. Попробуйте ещё раз:",
                        buttons=[profile_dialog_exit_button(user)],
                    )
            else:
                if value == SKIP:
                    context["contact"] = None
                else:
                    contact = _normalize_vk_telegram_contact(value)
                    if contact is None:
                        return BotResponse(
                            text="Не удалось разобрать профиль Telegram. Попробуйте ещё раз",
                            buttons=[Button(label=SKIP, value=SKIP), profile_dialog_exit_button(user)],
                        )
                    context["contact"] = contact
            user.dialog_state = "PROFILE_CROSSPLAY"
            return BotResponse(
                text="Готовы ли вы играть за персонажа противоположного пола?",
                buttons=profile_yes_no_buttons(user),
            )
        if state == "PROFILE_CROSSPLAY":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=profile_yes_no_buttons(user))
            context["crossplay"] = value == "Да"
            return await self._finish_profile(user, message)
        if state == "PROFILE_PASS_FOREIGNER":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=profile_yes_no_buttons(user))
            context["foreigner"] = value == "Нет"
            if context["foreigner"]:
                user.dialog_state = "PROFILE_PASS_FULL_NAME_LATIN"
                return BotResponse(
                    text=LATIN_FULL_NAME_PROMPT,
                    buttons=[profile_dialog_exit_button(user)],
                )
            user.dialog_state = "PROFILE_PASS_PHONE"
            return BotResponse(text=PHONE_PROMPT, buttons=[profile_dialog_exit_button(user)])
        if state == "PROFILE_PASS_FULL_NAME_LATIN":
            try:
                name_parts = parse_full_name(value, alphabet="latin")
            except ValueError:
                return BotResponse(
                    text=LATIN_FULL_NAME_RETRY_PROMPT,
                    buttons=[profile_dialog_exit_button(user)],
                )
            surname, name = name_parts[:2]
            patronym = name_parts[2] if len(name_parts) == 3 else "-"
            if (context["patronym_cyrillic"] == "-") != (patronym == "-"):
                return BotResponse(
                    text=LATIN_FULL_NAME_RETRY_PROMPT,
                    buttons=[profile_dialog_exit_button(user)],
                )
            context["surname_latin"] = surname
            context["name_latin"] = name
            context["patronym_latin"] = patronym
            user.dialog_state = "PROFILE_PASS_PHONE"
            return BotResponse(text=PHONE_PROMPT, buttons=[profile_dialog_exit_button(user)])
        if state == "PROFILE_PASS_PHONE":
            try:
                context["mobile_phone"] = normalize_mobile_phone(value)
            except ValueError:
                return BotResponse(
                    text=f"Некорректный номер. {PHONE_PROMPT}",
                    buttons=[profile_dialog_exit_button(user)],
                )
            user.dialog_state = "PROFILE_PASS_EMAIL_ADDRESS"
            return BotResponse(text=EMAIL_PROMPT, buttons=[profile_dialog_exit_button(user)])
        if state == "PROFILE_PASS_EMAIL_ADDRESS":
            try:
                email = validate_pass_email(value)
            except ValidationError:
                return BotResponse(
                    text=f"Некорректный email. {EMAIL_PROMPT}",
                    buttons=[profile_dialog_exit_button(user)],
                )
            context["email"] = email
            return self._profile_contact_prompt(user)
        raise AssertionError(f"unknown profile state: {state}")

    @staticmethod
    def _pass_details_from_context(context: dict[str, object]) -> PassDetails:
        foreigner = bool(context["foreigner"])
        return PassDetails(
            surname_cyrillic=str(context["surname_cyrillic"]),
            name_cyrillic=str(context["name_cyrillic"]),
            patronym_cyrillic=str(context["patronym_cyrillic"]),
            foreigner=foreigner,
            surname_latin=str(context["surname_latin"]) if foreigner else None,
            name_latin=str(context["name_latin"]) if foreigner else None,
            patronym_latin=str(context["patronym_latin"]) if foreigner else None,
            mobile_phone=str(context["mobile_phone"]),
            email=str(context["email"]),
        )

    async def _complete_profile_edit(
        self,
        user: User,
        message: InboundMessage,
        *,
        previous_name: str | None = None,
    ) -> BotResponse:
        profile_names = {name for name in (previous_name, user.full_name) if name is not None}
        if isinstance(user, TelegramUser):
            await self._refresh_telegram_link(user)
        await self.users.save(user)
        await self.registrations.enqueue_profile_sync(
            message.identity.platform,
            message.identity.platform_user_id,
            ProfileSyncPayload(names=sorted(profile_names)),
            idempotency_key=f"{message.update_id}:{Operation.SYNC_PROFILE.value}",
        )
        await self._clear(user)
        return await self._profile_response(user, prefix=PROFILE_EDITED)

    async def _profile_field_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        context = user.dialog_context
        state = user.dialog_state
        field = str(context["profile_field"])
        if state == "PROFILE_NICKNAME":
            if value == "profile:nickname:clear":
                user.nickname = None
            else:
                try:
                    user.nickname = normalize_nickname(value)
                except ValidationError:
                    return BotResponse(
                        text=f"Никнейм должен быть одной строкой длиной до 100 символов.\n\n{NICKNAME_PROMPT}",
                        buttons=nickname_buttons(user),
                    )
            return await self._complete_profile_edit(user, message)
        if state == "PROFILE_FULL_NAME_CYRILLIC":
            try:
                name_parts = parse_full_name(value, alphabet="cyrillic")
            except ValueError:
                return BotResponse(
                    text=CYRILLIC_FULL_NAME_RETRY_PROMPT,
                    buttons=[profile_dialog_exit_button(user)],
                )
            surname, name = name_parts[:2]
            patronym = name_parts[2] if len(name_parts) == 3 else "-"
            context.update(
                surname_cyrillic=surname,
                name_cyrillic=name,
                patronym_cyrillic=patronym,
                full_name=surname_and_name(value),
            )
            details = user.pass_details
            if details is not None and details.foreigner and (details.patronym_cyrillic == "-") != (patronym == "-"):
                user.dialog_state = "PROFILE_PASS_FULL_NAME_LATIN"
                return BotResponse(text=LATIN_FULL_NAME_PROMPT, buttons=[profile_dialog_exit_button(user)])
            previous_name = user.full_name
            if details is not None:
                updated = details.model_dump()
                updated.update(
                    surname_cyrillic=surname,
                    name_cyrillic=name,
                    patronym_cyrillic=patronym,
                )
                user.pass_details = PassDetails.model_validate(updated)
            user.full_name = surname_and_name(value)
            return await self._complete_profile_edit(user, message, previous_name=previous_name)
        if state == "PROFILE_CONTACT":
            if isinstance(user, TelegramUser):
                try:
                    user.vk_url = normalize_vk_url(value)
                except ValueError as exc:
                    return BotResponse(
                        text=f"❌ {exc}. Попробуйте ещё раз:",
                        buttons=[profile_dialog_exit_button(user)],
                    )
                await self._refresh_telegram_link(user, force_vk_resolution=True)
            else:
                if value == SKIP:
                    user.telegram_handle = None
                else:
                    contact = _normalize_vk_telegram_contact(value)
                    if contact is None:
                        return BotResponse(
                            text="Не удалось разобрать профиль Telegram. Попробуйте ещё раз",
                            buttons=[Button(label=SKIP, value=SKIP), profile_dialog_exit_button(user)],
                        )
                    user.telegram_handle = contact
            return await self._complete_profile_edit(user, message)
        if state == "PROFILE_CROSSPLAY":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=profile_yes_no_buttons(user))
            user.crossplay = value == "Да"
            return await self._complete_profile_edit(user, message)
        if state == "PROFILE_NEEDS_PASS":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=profile_yes_no_buttons(user))
            if value == "Нет":
                user.pass_details = None
                user.needs_pass = False
                return await self._complete_profile_edit(user, message)
            if user.needs_pass and user.pass_details is not None:
                return await self._complete_profile_edit(user, message)
            user.dialog_state = "PROFILE_PASS_FOREIGNER"
            return BotResponse(
                text="Есть ли у вас российское гражданство?",
                buttons=profile_yes_no_buttons(user),
            )
        if state == "PROFILE_PASS_FOREIGNER":
            if value not in {"Да", "Нет"}:
                return BotResponse(text="Выберите «Да» или «Нет».", buttons=profile_yes_no_buttons(user))
            context["foreigner"] = value == "Нет"
            if field == "pass":
                if context["foreigner"]:
                    user.dialog_state = "PROFILE_PASS_FULL_NAME_LATIN"
                    return BotResponse(text=LATIN_FULL_NAME_PROMPT, buttons=[profile_dialog_exit_button(user)])
                user.dialog_state = "PROFILE_PASS_PHONE"
                return BotResponse(text=PHONE_PROMPT, buttons=[profile_dialog_exit_button(user)])
            details = user.pass_details
            assert details is not None
            if context["foreigner"] and not details.foreigner:
                user.dialog_state = "PROFILE_PASS_FULL_NAME_LATIN"
                return BotResponse(text=LATIN_FULL_NAME_PROMPT, buttons=[profile_dialog_exit_button(user)])
            updated = details.model_dump()
            updated.update(
                foreigner=bool(context["foreigner"]),
                surname_latin=details.surname_latin if context["foreigner"] else None,
                name_latin=details.name_latin if context["foreigner"] else None,
                patronym_latin=details.patronym_latin if context["foreigner"] else None,
            )
            user.pass_details = PassDetails.model_validate(updated)
            return await self._complete_profile_edit(user, message)
        if state == "PROFILE_PASS_FULL_NAME_LATIN":
            try:
                name_parts = parse_full_name(value, alphabet="latin")
            except ValueError:
                return BotResponse(text=LATIN_FULL_NAME_RETRY_PROMPT, buttons=[profile_dialog_exit_button(user)])
            surname, name = name_parts[:2]
            patronym = name_parts[2] if len(name_parts) == 3 else "-"
            if (context["patronym_cyrillic"] == "-") != (patronym == "-"):
                return BotResponse(text=LATIN_FULL_NAME_RETRY_PROMPT, buttons=[profile_dialog_exit_button(user)])
            context.update(
                surname_latin=surname,
                name_latin=name,
                patronym_latin=patronym,
                foreigner=True,
            )
            if field == "pass":
                user.dialog_state = "PROFILE_PASS_PHONE"
                return BotResponse(text=PHONE_PROMPT, buttons=[profile_dialog_exit_button(user)])
            details = user.pass_details
            assert details is not None
            updated = details.model_dump()
            updated.update(
                surname_cyrillic=str(context["surname_cyrillic"]),
                name_cyrillic=str(context["name_cyrillic"]),
                patronym_cyrillic=str(context["patronym_cyrillic"]),
                foreigner=True,
                surname_latin=surname,
                name_latin=name,
                patronym_latin=patronym,
            )
            previous_name = user.full_name
            user.pass_details = PassDetails.model_validate(updated)
            if field == "name":
                user.full_name = str(context["full_name"])
            return await self._complete_profile_edit(
                user,
                message,
                previous_name=previous_name if field == "name" else None,
            )
        if state == "PROFILE_PASS_PHONE":
            try:
                phone = normalize_mobile_phone(value)
            except ValueError:
                return BotResponse(
                    text=f"Некорректный номер. {PHONE_PROMPT}",
                    buttons=[profile_dialog_exit_button(user)],
                )
            context["mobile_phone"] = phone
            if field == "pass":
                user.dialog_state = "PROFILE_PASS_EMAIL_ADDRESS"
                return BotResponse(text=EMAIL_PROMPT, buttons=[profile_dialog_exit_button(user)])
            details = user.pass_details
            assert details is not None
            user.pass_details = PassDetails.model_validate({**details.model_dump(), "mobile_phone": phone})
            return await self._complete_profile_edit(user, message)
        if state == "PROFILE_PASS_EMAIL_ADDRESS":
            try:
                email = validate_pass_email(value)
            except ValidationError:
                return BotResponse(
                    text=f"Некорректный email. {EMAIL_PROMPT}",
                    buttons=[profile_dialog_exit_button(user)],
                )
            context["email"] = email
            if field == "pass":
                user.needs_pass = True
                user.pass_details = self._pass_details_from_context(context)
            else:
                details = user.pass_details
                assert details is not None
                user.pass_details = PassDetails.model_validate({**details.model_dump(), "email": email})
            return await self._complete_profile_edit(user, message)
        raise AssertionError(f"unknown profile edit state: {state}")

    async def _finish_profile(self, user: User, message: InboundMessage) -> BotResponse:
        context = user.dialog_context
        signup_event_id = context.get("signup_event_id")
        full_name = str(context["full_name"])
        profile_names = {full_name}
        if user.full_name is not None:
            profile_names.add(user.full_name)
        user.full_name = full_name
        user.crossplay = bool(context["crossplay"])
        needs_pass = bool(context["needs_pass"])
        if needs_pass:
            user.needs_pass = True
            user.pass_details = self._pass_details_from_context(context)
        else:
            user.pass_details = None
            user.needs_pass = False
        if isinstance(user, TelegramUser):
            user.vk_url = str(context["contact"])
            await self._refresh_telegram_link(user, force_vk_resolution=True)
        else:
            contact = context.get("contact")
            user.telegram_handle = None if contact is None else str(contact)
        # Persist the completed profile before handing slow external lookups and
        # per-game projections to the worker. Keep the current profile state until
        # the durable command is published so a failed request can retry safely.
        await self.users.save(user)
        await self.registrations.enqueue_profile_sync(
            message.identity.platform,
            message.identity.platform_user_id,
            ProfileSyncPayload(names=sorted(profile_names)),
            idempotency_key=f"{message.update_id}:{Operation.SYNC_PROFILE.value}",
        )
        editing_existing = context.get("profile_flow") == "reset"
        await self._clear(user)
        if editing_existing:
            return await self._profile_response(user, prefix=PROFILE_EDITED)
        if isinstance(signup_event_id, str):
            response = await self._select_event(user, message, f"select:enlist:{signup_event_id}")
            response.text = f"✅ Профиль успешно заполнен.\n\n{response.text}"
            return response
        return BotResponse(
            text="✅ Профиль успешно заполнен.",
            buttons=[main_menu_button()],
        )
