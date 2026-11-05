from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from larp_bot.adapters.aws.dynamodb import DynamoEventRepository, DynamoRegistrationRepository
from larp_bot.adapters.memory import (
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
    StaticAdminProvider,
)
from larp_bot.adapters.telegram import telegram_inline_payload
from larp_bot.adapters.telegram.callbacks import decode_callback
from larp_bot.adapters.transports.deferred import CloudflareTelegramEgress
from larp_bot.adapters.yandex_disk.repository import (
    PUBLIC_HEADERS,
    YandexDiskShowcaseRepository,
    public_showcase_workbook_bytes,
    showcase_workbook_bytes,
)
from larp_bot.application.conversation_admin_games import AdministrationGamesMixin
from larp_bot.application.conversation_players import PlayerConversationMixin
from larp_bot.application.conversation_questions import QuestionConversationMixin
from larp_bot.application.notification_helpers import _chat_link_recipient_keys
from larp_bot.application.services import OperationNotAllowed, OrderedMutationService, RegistrationCatalog
from larp_bot.domain.models import (
    AnswerOption,
    AnswerPayload,
    AttendanceStatus,
    BotIdentity,
    EmptyPayload,
    EnlistPayload,
    Event,
    EventStatus,
    InboundMessage,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    QuestionPayload,
    Registration,
    RegistrationQuestion,
    TelegramUser,
    VkUser,
)
from larp_bot.domain.registration_queues import answer_text, main_roster, queue_position, reserve_roster
from larp_bot.domain.security import participant_key
from tests.conftest import MemoryDiskStore
from tests.unit.test_conversation import DeferredMemoryDiskStore, engine_setup, inbound
from tests.unit.test_dynamodb_repositories import MemoryDynamoClient
from tests.unit.test_linked_identity import SECRET, save_linked


@pytest.fixture
def question() -> RegistrationQuestion:
    return RegistrationQuestion(
        text="За каким столом хотите играть?",
        options=[
            AnswerOption(option_id="a", name="Первый стол", player_amount=1),
            AnswerOption(option_id="b", name="Второй стол", player_amount=2),
        ],
    )


@pytest.fixture
def choice_event(event: Event, question: RegistrationQuestion) -> Event:
    return event.model_copy(
        update={
            "event_id": "01234567-89ab-cdef-0123-456789abcdef",
            "registration_question": question,
            "player_amount": 3,
            "public_table_resource_path": "disk:/larp-bot/events/public.xlsx",
            "public_table_public_url": "https://drive.example/public",
        }
    )


def row(event: Event, key: str, answer: str, minute: int, *, confirmed: bool = False) -> Registration:
    return Registration(
        event_id=event.event_id,
        participant_key=key * 43,
        display_name=f"Иванов {key}",
        wish_play="Без пожеланий",
        answer_id=answer,
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
        answer_queue_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
        attendance_status=AttendanceStatus.CONFIRMED if confirmed else AttendanceStatus.WAITING,
    )


def command(event: Event, operation: Operation, payload: object, key: str = "a" * 43) -> OrderedRegistrationCommand:
    return OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=operation,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key=key,
        payload=payload,
    )


async def mutation_setup(store: MemoryDiskStore, event: Event, rows: list[Registration]):
    showcase = YandexDiskShowcaseRepository(store)
    await showcase.create_event_workbook(event.disk_resource_path, question=event.registration_question)
    await showcase.create_public_event_workbook(event.public_table_resource_path, question=event.registration_question)
    events = MemoryEventRepository([event])
    registrations = MemoryRegistrationRepository(rows)
    catalog = RegistrationCatalog(events, registrations, showcase)
    service = OrderedMutationService(events, catalog, admins=StaticAdminProvider(tg_ids={1}))
    return service, events, registrations


