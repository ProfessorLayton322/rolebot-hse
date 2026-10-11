from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from larp_bot.adapters.ydb.repositories import (
    YdbEventRepository,
    YdbExecutor,
    YdbUserRepository,
    _dt,
    _optional_pass,
)
from larp_bot.domain.models import BotIdentity, Button, PassDetails, Platform, PrivilegedRole, TelegramUser, VkUser


def test_ydb_uses_function_context_token_instead_of_metadata() -> None:
    credentials = object()
    driver = MagicMock()

    with (
        patch("larp_bot.adapters.ydb.common.ydb.AccessTokenCredentials", return_value=credentials) as access,
        patch(
            "larp_bot.adapters.ydb.common.ydb.iam.MetadataUrlCredentials",
            side_effect=AssertionError("metadata credentials must not be used"),
        ),
        patch("larp_bot.adapters.ydb.common.ydb.Driver", return_value=driver) as driver_factory,
        patch("larp_bot.adapters.ydb.common.ydb.SessionPool"),
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
async def test_read_only_query_uses_snapshot_transaction() -> None:
    executor = YdbExecutor.__new__(YdbExecutor)
    executor.pool = MagicMock()
    session = MagicMock()
    transaction = session.transaction.return_value
    session.prepare.return_value = object()
    transaction.execute.return_value = []
    executor.pool.retry_operation_sync.side_effect = lambda operation: operation(session)

    await executor.query("SELECT 1;", read_only=True)

    mode = session.transaction.call_args.args[0]
    assert mode.name == "snapshot_read_only"


@pytest.mark.asyncio
async def test_multi_table_snapshot_uses_one_transaction_and_commits_last_read() -> None:
    executor = YdbExecutor.__new__(YdbExecutor)
    executor.pool = MagicMock()
    session = MagicMock()
    transaction = session.transaction.return_value.begin.return_value
    session.prepare.side_effect = ["prepared-one", "prepared-two"]
    transaction.execute.side_effect = [
        [SimpleNamespace(rows=[{"id": "one"}])],
        [SimpleNamespace(rows=[{"id": "two"}])],
    ]
    executor.pool.retry_operation_sync.side_effect = lambda operation: operation(session)

    result = await executor.snapshot(["SELECT one;", "SELECT two;"])

    assert result == [[{"id": "one"}], [{"id": "two"}]]
    assert session.transaction.call_args.args[0].name == "snapshot_read_only"
    transaction.execute.assert_any_call("prepared-one", {}, commit_tx=False)
    transaction.execute.assert_any_call("prepared-two", {}, commit_tx=True)


@pytest.mark.asyncio
async def test_mutating_query_keeps_serializable_transaction() -> None:
    executor = YdbExecutor.__new__(YdbExecutor)
    executor.pool = MagicMock()
    session = MagicMock()
    transaction = session.transaction.return_value
    session.prepare.return_value = object()
    transaction.execute.return_value = []
    executor.pool.retry_operation_sync.side_effect = lambda operation: operation(session)

    await executor.query("UPDATE `events` SET name = 'new';")

    mode = session.transaction.call_args.args[0]
    assert mode.name == "serializable_read_write"


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
        buttons=[Button(label="Visible", value="callback:value")],
    )

    assert claimed is True
    assert session.prepare.call_count == 2
    transaction.execute.assert_any_call(prepared_select, {"$user_id": 42})
    transaction.execute.assert_any_call(
        prepared_update,
        {
            "$user_id": 42,
            "$operation_id": "operation-1",
            "$last_bot_buttons": '[{"label":"Visible","value":"callback:value"}]',
        },
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
                    "last_bot_buttons_json": '[{"label":"Visible","value":"callback:value"}]',
                    "created_at": raw_timestamp,
                    "updated_at": raw_timestamp,
                }
            ]
        )
    )

    user = await YdbUserRepository(executor).get(Platform.TELEGRAM, 42)

    assert user is not None
    assert user.last_update_at == _dt(raw_timestamp)
    assert user.last_bot_buttons == [Button(label="Visible", value="callback:value")]
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
        "last_bot_buttons_json": None,
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


@pytest.mark.asyncio
async def test_user_repository_serializes_the_last_bot_keyboard() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))
    user = TelegramUser(
        tg_id=42,
        nickname="Сова",
        last_bot_buttons=[Button(label="Visible", value="callback:value")],
    )

    await YdbUserRepository(executor).save(user)

    query, params = executor.query.await_args.args
    assert "last_bot_buttons_json" in query
    assert params["$nickname"] == "Сова"
    assert params["$last_bot_buttons"] == '[{"label":"Visible","value":"callback:value"}]'


@pytest.mark.asyncio
async def test_user_repository_persists_telegram_identity_without_overwriting_role() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))
    user = TelegramUser(
        tg_id=42,
        telegram_handle="@Game_Master",
        linked_vk_id=84,
        is_gamemaster=True,
        gamemaster_grant_operation_id="grant-1",
    )

    await YdbUserRepository(executor).save(user)

    query, params = executor.query.await_args.args
    assert "telegram_handle" in query
    assert params["$telegram_handle"] == "@game_master"
    assert params["$linked_vk_id"] == 84
    assert "is_gamemaster" not in query
    assert "privileged_role" not in query
    assert "$is_gamemaster" not in params


