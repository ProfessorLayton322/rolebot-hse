from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from larp_bot.adapters.ymq.client import YmqCommandPublisher
from larp_bot.domain.models import (
    CharacterWishPayload,
    CoPlayerWishPayload,
    EmptyPayload,
    Operation,
    OrderedRegistrationCommand,
    Platform,
)


class RecordingSqs:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


def event_command(event_id: str) -> OrderedRegistrationCommand:
    return OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event_id,
        operation=Operation.CLOSE_EVENT,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        payload=EmptyPayload(),
    )


@pytest.mark.asyncio
async def test_fifo_uses_independent_event_groups_and_operation_deduplication() -> None:
    sqs = RecordingSqs()
    publisher = YmqCommandPublisher(sqs, "fifo-url", "kick-url")
    first = event_command("event-a1")
    second = event_command("event-b1")
    await publisher.publish(first)
    await publisher.publish(second)

    fifo_messages = [message for message in sqs.messages if message["QueueUrl"] == "fifo-url"]
    assert [message["MessageGroupId"] for message in fifo_messages] == ["event-a1", "event-b1"]
    assert [message["MessageDeduplicationId"] for message in fifo_messages] == [
        first.operation_id,
        second.operation_id,
    ]
    assert len([message for message in sqs.messages if message["QueueUrl"] == "kick-url"]) == 2


@pytest.mark.asyncio
async def test_confirmations_for_different_games_share_the_players_fifo_group() -> None:
    sqs = RecordingSqs()
    publisher = YmqCommandPublisher(sqs, "fifo-url", "kick-url")
    commands = [
        OrderedRegistrationCommand(
            operation_id=str(uuid4()),
            event_id=event_id,
            operation=Operation.CONFIRM,
            platform=Platform.TELEGRAM,
            platform_user_id=7,
            participant_key=character * 43,
            payload=CharacterWishPayload(character_wish="Doctor"),
        )
        for event_id, character in (("event-a1", "a"), ("event-b1", "b"))
    ]

    for command in commands:
        await publisher.publish(command)

    fifo_messages = [message for message in sqs.messages if message["QueueUrl"] == "fifo-url"]
    assert [message["MessageGroupId"] for message in fifo_messages] == [
        "confirmation:telegram:7",
        "confirmation:telegram:7",
    ]


def test_co_player_wish_command_round_trips_through_queue_json() -> None:
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id="event-a1",
        operation=Operation.UPDATE_CO_PLAYER_WISH,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key="a" * 43,
        payload=CoPlayerWishPayload(wish_play="Петров Пётр"),
    )

    decoded = OrderedRegistrationCommand.model_validate_json(command.model_dump_json())

    assert isinstance(decoded.payload, CoPlayerWishPayload)
    assert decoded.payload.wish_play == "Петров Пётр"