@pytest.mark.parametrize(
    "name", [" ", "x" * 32, "a/b", "a:b", "a?b", "a*b", "a[b]", "a\\b", "'a", "a'", "History", "a\nb"]
)
def test_invalid_answer_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        AnswerOption(option_id="a", name=name, player_amount=1)


def test_options_have_distinct_ids_names_and_bounded_capacity(question: RegistrationQuestion) -> None:
    for update in [{"option_id": "a"}, {"name": "ПЕРВЫЙ СТОЛ"}, {"player_amount": 10000}]:
        data = question.model_dump()
        data["options"][1].update(update)
        with pytest.raises(ValidationError):
            RegistrationQuestion.model_validate(data)
    expanded = question.model_copy(deep=True)
    expanded.options.append(AnswerOption(option_id="c", name="Третий стол", player_amount=1))
    assert len(RegistrationQuestion.model_validate(expanded.model_dump()).options) == 3


def test_each_answer_has_independent_main_and_reserve(choice_event: Event) -> None:
    rows = [
        row(choice_event, "a", "a", 1, confirmed=True),
        row(choice_event, "b", "a", 2, confirmed=True),
        row(choice_event, "c", "b", 3, confirmed=True),
    ]
    assert [r.participant_key for r in main_roster(choice_event, rows)] == ["a" * 43, "c" * 43]
    assert [r.participant_key for r in reserve_roster(choice_event, rows)] == ["b" * 43]
    assert _chat_link_recipient_keys(rows, choice_event) == {"a" * 43, "c" * 43}
    assert queue_position(choice_event, rows, rows[2]) == 1


@pytest.mark.parametrize("render", [showcase_workbook_bytes, public_showcase_workbook_bytes])
def test_workbooks_partition_rows_keep_styles_and_hide_contacts(choice_event: Event, render) -> None:
    rows = [row(choice_event, "a", "a", 1), row(choice_event, "b", "a", 2), row(choice_event, "c", "b", 3)]
    rows[0].wish_play = rows[2].display_name
    rows[2].wish_play = rows[0].display_name
    rows[0].vk_profile = "https://vk.com/id123"
    workbook = load_workbook(BytesIO(render(rows, question=choice_event.registration_question)))
    assert workbook.sheetnames == ["Первый стол", "Второй стол"]
    assert workbook.worksheets[0]["B2"].value == "a Иванов"
    assert workbook.worksheets[0]["A3"].value == "ЗАПАС"
    assert workbook.worksheets[0]["B4"].value == "b Иванов"
    assert workbook.worksheets[1]["B2"].value == "c Иванов"
    assert workbook.worksheets[1]["A4"].value == "ЗАПАС"
    assert workbook.worksheets[0]["B2"].fill.fgColor.rgb == workbook.worksheets[1]["B2"].fill.fgColor.rgb
    if render is public_showcase_workbook_bytes:
        assert tuple(cell.value for cell in workbook.worksheets[0][1]) == PUBLIC_HEADERS
        assert all(cell.value != rows[0].vk_profile for sheet in workbook for line in sheet for cell in line)
    workbook.close()


async def test_switch_moves_to_target_tail_promotes_source_and_survives_retry(disk_store, choice_event) -> None:
    rows = [
        row(choice_event, "a", "a", 1),
        row(choice_event, "b", "a", 2, confirmed=True),
        row(choice_event, "c", "b", 3),
        row(choice_event, "d", "b", 4),
    ]
    service, events, registrations = await mutation_setup(disk_store, choice_event, rows)
    change = command(choice_event, Operation.UPDATE_ANSWER, AnswerPayload(answer_id="b"))
    result = await service.apply(change)
    assert "запас, место 3" in result.text
    changed = await registrations.get(choice_event.event_id, "a" * 43)
    assert changed.created_at == rows[0].created_at
    assert changed.answer_id == "b"
    assert changed.answer_queue_at > rows[-1].answer_queue_at
    persisted = await events.get(choice_event.event_id)
    assert persisted.last_reserve_promotion_participant_key == "b" * 43
    assert persisted.last_confirmed_link_promotion_participant_key == "b" * 43
    await service.apply(change)
    retried = await registrations.get(choice_event.event_id, "a" * 43)
    assert retried.answer_queue_at == changed.answer_queue_at
    assert (await events.get(choice_event.event_id)).last_reserve_promotion_participant_key == "b" * 43
    same = command(choice_event, Operation.UPDATE_ANSWER, AnswerPayload(answer_id="b"))
    assert not (await service.apply(same)).notifications_enabled
    assert (await registrations.get(choice_event.event_id, "a" * 43)).answer_queue_at == changed.answer_queue_at


