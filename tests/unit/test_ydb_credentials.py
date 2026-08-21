from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from larp_bot.adapters.ydb.repositories import YdbExecutor


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