@pytest.mark.asyncio
async def test_user_repository_finds_telegram_user_by_own_handle() -> None:
    timestamp = datetime(2026, 9, 5, tzinfo=UTC)
    executor = SimpleNamespace(
        query=AsyncMock(
            return_value=[
                {
                    "tg_id": 42,
                    "vk_url": "https://vk.com/player",
                    "telegram_handle": "@player_name",
                    "full_name": "Иванов Иван",
                    "crossplay": True,
                    "larp_experience": False,
                    "needs_pass": False,
                    "pass_details_json": None,
                    "dialog_state": "IDLE",
                    "dialog_context_json": "{}",
                    "last_update_id": None,
                    "last_update_at": None,
                    "last_delivery_operation_id": None,
                    "last_bot_buttons_json": None,
                    "is_gamemaster": True,
                    "privileged_role": "gamemaster_viewer",
                    "gamemaster_grant_operation_id": "grant-1",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            ]
        )
    )

    user = await YdbUserRepository(executor).find_telegram_by_handle("@player_name")

    assert user is not None and user.tg_id == 42 and user.profile_complete
    assert user.is_gamemaster and user.privileged_role is PrivilegedRole.GAMEMASTER_VIEWER
    assert user.gamemaster_grant_operation_id == "grant-1"
    assert executor.query.await_args.args[1] == {"$handle": "@player_name"}
    assert "FROM `tg_users` VIEW telegram_identity WHERE telegram_handle = $handle" in executor.query.await_args.args[0]


@pytest.mark.parametrize("owners", [[], [{"tg_id": 99}], [{"tg_id": 42}, {"tg_id": 99}]])
async def test_indexed_telegram_link_rejects_missing_wrong_or_ambiguous_owner(owners: list[dict[str, int]]) -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=owners))
    user = TelegramUser(tg_id=42, linked_vk_id=84, telegram_handle="@player_name")

    assert await YdbUserRepository(executor).find_linked(user) is None

    executor.query.assert_awaited_once()
    query, params = executor.query.await_args.args
    assert "SELECT tg_id FROM `tg_users` VIEW telegram_identity" in query
    assert "WHERE linked_vk_id = $vk_id AND telegram_handle = $handle" in query
    assert "LIMIT 2" in query
    assert params == {"$vk_id": 84, "$handle": "@player_name"}


@pytest.mark.parametrize("reciprocal_handle", ["@player_name", "@someone_else", None])
async def test_indexed_telegram_link_still_checks_the_vk_profile(reciprocal_handle: str | None) -> None:
    timestamp = datetime(2026, 9, 29, tzinfo=UTC)
    executor = SimpleNamespace(
        query=AsyncMock(
            side_effect=[
                [{"tg_id": 42}],
                [{"vk_id": 84, "telegram_handle": reciprocal_handle, "created_at": timestamp, "updated_at": timestamp}],
            ]
        )
    )
    user = TelegramUser(tg_id=42, linked_vk_id=84, telegram_handle="@player_name")

    linked = await YdbUserRepository(executor).find_linked(user)

    assert (linked is not None) == (reciprocal_handle == user.telegram_handle)
    assert "FROM `tg_users` VIEW telegram_identity" in executor.query.await_args_list[0].args[0]
    assert "FROM `vk_users` WHERE vk_id = $user_id" in executor.query.await_args_list[1].args[0]
    assert executor.query.await_args_list[1].args[1] == {"$user_id": 84}


@pytest.mark.parametrize("matches", [0, 1, 2])
@pytest.mark.parametrize("by_handle", [False, True])
async def test_indexed_telegram_profile_lookup_requires_exactly_one_match(matches: int, by_handle: bool) -> None:
    timestamp = datetime(2026, 9, 29, tzinfo=UTC)
    executor = SimpleNamespace(
        query=AsyncMock(
            return_value=[
                {
                    "tg_id": 42 + index,
                    "telegram_handle": "@player_name",
                    "linked_vk_id": 84,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                for index in range(matches)
            ]
        )
    )
    repository = YdbUserRepository(executor)
    linked = (
        await repository.find_telegram_by_handle("@player_name")
        if by_handle
        else await repository.find_linked(VkUser(vk_id=84, telegram_handle="@player_name"))
    )

    assert (linked is not None) == (matches == 1)
    if linked is not None:
        assert isinstance(linked, TelegramUser) and linked.tg_id == 42
    query, params = executor.query.await_args.args
    assert "FROM `tg_users` VIEW telegram_identity" in query
    assert "LIMIT 2" in query
    assert params == ({"$handle": "@player_name"} if by_handle else {"$vk_id": 84, "$handle": "@player_name"})
    if not by_handle:
        assert "WHERE linked_vk_id = $vk_id AND telegram_handle = $handle" in query


@pytest.mark.asyncio
async def test_user_repository_backfills_telegram_handle_without_overwriting_profile() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))

    await YdbUserRepository(executor).update_telegram_handle(42, "@player_name")

    query, params = executor.query.await_args.args
    assert "UPDATE `tg_users`" in query
    assert "SET telegram_handle = $handle" in query
    assert params["$user_id"] == 42
    assert params["$handle"] == "@player_name"