async def test_rename_changes_display_and_both_workbooks_without_moving_rows(disk_store, choice_event) -> None:
    rows = [row(choice_event, "a", "a", 1), row(choice_event, "b", "a", 2)]
    service, events, registrations = await mutation_setup(disk_store, choice_event, rows)
    question = choice_event.registration_question.model_copy(deep=True)
    question.text = "Какой вариант выбираете?"
    question.options[0].name = "Новое название"
    await service.apply(command(choice_event, Operation.UPDATE_QUESTION, QuestionPayload(question=question)))
    current = await events.get(choice_event.event_id)
    assert (await registrations.list_for_event(choice_event.event_id)) == rows
    assert "Ваш ответ: Новое название" in answer_text(current, rows[0])
    for path in [choice_event.disk_resource_path, choice_event.public_table_resource_path]:
        workbook = load_workbook(BytesIO(disk_store.files[path]))
        assert workbook.sheetnames == ["Новое название", "Второй стол"]
        assert workbook.worksheets[0]["B2"].value == "a Иванов"
        assert workbook.worksheets[0]["B4"].value == "b Иванов"
        workbook.close()
    question.options[0].player_amount = 2
    with pytest.raises(OperationNotAllowed, match="только текст"):
        await service.apply(command(choice_event, Operation.UPDATE_QUESTION, QuestionPayload(question=question)))


@pytest.mark.parametrize("answer_id", [None, "missing"])
async def test_signup_requires_existing_answer(disk_store, choice_event, answer_id) -> None:
    service, _, registrations = await mutation_setup(disk_store, choice_event, [])
    with pytest.raises(OperationNotAllowed, match="Выберите ответ"):
        await service.apply(
            command(
                choice_event,
                Operation.ENLIST,
                EnlistPayload(display_name="Иванов Иван", wish_play="Без пожеланий", answer_id=answer_id),
            )
        )
    assert await registrations.list_for_event(choice_event.event_id) == []


async def test_signup_uses_selected_answer_capacity_in_result(disk_store, choice_event) -> None:
    service, _, _ = await mutation_setup(disk_store, choice_event, [row(choice_event, "b", "a", 1)])
    result = await service.apply(
        command(
            choice_event,
            Operation.ENLIST,
            EnlistPayload(display_name="Иванов Иван", wish_play="Без пожеланий", answer_id="a"),
        )
    )
    assert "Сейчас вы в запасе" in result.text
    assert "Ваш ответ: Первый стол" in result.text


