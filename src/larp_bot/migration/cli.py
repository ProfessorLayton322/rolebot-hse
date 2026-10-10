from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from larp_bot.migration.canonical import (
    CanonicalDocument,
    CanonicalValidationError,
    load_document,
    require_private_dump,
    write_document,
)
from larp_bot.migration.dynamodb import DynamoMigrationStore, DynamoTableNames
from larp_bot.migration.ydb import open_ydb_store

AWS_PROFILE = "rolebot-bootstrap-advanced"
AWS_REGION = "eu-central-1"
AWS_ACCOUNT_ID = "319215798726"
AWS_ROLE_NAME = "AccountFullAccessRole"
DEFAULT_DYNAMODB_PREFIX = "larp-bot-prod"
DELETE_CONFIRMATION = "DELETE-EXTRA-DURING-MAINTENANCE"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DUMP_ROOT = REPOSITORY_ROOT / "local-dumps"


def _default_dump(source: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DUMP_ROOT / f"{source}-{timestamp}.json"


def _output_path(value: str | None, source: str) -> Path:
    path = _default_dump(source) if value is None else Path(value)
    return require_private_dump(path, DUMP_ROOT)


def _input_path(value: str) -> Path:
    path = require_private_dump(Path(value), DUMP_ROOT)
    if not path.is_file():
        raise CanonicalValidationError(f"dump does not exist: {path}")
    return path


def _print_summary(action: str, path: Path | None, document: CanonicalDocument) -> None:
    location = f" {path}" if path is not None else ""
    counts = ", ".join(f"{table}={count}" for table, count in document["counts"].items())
    print(f"{action}{location}: {counts}; payload_sha256={document['hashes']['payload']}")


def _dynamodb_store(prefix: str) -> DynamoMigrationStore:
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    sts = session.client("sts", region_name=AWS_REGION)
    identity = sts.get_caller_identity()
    account = str(identity.get("Account", ""))
    arn = str(identity.get("Arn", ""))
    expected_arn_part = f":assumed-role/{AWS_ROLE_NAME}/"
    if account != AWS_ACCOUNT_ID or expected_arn_part not in arn:
        raise RuntimeError(
            f"refusing AWS operation: expected account {AWS_ACCOUNT_ID} and assumed role {AWS_ROLE_NAME}"
        )
    client = session.client("dynamodb", region_name=AWS_REGION)
    return DynamoMigrationStore(client, DynamoTableNames.from_prefix(prefix))


def _ydb_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", default=os.getenv("YDB_ENDPOINT"), help="YDB endpoint (or YDB_ENDPOINT)")
    parser.add_argument("--database", default=os.getenv("YDB_DATABASE"), help="YDB database (or YDB_DATABASE)")


def _required_ydb(value: str | None, name: str) -> str:
    if not value:
        raise CanonicalValidationError(f"{name} is required")
    return value


def _delete_extra_requested(args: argparse.Namespace) -> bool:
    if args.delete_extra and args.confirm_delete_extra != DELETE_CONFIRMATION:
        raise CanonicalValidationError(f"--delete-extra requires --confirm-delete-extra {DELETE_CONFIRMATION}")
    return bool(args.delete_extra)


def _delete_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--delete-extra",
        action="store_true",
        help="delete target rows absent from the dump; use only during an explicit maintenance window",
    )
    parser.add_argument("--confirm-delete-extra", help=argparse.SUPPRESS)


async def _export_ydb(args: argparse.Namespace) -> None:
    output = _output_path(args.output, "ydb")
    store, executor = open_ydb_store(
        _required_ydb(args.endpoint, "YDB endpoint"),
        _required_ydb(args.database, "YDB database"),
        _required_ydb(os.getenv("YDB_IAM_TOKEN"), "YDB_IAM_TOKEN"),
    )
    try:
        document = await store.export_document()
    finally:
        executor.close()
    write_document(output, document)
    _print_summary("exported", output, document)


def export_ydb_main() -> None:
    parser = argparse.ArgumentParser(description="Export YDB to a validated canonical JSON dump")
    _ydb_arguments(parser)
    parser.add_argument("--output", help="path inside local-dumps/ (default: timestamped filename)")
    asyncio.run(_export_ydb(parser.parse_args()))


async def _import_ydb(args: argparse.Namespace) -> None:
    source = _input_path(args.input)
    document = load_document(source)
    delete_extra = _delete_extra_requested(args)
    store, executor = open_ydb_store(
        _required_ydb(args.endpoint, "YDB endpoint"),
        _required_ydb(args.database, "YDB database"),
        _required_ydb(os.getenv("YDB_IAM_TOKEN"), "YDB_IAM_TOKEN"),
    )
    try:
        verified = await store.import_document(document, delete_extra=delete_extra)
    finally:
        executor.close()
    _print_summary("imported and round-trip verified", None, verified)


def import_ydb_main() -> None:
    parser = argparse.ArgumentParser(description="Import canonical JSON into YDB and verify by re-export")
    _ydb_arguments(parser)
    parser.add_argument("--input", required=True, help="0600 dump path inside local-dumps/")
    _delete_arguments(parser)
    asyncio.run(_import_ydb(parser.parse_args()))


async def _export_dynamodb(args: argparse.Namespace) -> None:
    output = _output_path(args.output, "dynamodb")
    document = await _dynamodb_store(args.table_prefix).export_document()
    write_document(output, document)
    _print_summary("exported", output, document)


def export_dynamodb_main() -> None:
    parser = argparse.ArgumentParser(description="Export DynamoDB to a validated canonical JSON dump")
    parser.add_argument("--table-prefix", default=DEFAULT_DYNAMODB_PREFIX)
    parser.add_argument("--output", help="path inside local-dumps/ (default: timestamped filename)")
    asyncio.run(_export_dynamodb(parser.parse_args()))


async def _import_dynamodb(args: argparse.Namespace) -> None:
    source = _input_path(args.input)
    document = load_document(source)
    delete_extra = _delete_extra_requested(args)
    verified = await _dynamodb_store(args.table_prefix).import_document(
        document,
        delete_extra=delete_extra,
    )
    _print_summary("imported and round-trip verified", None, verified)


def import_dynamodb_main() -> None:
    parser = argparse.ArgumentParser(description="Import canonical JSON into DynamoDB and verify by re-export")
    parser.add_argument("--table-prefix", default=DEFAULT_DYNAMODB_PREFIX)
    parser.add_argument("--input", required=True, help="0600 dump path inside local-dumps/")
    _delete_arguments(parser)
    asyncio.run(_import_dynamodb(parser.parse_args()))


def run(main: Any) -> None:
    try:
        main()
    except (CanonicalValidationError, RuntimeError) as exc:
        raise SystemExit(f"migration failed: {exc}") from exc
