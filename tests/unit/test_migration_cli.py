from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from larp_bot.migration.canonical import CanonicalValidationError
from larp_bot.migration.cli import (
    AWS_ACCOUNT_ID,
    AWS_PROFILE,
    AWS_REGION,
    DELETE_CONFIRMATION,
    _delete_extra_requested,
    _dynamodb_store,
)


def test_delete_extra_requires_explicit_maintenance_confirmation() -> None:
    with pytest.raises(CanonicalValidationError, match=DELETE_CONFIRMATION):
        _delete_extra_requested(Namespace(delete_extra=True, confirm_delete_extra=None))

    assert _delete_extra_requested(Namespace(delete_extra=True, confirm_delete_extra=DELETE_CONFIRMATION))
    assert not _delete_extra_requested(Namespace(delete_extra=False, confirm_delete_extra=None))


def test_dynamodb_store_uses_fixed_profile_region_and_verifies_role() -> None:
    session = MagicMock()
    sts = MagicMock()
    dynamodb = MagicMock()
    session.client.side_effect = [sts, dynamodb]
    sts.get_caller_identity.return_value = {
        "Account": AWS_ACCOUNT_ID,
        "Arn": f"arn:aws:sts::{AWS_ACCOUNT_ID}:assumed-role/AccountFullAccessRole/session",
    }

    with patch("larp_bot.migration.cli.boto3.Session", return_value=session) as factory:
        store = _dynamodb_store("rehearsal")

    factory.assert_called_once_with(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    assert session.client.call_args_list[0].args == ("sts",)
    assert session.client.call_args_list[0].kwargs == {"region_name": AWS_REGION}
    assert session.client.call_args_list[1].args == ("dynamodb",)
    assert store.client is dynamodb


def test_dynamodb_store_rejects_unexpected_account_before_opening_table_client() -> None:
    session = MagicMock()
    sts = MagicMock()
    session.client.return_value = sts
    sts.get_caller_identity.return_value = {
        "Account": "000000000000",
        "Arn": "arn:aws:sts::000000000000:assumed-role/AccountFullAccessRole/session",
    }

    with (
        patch("larp_bot.migration.cli.boto3.Session", return_value=session),
        pytest.raises(RuntimeError, match="refusing AWS operation"),
    ):
        _dynamodb_store("rehearsal")

    assert session.client.call_count == 1