async def test_dynamodb_persists_question_and_atomic_linked_answer_updates(choice_event) -> None:
    client = MemoryDynamoClient()
    events = DynamoEventRepository(client, events_table="events", leaders_table="leaders")
    registrations = DynamoRegistrationRepository(client, registrations_table="registrations")
    await events.create(choice_event, [])
    rows = [row(choice_event, "a", "a", 1), row(choice_event, "b", "a", 2), row(choice_event, "c", "b", 3)]
    await registrations.import_missing(rows)
    operation_id = str(uuid4())
    client.fail_next_transaction = True
    await registrations.update_answer(
        choice_event.event_id, operation_id=operation_id, participant_keys=["a" * 43, "b" * 43], answer_id="b"
    )
    stored = await registrations.list_for_event(choice_event.event_id)
    linked = [r for r in stored if r.participant_key in {"a" * 43, "b" * 43}]
    assert {r.answer_id for r in linked} == {"b"}
    assert linked[0].answer_queue_at == linked[1].answer_queue_at
    assert linked[0].answer_queue_at > rows[-1].answer_queue_at
    assert not await registrations.update_answer(
        choice_event.event_id, operation_id=operation_id, participant_keys=["a" * 43, "b" * 43], answer_id="b"
    )
    question = choice_event.registration_question.model_copy(deep=True)
    question.options[0].name = "Новое название"
    await events.set_registration_question(choice_event.event_id, question)
    assert (await events.get(choice_event.event_id)).registration_question == question


@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.VK])
async def test_player_must_answer_can_cancel_and_can_change_from_game_page(disk_store, choice_event, platform) -> None:
    engine, users, publisher, registrations = await engine_setup(disk_store, choice_event)
    await users.save(VkUser(vk_id=1, full_name="Иванов Иван", crossplay=True, larp_experience=True, needs_pass=False))

    def message(number, value, callback=False):
        return InboundMessage(
            identity=BotIdentity(platform=platform, platform_user_id=1),
            update_id=str(number),
            text="" if callback else value,
            callback=value if callback else None,
            chat_id=1,
            peer_id=1,
        )

    await engine.handle(message(1, f"select:enlist:{choice_event.event_id}", True))
    prompt = await engine.handle(message(2, "enlist:wish-play:skip", True))
    assert prompt.text == choice_event.registration_question.text
    assert len(prompt.buttons) == 3
    assert publisher.commands == []
    await engine.handle(message(3, prompt.buttons[-1].value, True))
    assert publisher.commands == []
    await engine.handle(message(4, f"select:enlist:{choice_event.event_id}", True))
    await engine.handle(message(5, "enlist:wish-play:skip", True))
    await engine.handle(message(6, f"rq:pick:{choice_event.event_id}:a", True))
    assert publisher.commands[-1].payload.answer_id == "a"
    key = engine.registrations.key(platform, 1, choice_event.event_id)
    registration = row(choice_event, "a", "a", 1).model_copy(update={"participant_key": key})
    await registrations.import_missing([registration])
    prompt = await engine.handle(message(7, f"select:answer:{choice_event.event_id}", True))
    assert "Ваш ответ: Первый стол" in prompt.text
    assert "конец очереди" in prompt.text
    await engine.handle(message(8, f"rq:pick:{choice_event.event_id}:b", True))
    assert publisher.commands[-1].operation is Operation.UPDATE_ANSWER
    assert publisher.commands[-1].payload.answer_id == "b"


async def test_creation_collects_two_answers_and_individual_limits(disk_store, event) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event, admin=True)
    values = [
        ("➕ Создать игру", False),
        ("Новая игра", False),
        ("14.11.40", False),
        ("create:close:cancel", True),
        ("rq:create:yes", True),
        ("За каким столом?", False),
        ("Первый стол", False),
        ("1", False),
        ("Второй стол", False),
        ("2", False),
        ("create:master-self", True),
        ("create:schedule:no", True),
        ("create:confirmation:no", True),
    ]
    for number, (value, callback) in enumerate(values, 1):
        response = await engine.handle(inbound(number, value, callback=callback))
    assert "Игра создана" in response.text
    created = next(e for e in await engine.events.list_page(limit=10) if e.name == "Новая игра")
    assert created.player_amount == 3
    assert [o.player_amount for o in created.registration_question.options] == [1, 2]
    for path in [created.disk_resource_path, created.public_table_resource_path]:
        workbook = load_workbook(BytesIO(disk_store.files[path]))
        assert workbook.sheetnames == ["Первый стол", "Второй стол"]
        workbook.close()


