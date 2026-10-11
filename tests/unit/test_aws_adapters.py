from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from larp_bot.adapters.aws.runtime_config import SsmRuntimeConfigProvider
from larp_bot.adapters.aws.sqs import AWS_ORDERED_MESSAGE_GROUP, LambdaBatchConsumer, SqsFifoCommandPublisher
from larp_bot.adapters.aws.storage import AwsStagedYandexDiskUploader
from larp_bot.adapters.yandex_disk.repository import DiskUpload
from larp_bot.domain.models import EmptyPayload, Operation, OrderedRegistrationCommand, Platform
from larp_bot.functions import aws_upload_completion
from larp_bot.functions.aws_bootstrap import AWS_DYNAMODB_CONFIG
from larp_bot.functions.gateway.handler import async_handler
from larp_bot.functions.gateway.handler import handler as gateway_handler
from larp_bot.functions.runtime_mode import ApplicationMode, RuntimeGate, runtime_controls_configured


def _command(operation_id: str = "11111111-1111-4111-8111-111111111111") -> OrderedRegistrationCommand:
    return OrderedRegistrationCommand(
        operation_id=operation_id,
        event_id="event-a1",
        operation=Operation.CANCEL,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key="a" * 43,
        payload=EmptyPayload(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_aws_dynamodb_client_adapts_to_free_tier_throttling() -> None:
    assert AWS_DYNAMODB_CONFIG.retries == {"mode": "adaptive", "total_max_attempts": 20}


class RecordingSqs:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def send_message(self, **request: Any) -> dict[str, str]:
        self.requests.append(request)
        return {"MessageId": "message-1"}


@pytest.mark.asyncio
async def test_aws_fifo_publisher_serializes_commands_and_preserves_operation_deduplication() -> None:
    client = RecordingSqs()
    publisher = SqsFifoCommandPublisher(client, "https://sqs.example/commands.fifo")
    command = _command()

    await publisher.publish(command)

    assert client.requests == [
        {
            "QueueUrl": "https://sqs.example/commands.fifo",
            "MessageBody": command.model_dump_json(),
            "MessageGroupId": AWS_ORDERED_MESSAGE_GROUP,
            "MessageDeduplicationId": command.operation_id,
        }
    ]


@pytest.mark.asyncio
async def test_lambda_batch_consumer_reports_failed_and_unprocessed_group_messages() -> None:
    first = _command("11111111-1111-4111-8111-111111111111")
    second = _command("22222222-2222-4222-8222-222222222222")
    consumer = LambdaBatchConsumer()
    consumer.reset(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "body": first.model_dump_json(),
                    "attributes": {"MessageGroupId": AWS_ORDERED_MESSAGE_GROUP},
                },
                {
                    "messageId": "message-2",
                    "body": second.model_dump_json(),
                    "attributes": {"MessageGroupId": AWS_ORDERED_MESSAGE_GROUP},
                },
            ]
        }
    )

    received = await consumer.receive(max_messages=10, wait_seconds=0)
    await consumer.delete(received[0].receipt_handle)

    assert await consumer.receive(max_messages=10, wait_seconds=0) == []
    assert consumer.batch_response() == {"batchItemFailures": [{"itemIdentifier": "message-2"}]}


@pytest.mark.asyncio
async def test_lambda_batch_consumer_accepts_a_legacy_per_command_group() -> None:
    command = _command()
    consumer = LambdaBatchConsumer()
    consumer.reset(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "body": command.model_dump_json(),
                    "attributes": {"MessageGroupId": command.queue_group_id},
                }
            ]
        }
    )

    assert len(await consumer.receive(max_messages=10, wait_seconds=0)) == 1


def test_lambda_batch_consumer_rejects_a_forged_message_group() -> None:
    command = _command()
    consumer = LambdaBatchConsumer()

    with pytest.raises(ValueError, match="message group"):
        consumer.reset(
            {
                "Records": [
                    {
                        "messageId": "message-1",
                        "body": command.model_dump_json(),
                        "attributes": {"MessageGroupId": "another-event"},
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_upload_completion_accepts_batched_s3_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    objects = {
        "upload-completions/job-1.json": {"schema_version": 1, "job_id": "job-1"},
        "upload-completions/job-2.json": {"schema_version": 1, "job_id": "job-2"},
    }

    class S3:
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            assert Bucket == "private-bucket"
            return {"Body": io.BytesIO(json.dumps(objects[Key]).encode())}

    monkeypatch.setattr(aws_upload_completion.boto3, "client", lambda service: S3())
    event = {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "private-bucket"},
                    "object": {"key": "upload-completions%2Fjob-1.json"},
                }
            },
            {
                "s3": {
                    "bucket": {"name": "private-bucket"},
                    "object": {"key": "upload-completions%2Fjob-2.json"},
                }
            },
        ]
    }

    payloads = await aws_upload_completion._completion_payloads(event)

    assert [payload["job_id"] for payload in payloads] == ["job-1", "job-2"]