@pytest.mark.asyncio
async def test_user_repository_updates_only_resolved_telegram_link_columns() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))

    await YdbUserRepository(executor).update_telegram_link(42, "@player_name", 84)

    query, params = executor.query.await_args.args
    assert "UPDATE `tg_users`" in query
    assert "telegram_handle = $handle" in query
    assert "linked_vk_id = $linked_vk_id" in query
    assert "dialog_state" not in query
    assert params["$user_id"] == 42
    assert params["$handle"] == "@player_name"
    assert params["$linked_vk_id"] == 84


@pytest.mark.asyncio
async def test_user_repository_grants_larp_experience_without_rewriting_profile() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))

    await YdbUserRepository(executor).grant_larp_experience(Platform.TELEGRAM, 42)

    query, params = executor.query.await_args.args
    assert "UPDATE `tg_users`" in query
    assert "SET larp_experience = TRUE" in query
    assert params["$user_id"] == 42
    assert set(params) == {"$user_id", "$updated_at"}


@pytest.mark.parametrize("experienced", [True, False])
@pytest.mark.asyncio
async def test_user_repository_sets_larp_experience_without_rewriting_profile(experienced: bool) -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))

    await YdbUserRepository(executor).set_larp_experience(Platform.VK, 84, experienced)

    query, params = executor.query.await_args.args
    assert "UPDATE `vk_users`" in query
    assert "SET larp_experience = $experienced" in query
    assert params["$user_id"] == 84
    assert params["$experienced"] is experienced
    assert set(params) == {"$user_id", "$experienced", "$updated_at"}


@pytest.mark.asyncio
async def test_user_repository_sets_privileged_role_in_selected_platform_table() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))

    await YdbUserRepository(executor).set_privileged_role(
        Platform.VK,
        84,
        PrivilegedRole.PASS_MANAGER,
        "grant-1",
    )

    query, params = executor.query.await_args.args
    assert "UPDATE `vk_users`" in query
    assert "SET is_gamemaster = TRUE" in query
    assert "privileged_role = $role" in query
    assert params == {"$user_id": 84, "$role": "pass_manager", "$operation_id": "grant-1"}


@pytest.mark.asyncio
async def test_user_repository_clears_privileged_role_in_selected_platform_table() -> None:
    executor = SimpleNamespace(query=AsyncMock(return_value=[]))

    await YdbUserRepository(executor).clear_privileged_role(Platform.TELEGRAM, 42, "revoke-1")

    query, params = executor.query.await_args.args
    assert "UPDATE `tg_users`" in query
    assert "SET is_gamemaster = FALSE" in query
    assert "privileged_role = $role" in query
    assert params == {"$user_id": 42, "$role": None, "$operation_id": "revoke-1"}


@pytest.mark.asyncio
async def test_event_repository_removes_leader_without_renaming_ydb_table() -> None:
    executor = SimpleNamespace(query=AsyncMock(side_effect=[[{"event_id": "event-a1"}], []]))
    repository = YdbEventRepository(executor)
    identity = BotIdentity(platform=Platform.VK, platform_user_id=84)

    removed = await repository.remove_leader("event-a1", identity)

    assert removed
    delete_query, delete_params = executor.query.await_args.args
    assert "DELETE FROM `event_leaders`" in delete_query
    assert "RETURNING event_id" in delete_query
    executor.query.assert_awaited_once()
    assert delete_params == {
        "$event_id": "event-a1",
        "$platform": "vk",
        "$platform_user_id": 84,
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, '["earlier"]', '["earlier","current"]'])
async def test_participation_delivery_markers_append_in_a_serializable_transaction(previous: str | None) -> None:
    executor = YdbExecutor.__new__(YdbExecutor)
    executor.pool = MagicMock()
    session = MagicMock()
    transaction = session.transaction.return_value.begin.return_value
    session.prepare.side_effect = ["prepared-select", "prepared-update"]
    transaction.execute.side_effect = [
        [SimpleNamespace(rows=[{"participation_notification_deliveries_json": previous}])],
        [],
    ]
    executor.pool.retry_operation_sync.side_effect = lambda operation: operation(session)
    await executor.mark_participation_notification_delivered("event-a1", "a" * 43, "current")
    assert session.transaction.call_args.args[0].name == "serializable_read_write"
    if previous == '["earlier","current"]':
        transaction.commit.assert_called_once()
        assert transaction.execute.call_count == 1
    else:
        update = transaction.execute.call_args
        assert update.kwargs == {"commit_tx": True}
        assert update.args[1]["$event_id"] == "event-a1"
        assert update.args[1]["$participant_key"] == "a" * 43
        assert update.args[1]["$delivered"] == ('["current"]' if previous is None else '["earlier","current"]')