async def test_new_question_buttons_fit_inline_and_deferred_telegram_with_uuid(choice_event) -> None:
    # Maximum ID size and non-ASCII names exercise encoded byte sizes, not character counts.
    choice_event.registration_question.options[0].option_id = "a" * 16
    choice_event.registration_question.options[0].name = "Ответ с кириллицей и 🎭"
    user = TelegramUser(tg_id=1)
    responses = [
        QuestionConversationMixin._answer_prompt(choice_event),
        QuestionConversationMixin._question_edit_menu(user, choice_event),
        QuestionConversationMixin._creation_question_prompt(user),
        AdministrationGamesMixin._edit_game_menu(choice_event),
        PlayerConversationMixin._game_wishes_menu(choice_event),
    ]
    captured = []

    async def handler(request):
        captured.append(json.loads(request.content)["payload"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = CloudflareTelegramEgress("https://egress.example", "secret", client)
        for index, response in enumerate(responses):
            inline = telegram_inline_payload(response, 1)
            await transport.send(
                user_id=1, request_id=f"question-{index}", text=response.text, buttons=response.buttons
            )
            for payload in [inline, captured[-1]]:
                for buttons in payload["reply_markup"]["inline_keyboard"]:
                    for button in buttons:
                        assert 1 <= len(button["callback_data"].encode("utf-8")) <= 64
            callbacks = [b["callback_data"] for line in inline["reply_markup"]["inline_keyboard"] for b in line]
            assert [decode_callback(value) for value in callbacks] == [b.value for b in response.buttons]


async def test_switch_retry_after_failed_table_refresh_preserves_position_and_promotion(
    disk_store,
    choice_event,
    monkeypatch,
) -> None:
    rows = [row(choice_event, "a", "a", 1), row(choice_event, "b", "a", 2), row(choice_event, "c", "b", 3)]
    service, events, registrations = await mutation_setup(disk_store, choice_event, rows)
    refresh = service.catalog.refresh_with_rows

    async def fail_refresh(event):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(service.catalog, "refresh_with_rows", fail_refresh)
    change = command(choice_event, Operation.UPDATE_ANSWER, AnswerPayload(answer_id="b"))
    with pytest.raises(RuntimeError, match="upload failed"):
        await service.apply(change)
    first = await registrations.get(choice_event.event_id, "a" * 43)
    monkeypatch.setattr(service.catalog, "refresh_with_rows", refresh)
    await service.apply(change)
    assert (await registrations.get(choice_event.event_id, "a" * 43)).answer_queue_at == first.answer_queue_at
    assert (await events.get(choice_event.event_id)).last_reserve_promotion_participant_key == "b" * 43


async def test_master_renames_answer_through_menu_and_outsider_is_denied(disk_store, choice_event) -> None:
    engine, _, publisher, _ = await engine_setup(disk_store, choice_event, admin=True)
    menu = await engine.handle(inbound(1, f"ag:question:{choice_event.event_id}", callback=True))
    assert "Первый стол: лимит 1" in menu.text
    await engine.handle(inbound(2, "rq:edit:name:a", callback=True))
    invalid = await engine.handle(inbound(3, "Второй стол"))
    assert "Некорректный текст" in invalid.text
    await engine.handle(inbound(4, "Новое название"))
    assert publisher.commands[-1].operation is Operation.UPDATE_QUESTION
    assert publisher.commands[-1].payload.question.options[0].name == "Новое название"
    denied = await engine.handle(inbound(5, f"ag:question:{choice_event.event_id}", callback=True, user_id=99))
    assert "прав" in denied.text
    assert len(publisher.commands) == 1


async def test_closed_game_rejects_answer_change_at_worker_time(disk_store, choice_event) -> None:
    choice_event.status = EventStatus.CLOSED
    service, _, registrations = await mutation_setup(disk_store, choice_event, [row(choice_event, "a", "a", 1)])
    with pytest.raises(OperationNotAllowed, match="закрыта"):
        await service.apply(command(choice_event, Operation.UPDATE_ANSWER, AnswerPayload(answer_id="b")))
    assert (await registrations.get(choice_event.event_id, "a" * 43)).answer_id == "a"


async def test_sift_and_manual_promotion_stay_within_answer_queues(disk_store, choice_event) -> None:
    rows = [
        row(choice_event, "a", "a", 1),
        row(choice_event, "b", "a", 2, confirmed=True),
        row(choice_event, "c", "b", 3),
        row(choice_event, "d", "b", 4),
        row(choice_event, "e", "b", 5, confirmed=True),
    ]
    service, _, registrations = await mutation_setup(disk_store, choice_event, rows)
    original = {r.participant_key: r.created_at for r in rows}
    await service.apply(command(choice_event, Operation.PROMOTE_RESERVE_PARTICIPANT, EmptyPayload(), "b" * 43))
    current = await registrations.list_for_event(choice_event.event_id)
    assert {r.participant_key for r in main_roster(choice_event, current)} == {"b" * 43, "c" * 43, "d" * 43}
    await service.apply(command(choice_event, Operation.REMOVE_WAITING_PARTICIPANTS, EmptyPayload()))
    current = await registrations.list_for_event(choice_event.event_id)
    assert {r.participant_key for r in main_roster(choice_event, current)} == {"b" * 43, "c" * 43, "e" * 43}
    assert {r.participant_key: r.created_at for r in current} == original
    with pytest.raises(OperationNotAllowed, match="запасе"):
        await service.apply(command(choice_event, Operation.PROMOTE_RESERVE_PARTICIPANT, EmptyPayload(), "z" * 43))


async def test_deferred_creation_keeps_question_in_upload_callback(disk_store, event) -> None:
    engine, _, _, _ = await engine_setup(disk_store, event, admin=True)
    uploader = DeferredMemoryDiskStore()
    engine.administration.showcase = YandexDiskShowcaseRepository(disk_store, uploader)
    values = [
        ("➕ Создать игру", False),
        ("Отложенная загрузка", False),
        ("14.11.40", False),
        ("create:close:cancel", True),
        ("rq:create:yes", True),
        ("Ваш выбор?", False),
        ("Первый", False),
        ("1", False),
        ("Второй", False),
        ("2", False),
        ("create:master-self", True),
        ("create:schedule:no", True),
        ("create:confirmation:no", True),
    ]
    for number, (value, callback) in enumerate(values, 1):
        response = await engine.handle(inbound(number, value, callback=callback))
    assert response.silent
    uploads, callback = uploader.jobs[-1]
    assert callback["event"]["registration_question"]["text"] == "Ваш выбор?"
    assert [option["player_amount"] for option in callback["event"]["registration_question"]["options"]] == [1, 2]
    for upload in uploads:
        workbook = load_workbook(BytesIO(upload.content))
        assert workbook.sheetnames == (["Первый", "Второй"] if upload.role in {"master", "public"} else ["Лист1"])
        workbook.close()


async def test_linked_telegram_player_changes_registration_created_in_vk(disk_store, choice_event) -> None:
    vk_key = participant_key(SECRET, Platform.VK, 22, choice_event.event_id)
    existing = row(choice_event, "a", "a", 1).model_copy(update={"participant_key": vk_key})
    service, _, registrations = await mutation_setup(disk_store, choice_event, [existing])
    users = MemoryUserRepository()
    await save_linked(users)
    service.users = users
    service.participant_secret = SECRET
    change = command(choice_event, Operation.UPDATE_ANSWER, AnswerPayload(answer_id="b"), vk_key)
    change.platform_user_id = 11
    await service.apply(change)
    stored = await registrations.list_for_event(choice_event.event_id)
    assert len(stored) == 1
    assert stored[0].participant_key == vk_key
    assert stored[0].answer_id == "b"
