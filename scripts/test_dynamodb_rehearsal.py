from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer

from larp_bot.adapters.aws.dynamodb import (
    DynamoEventRepository,
    DynamoRegistrationRepository,
    DynamoUserRepository,
)
from larp_bot.domain.models import (
    AttendanceStatus,
    BotIdentity,
    Event,
    EventStatus,
    Platform,
    Registration,
    TelegramUser,
)
from larp_bot.migration.cli import AWS_ACCOUNT_ID, AWS_PROFILE, AWS_REGION, AWS_ROLE_NAME
from larp_bot.migration.dynamodb import DynamoTableNames

_SERIALIZER = TypeSerializer()


def serialized(**values: str) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(value) for key, value in values.items()}


def client() -> Any:
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != AWS_ACCOUNT_ID or f":assumed-role/{AWS_ROLE_NAME}/" not in identity.get("Arn", ""):
        raise RuntimeError("refusing rehearsal outside the fixed AWS account and bootstrap role")
    return session.client("dynamodb")


async def exercise(prefix: str) -> None:
    dynamodb = client()
    tables = DynamoTableNames.from_prefix(prefix)
    suffix = uuid4().hex
    tg_id = 8_000_000_000_000 + int(suffix[:8], 16)
    event_id = f"rehearsal-{suffix}"
    other_event_id = f"rehearsal-other-{suffix}"
    signup_token = f"Rehearsal_{suffix}"
    chat_id = str(-(8_000_000_000_000 + int(suffix[8:16], 16)))
    first_key = "a" * 42 + suffix[0]
    second_key = "b" * 42 + suffix[1]
    replaced_event_id = f"rehearsal-replaced-{suffix}"
    cleanup: list[tuple[str, dict[str, Any]]] = []

    def remember(table: str, key: dict[str, Any]) -> None:
        cleanup.append((table, key))

    try:
        users = DynamoUserRepository(dynamodb, telegram_table=tables.tg_users, vk_table=tables.vk_users)
        await users.save(TelegramUser(tg_id=tg_id, telegram_handle=f"@rehearsal_{suffix}"))
        remember(tables.tg_users, serialized(tg_id=str(tg_id)))
        assert await users.claim_delivery(Platform.TELEGRAM, tg_id, f"delivery-{suffix}")
        assert not await users.claim_delivery(Platform.TELEGRAM, tg_id, f"delivery-{suffix}")

        events = DynamoEventRepository(dynamodb, events_table=tables.events, leaders_table=tables.event_leaders)
        event = Event(
            event_id=event_id,
            name="AWS migration rehearsal",
            disk_resource_path=f"disk:/larp-bot/rehearsal/{suffix}.xlsx",
            public_registration_url=f"https://example.invalid/{suffix}",
            signup_token=signup_token,
            telegram_chat_id=int(chat_id),
            status=EventStatus.CONFIRMATION_OPEN,
        )
        await events.create(event, leaders=[BotIdentity(platform=Platform.TELEGRAM, platform_user_id=tg_id)])
        remember(tables.events, serialized(event_id=event_id))
        remember(tables.events, serialized(event_id=f"lock:signup:{signup_token}"))
        remember(tables.events, serialized(event_id=f"lock:telegram-chat:{chat_id}"))
        remember(tables.event_leaders, serialized(event_id=event_id, leader_key=f"telegram:{tg_id}"))
        assert await events.get_by_signup_token(signup_token) == event

        other = event.model_copy(
            update={
                "event_id": other_event_id,
                "name": "AWS migration rehearsal conflict",
                "signup_token": None,
                "telegram_chat_id": None,
            }
        )
        await events.create(other)
        remember(tables.events, serialized(event_id=other_event_id))
        assert not await events.bind_telegram_chat(other_event_id, int(chat_id))

        registrations = DynamoRegistrationRepository(dynamodb, registrations_table=tables.registrations)
        first_time = datetime.now(UTC) - timedelta(minutes=2)
        second_time = datetime.now(UTC) - timedelta(minutes=1)
        current = [
            Registration(
                event_id=event_id,
                participant_key=first_key,
                display_name="Rehearsal A",
                wish_play="A",
                created_at=first_time,
            ),
            Registration(
                event_id=event_id,
                participant_key=second_key,
                display_name="Rehearsal B",
                wish_play="B",
                created_at=second_time,
            ),
        ]
        replaced = [item.model_copy(update={"event_id": replaced_event_id}) for item in current]
        await registrations.import_missing([*current, *replaced])
        for registration in [*current, *replaced]:
            remember(
                tables.registrations,
                serialized(event_id=registration.event_id, participant_key=registration.participant_key),
            )
        assert await registrations.confirm_linked(
            event_id,
            operation_id=f"confirm-{suffix}",
            participant_keys=[first_key, second_key],
            character_wish="Wizard",
            replaced_event_id=replaced_event_id,
            replaced_participant_keys=[first_key, second_key],
        )
        assert {row.attendance_status for row in await registrations.list_for_event(event_id)} == {
            AttendanceStatus.CONFIRMED
        }
        assert await registrations.swap_queue_positions(
            event_id,
            operation_id=f"swap-{suffix}",
            promoted_participant_key=second_key,
            promoted_created_at=second_time,
            demoted_participant_key=first_key,
            demoted_created_at=first_time,
        )
        assert [row.display_name for row in await registrations.list_for_event(event_id)] == [
            "Rehearsal B",
            "Rehearsal A",
        ]
    finally:
        for table, key in reversed(cleanup):
            dynamodb.delete_item(TableName=table, Key=key)

    print("DynamoDB rehearsal transactions passed and synthetic rows were removed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table-prefix", default="larp-bot-rehearsal")
    args = parser.parse_args()
    if "rehearsal" not in args.table_prefix:
        raise SystemExit("the integration transaction test requires an unmistakable rehearsal prefix")
    asyncio.run(exercise(args.table_prefix))


if __name__ == "__main__":
    main()
