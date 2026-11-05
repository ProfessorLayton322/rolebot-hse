"""Optional registration question: creation, renaming and player answer changes."""

from __future__ import annotations

from pydantic import ValidationError

from larp_bot.domain.models import (
    MAX_PLAYER_AMOUNT,
    AnswerOption,
    AnswerPayload,
    AttendanceStatus,
    BotResponse,
    Button,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    QuestionPayload,
    RegistrationQuestion,
    ReplyContext,
    User,
)
from larp_bot.domain.registration_queues import answer_text

from .conversation_context import ConversationContext
from .conversation_support import CANCEL_DIALOG, edit_game_button
from .navigation import main_menu_button


class QuestionConversationMixin(ConversationContext):
    @staticmethod
    def _creation_question_prompt(user: User) -> BotResponse:
        user.dialog_state = "ADMIN_QUESTION_CREATE_CHOICE"
        return BotResponse(
            text="Добавить дополнительный вопрос с выбором из двух ответов при регистрации?",
            buttons=[Button(label="Да", value="rq:create:yes"), Button(label="Нет", value="rq:create:no")],
        )

    async def _question_creation_step(self, user: User, value: str) -> BotResponse:
        context = user.dialog_context
        if user.dialog_state == "ADMIN_QUESTION_CREATE_CHOICE":
            if value == "rq:create:no":
                user.dialog_state = "ADMIN_CREATE_PLAYER_AMOUNT"
                return BotResponse(text=f"Введите количество игроков (от 1 до {MAX_PLAYER_AMOUNT}):")
            if value != "rq:create:yes":
                return self._creation_question_prompt(user)
            user.dialog_state = "ADMIN_QUESTION_CREATE_TEXT"
            return BotResponse(text="Введите текст дополнительного вопроса (до 500 символов):")
        if user.dialog_state == "ADMIN_QUESTION_CREATE_TEXT":
            if not value.strip() or len(value.strip()) > 500:
                return BotResponse(text="Введите непустой текст вопроса, не длиннее 500 символов:")
            context["question_text"] = value.strip()
            context["question_options"] = []
            user.dialog_state = "ADMIN_QUESTION_CREATE_NAME"
            return BotResponse(text="Введите название первого ответа (до 31 символа, оно станет названием листа):")
        options = [AnswerOption.model_validate(item) for item in context.get("question_options", [])]
        if user.dialog_state == "ADMIN_QUESTION_CREATE_NAME":
            try:
                option = AnswerOption(option_id=str(len(options) + 1), name=value.strip(), player_amount=1)
                if any(existing.name.casefold() == option.name.casefold() for existing in options):
                    raise ValueError("Названия ответов должны различаться")
            except ValueError:
                return BotResponse(
                    text=(
                        "Введите отличающееся название: 1–31 символ, без \\ / * ? : [ ], "
                        "управляющих символов и крайних апострофов. Название History недоступно."
                    )
                )
            context["question_option_name"] = option.name
            user.dialog_state = "ADMIN_QUESTION_CREATE_CAPACITY"
            maximum = MAX_PLAYER_AMOUNT - sum(o.player_amount for o in options) - (1 if not options else 0)
            return BotResponse(
                text=(f"Сколько основных мест для ответа «{option.name}»? Введите целое число от 1 до {maximum}:")
            )
        remaining = MAX_PLAYER_AMOUNT - sum(o.player_amount for o in options)
        # Leave at least one slot for every answer still to be collected.
        maximum = remaining - (1 if not options else 0)
        if not value.isdecimal() or not 1 <= int(value) <= maximum:
            return BotResponse(text=f"Введите целое число от 1 до {maximum}:")
        options.append(
            AnswerOption(
                option_id=str(len(options) + 1), name=str(context["question_option_name"]), player_amount=int(value)
            )
        )
        context["question_options"] = [option.model_dump(mode="json") for option in options]
        if len(options) < 2:
            user.dialog_state = "ADMIN_QUESTION_CREATE_NAME"
            return BotResponse(text="Введите название второго ответа (до 31 символа):")
        question = RegistrationQuestion(text=str(context["question_text"]), options=options)
        context["registration_question"] = question.model_dump(mode="json")
        context["player_amount"] = sum(option.player_amount for option in options)
        if await self._is_admin(user):
            return self._creation_master_question(user)
        return self._creation_schedule_question(user, user)

    @staticmethod
    def _question_edit_menu(user: User, event: Event) -> BotResponse:
        question = event.registration_question
        if question is None:
            return BotResponse(
                text="В этой игре нет дополнительного вопроса.", buttons=[edit_game_button(event.event_id)]
            )
        user.dialog_state = "ADMIN_QUESTION_EDIT_MENU"
        user.dialog_context = {"event_id": event.event_id}
        return BotResponse(
            text=f"Дополнительный вопрос: {question.text}\n\n"
            + "\n".join(f"{option.name}: лимит {option.player_amount}" for option in question.options),
            buttons=[
                Button(label="Изменить текст вопроса", value="rq:edit:text"),
                *[
                    Button(label=f"Изменить ответ «{option.name}»", value=f"rq:edit:name:{option.option_id}")
                    for option in question.options
                ],
                edit_game_button(event.event_id),
            ],
        )

    async def _question_edit_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        event = await self.events.get(str(user.dialog_context["event_id"]))
        if event is None:
            await self._clear(user)
            return BotResponse(text="Игра не найдена.")
        denied = await self._event_leader_required(user, event.event_id)
        if denied is not None:
            await self._clear(user)
            return denied
        if event.archived_at is not None or event.registration_question is None:
            await self._clear(user)
            return BotResponse(text="Дополнительный вопрос недоступен для изменения.")
        if user.dialog_state == "ADMIN_QUESTION_EDIT_MENU":
            if value == "rq:edit:text":
                user.dialog_context["question_field"] = "text"
                prompt = "Введите новый текст вопроса (до 500 символов):"
            elif value.startswith("rq:edit:name:") and event.registration_question.option(
                value.removeprefix("rq:edit:name:")
            ):
                user.dialog_context["question_field"] = value.removeprefix("rq:edit:name:")
                prompt = "Введите новое название ответа (до 31 символа). Игроки и их места сохранятся:"
            else:
                return self._question_edit_menu(user, event)
            user.dialog_state = "ADMIN_QUESTION_EDIT_VALUE"
            return BotResponse(text=prompt, buttons=[edit_game_button(event.event_id)])
        data = event.registration_question.model_dump()
        field = str(user.dialog_context["question_field"])
        if field == "text":
            data["text"] = value.strip()
        else:
            for option in data["options"]:
                if option["option_id"] == field:
                    option["name"] = value.strip()
        try:
            question = RegistrationQuestion.model_validate(data)
        except ValidationError:
            return BotResponse(
                text=(
                    "Некорректный текст. Вопрос: 1–500 символов. Название ответа: 1–31 символ, "
                    "без \\ / * ? : [ ], управляющих символов и крайних апострофов. "
                    "Ответы должны различаться; History недоступно."
                ),
                buttons=[edit_game_button(event.event_id)],
            )
        await self.registrations.enqueue(
            operation=Operation.UPDATE_QUESTION,
            event_id=event.event_id,
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=QuestionPayload(question=question),
            reply_context=ReplyContext(
                chat_id=message.chat_id, peer_id=message.peer_id, buttons=[edit_game_button(event.event_id)]
            ),
            idempotency_key=f"{message.update_id}:UPDATE_QUESTION",
        )
        await self._clear(user)
        return BotResponse(text="⏳ Изменение принято в обработку.", deferred=True, command_enqueued=True)

    @staticmethod
    def _answer_prompt(event: Event, *, current: str = "", changing: bool = False) -> BotResponse:
        assert event.registration_question is not None
        warning = (
            "\n\nПри смене ответа вы перейдёте в конец очереди нового ответа и можете попасть в запас."
            if changing
            else ""
        )
        return BotResponse(
            text=f"{current or event.registration_question.text}{warning}",
            buttons=[
                *[
                    Button(label=option.name, value=f"rq:pick:{event.event_id}:{option.option_id}")
                    for option in event.registration_question.options
                ],
                Button(label="Отмена", value=CANCEL_DIALOG),
            ],
        )

    async def _start_answer_change(self, user: User, message: InboundMessage, event: Event) -> BotResponse:
        registration = await self.registrations.get_registration(
            event.event_id, message.identity.platform, message.identity.platform_user_id
        )
        if (
            event.archived_at is not None
            or event.status is EventStatus.CLOSED
            or event.registration_question is None
            or registration is None
            or registration.attendance_status is AttendanceStatus.CANCELLED
        ):
            return BotResponse(text="Изменение ответа для этой игры недоступно.")
        user.dialog_state = "ANSWER_EDIT"
        user.dialog_context = {"event_id": event.event_id}
        return self._answer_prompt(event, current=answer_text(event, registration), changing=True)

    async def _answer_step(self, user: User, message: InboundMessage, value: str) -> BotResponse:
        event = await self.events.get(str(user.dialog_context["event_id"]))
        if event is None or event.registration_question is None:
            await self._clear(user)
            return BotResponse(text="Дополнительный вопрос недоступен.")
        prefix = f"rq:pick:{event.event_id}:"
        option = (
            event.registration_question.option(value.removeprefix(prefix))
            if value.startswith(prefix) and message.callback
            else None
        )
        if option is None:
            return self._answer_prompt(event, changing=user.dialog_state == "ANSWER_EDIT")
        if user.dialog_state == "ENLIST_ANSWER":
            user.dialog_context["answer_id"] = option.option_id
            return await self._enqueue_enlist(user, message)
        await self.registrations.enqueue(
            operation=Operation.UPDATE_ANSWER,
            event_id=event.event_id,
            platform=message.identity.platform,
            user_id=message.identity.platform_user_id,
            payload=AnswerPayload(answer_id=option.option_id),
            reply_context=ReplyContext(chat_id=message.chat_id, peer_id=message.peer_id, buttons=[main_menu_button()]),
            idempotency_key=f"{message.update_id}:UPDATE_ANSWER",
        )
        await self._clear(user)
        return BotResponse(text="⏳ Изменение принято в обработку.", deferred=True, command_enqueued=True)