class RecordingSsm:
    def __init__(self) -> None:
        self.calls = 0

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:
        self.calls += 1
        assert WithDecryption is True
        return {
            "Parameter": {"Name": Name, "Value": f"value:{Name.rsplit('/', 1)[-1]}"},
        }


@pytest.mark.asyncio
async def test_ssm_provider_uses_allowlisted_paths_and_warm_cache() -> None:
    client = RecordingSsm()
    provider = SsmRuntimeConfigProvider(
        client,
        "/larp-bot/production/",
        environment={"TG_ADMIN_IDS": "[10]", "VK_ADMIN_IDS": "[]"},
    )

    assert await provider.get_secret("VK_GROUP_ID") == "value:vk-group-id"
    assert await provider.get_secret("VK_ACCESS_TOKEN") == "value:vk-access-token"
    assert await provider.get_secret("GOOGLE_APPS_SCRIPT_HMAC_SECRET") == "value:google-apps-script-hmac-secret"
    assert client.calls == 3
    assert [identity.platform_user_id for identity in await provider.list_admins()] == [10]
    with pytest.raises(RuntimeError, match="not allowed"):
        await provider.get_secret("UNSCOPED_SECRET")


class RecordingS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    def put_object(self, **request: Any) -> dict[str, str]:
        self.puts.append(request)
        return {"VersionId": "version-1"}

    def delete_object(self, **request: Any) -> dict[str, Any]:
        self.deletes.append(request)
        return {}


class RecordingLambda:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def invoke(self, **request: Any) -> dict[str, int]:
        self.requests.append(request)
        return {"StatusCode": 202}


@pytest.mark.asyncio
async def test_aws_staging_uses_versioned_s3_and_async_lambda_invocation() -> None:
    s3 = RecordingS3()
    lambda_client = RecordingLambda()
    uploader = AwsStagedYandexDiskUploader(
        s3_client=s3,
        lambda_client=lambda_client,
        bucket="rolebot-storage",
        function_name="rolebot-disk-uploader",
    )

    await uploader.submit(
        [
            DiskUpload(
                role="master",
                disk_path="disk:/larp-bot/events/event-a1.xlsx",
                content=b"workbook",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                overwrite=True,
                publish=False,
            )
        ],
        callback={"kind": "event"},
    )

    assert s3.puts[0]["Bucket"] == "rolebot-storage"
    assert s3.puts[0]["Key"].startswith("uploads/")
    assert s3.puts[0]["Metadata"]["sha256"]
    request = lambda_client.requests[0]
    assert request["InvocationType"] == "Event"
    payload = json.loads(request["Payload"])
    assert payload["files"][0]["object_version"] == "version-1"
    assert payload["callback"] == {"kind": "event"}


def test_runtime_gate_requires_active_mode_and_explicit_mutations() -> None:
    standby = RuntimeGate.from_environment({"APPLICATION_MODE": "standby", "MUTATIONS_ENABLED": "false"})
    active = RuntimeGate.from_environment({"APPLICATION_MODE": "active", "MUTATIONS_ENABLED": "true"})

    assert standby.mode is ApplicationMode.STANDBY
    assert not standby.accepts_application_work
    assert active.accepts_application_work
    with pytest.raises(RuntimeError, match="only in active mode"):
        RuntimeGate.from_environment({"APPLICATION_MODE": "maintenance", "MUTATIONS_ENABLED": "true"})
    assert not runtime_controls_configured({})
    assert runtime_controls_configured({"MUTATIONS_ENABLED": "false"})


def test_aws_gateway_rejects_work_before_constructing_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("APPLICATION_MODE", "standby")
    monkeypatch.setenv("MUTATIONS_ENABLED", "false")

    response = gateway_handler(
        {"rawPath": "/webhooks/telegram", "requestContext": {"http": {"method": "POST"}}},
        None,
    )

    assert response["statusCode"] == 503


def test_yandex_gateway_honors_maintenance_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    monkeypatch.setenv("APPLICATION_MODE", "maintenance")
    monkeypatch.setenv("MUTATIONS_ENABLED", "false")

    response = gateway_handler(
        {"path": "/webhooks/telegram", "httpMethod": "POST"},
        None,
    )

    assert response["statusCode"] == 503


@pytest.mark.asyncio
async def test_function_url_v2_method_and_path_are_parsed_before_bootstrap() -> None:
    method_response = await async_handler(
        {"rawPath": "/webhooks/telegram", "requestContext": {"http": {"method": "GET"}}},
        None,
    )
    path_response = await async_handler(
        {"rawPath": "/unknown", "requestContext": {"http": {"method": "POST"}}},
        None,
    )

    assert method_response["statusCode"] == 405
    assert path_response["statusCode"] == 404
