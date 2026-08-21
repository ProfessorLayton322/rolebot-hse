from __future__ import annotations

from unittest.mock import MagicMock, patch

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
