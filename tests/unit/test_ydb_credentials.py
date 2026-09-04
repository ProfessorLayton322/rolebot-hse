from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from larp_bot.adapters.ydb.repositories import YdbExecutor, YdbUserRepository, _dt, _optional_pass
from larp_bot.domain.models import PassDetails, Platform, TelegramUser, VkUser


def test_ydb_uses_function_context_token_instead_of_metadata() -> None:
    credentials = object()
    driver = MagicMock()

    with (
        patch("larp_bot.adapters.ydb.repositories.ydb.AccessTokenCredentials", return_value=credentials) as access,
        patch(
            "larp_bot.adapters.ydb.repositories.ydb.iam.MetadataUrlCredentials",
            side_effect=AssertionError("metadata credentials must not be used"),
        ),
        patch("larp_bot.adapters.ydb.repositories.ydb.Driver", return_value=driver) as driver_factory,
        patch("larp_bot.adapters.ydb.repositories.ydb.SessionPool"),
    ):
        YdbExecutor("grpcs://ydb.example:2135", "/ru-central1/example", iam_token="context-token")

    access.assert_called_once_with("context-token")
    driver_factory.assert_called_once_with(
        endpoint="grpcs://ydb.example:2135",
        database="/ru-central1/example",
        credentials=credentials,
    )
    driver.wait.assert_called_once_with(timeout=10, fail_fast=True)


@pytest.mark.asyncio
async def test_parameterized_query_is_prepared_before_execution() -> None:
    executor = YdbExecutor.__new__(YdbExecutor)
    executor.pool = MagicMock()
    session = MagicMock()
    transaction = session.transaction.return_value
    prepared = object()
    session.prepare.return_value = prepared
    transaction.execute.return_value = []
    executor.pool.retry_operation_sync.side_effect = lambda operation: operation(session)
    query = "DECLARE $user_id AS Uint64; SELECT $user_id;"
    params = {"$user_id": 42}

    result = await executor.query(query, params)

    assert result == []
    session.prepare.assert_called_once_with(query)
    transaction.execute.assert_called_once_with(prepared, params, commit_tx=True)


@pytest.mark.asyncio
async def test_delivery_claim_prepares_both_transaction_queries() -> None:
    executor = YdbExecutor.__new__(YdbExecutor)
    executor.pool = MagicMock()
    session = MagicMock()
    transaction = session.transaction.return_value.begin.return_value
    prepared_select = object()
    prepared_update = object()
    session.prepare.side_effect = [prepared_select, prepared_update]
    transaction.execute.side_effect = [[SimpleNamespace(rows=[])], []]
    executor.pool.retry_operation_sync.side_effect = lambda operation: operation(session)

    claimed = await executor.claim_delivery(
        table="tg_users",
        id_column="tg_id",
        user_id=42,
        operation_id="operation-1",
    )

    assert claimed is True
    assert session.prepare.call_count == 2
    transaction.execute.assert_any_call(prepared_select, {"$user_id": 42})
    transaction.execute.assert_any_call(
        prepared_update,
        {"$user_id": 42, "$operation_id": "operation-1"},
        commit_tx=True,
    )


def test_ydb_timestamp_microseconds_are_decoded_as_utc() -> None:
    assert _dt(1_787_336_355_988_401) == datetime(2026, 8, 21, 18, 19, 15, 988401, tzinfo=UTC)


@pytest.mark.asyncio
async def test_user_repository_decodes_optional_ydb_timestamp() -> None:
    raw_timestamp = 1_787_336_355_988_401
    executor = SimpleNamespace(
        query=AsyncMock(
            return_value=[
                {
                    "tg_id": 42,
                    "vk_url": None,
                    "full_name": None,
                    "crossplay": None,
                    "larp_experience": None,
                    "needs_pass": None,
                    "pass_details_json": None,
                    "dialog_state": "IDLE",
                    "dialog_context_json": "{}",
                    "last_update_id": "update-1",
                    "last_update_at": raw_timestamp,
                    "last_delivery_operation_id": None,
                    "created_at": raw_timestamp,
                    "updated_at": raw_timestamp,
                }
            ]
        )
    )

    user = await YdbUserRepository(executor).get(Platform.TELEGRAM, 42)

    assert user is not None
    assert user.last_update_at == _dt(raw_timestamp)
    assert user.created_at == _dt(raw_timestamp)
    assert user.updated_at == _dt(raw_timestamp)


@pytest.mark.asyncio
async def test_user_repository_lists_both_platforms_for_notification_resolution() -> None:
    timestamp = datetime(2026, 9, 4, tzinfo=UTC)
    common = {
        "full_name": None,
        "crossplay": None,
        "larp_experience": None,
        "needs_pass": None,
        "pass_details_json": None,
        "dialog_state": "IDLE",
        "dialog_context_json": "{}",
        "last_update_id": None,
        "last_update_at": None,
        "last_delivery_operation_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    executor = SimpleNamespace(
        query=AsyncMock(
            side_effect=[
                [{"tg_id": 42, "vk_url": None, **common}],
                [{"vk_id": 84, "telegram_handle": None, **common}],
            ]
        )
    )

    users = await YdbUserRepository(executor).list_all()

    assert len(users) == 2
    assert isinstance(users[0], TelegramUser) and users[0].tg_id == 42
    assert isinstance(users[1], VkUser) and users[1].vk_id == 84


def test_legacy_pass_json_makes_profile_incomplete_until_refilled() -> None:
    assert (
        _optional_pass(
            '{"legal_name_cyrillic":"Иванов Иван",'
            '"legal_name_latin":"Ivanov Ivan","email":"ivan@example.com","russian_citizen":true}'
        )
        is None
    )


@pytest.mark.asyncio
async def test_user_repository_persists_all_pass_identity_fields_in_ydb() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))
    details = PassDetails(
        surname_cyrillic="Ли",
        name_cyrillic="Анна",
        patronym_cyrillic="-",
        foreigner=True,
        surname_latin="Li",
        name_latin="Anna",
        patronym_latin="-",
        mobile_phone="+44 7700 900123",
        email="anna@example.com",
    )
    user = TelegramUser(
        tg_id=42,
        vk_url="https://vk.com/anna",
        full_name="Анна Ли",
        crossplay=False,
        larp_experience=True,
        needs_pass=True,
        pass_details=details,
    )

    await YdbUserRepository(executor).save(user)

    params = executor.query.await_args.args[1]
    assert params["$pass_details"] == details.model_dump_json()
