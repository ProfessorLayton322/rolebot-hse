from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3

from larp_bot.adapters.lockbox import LockboxConfigProvider
from larp_bot.adapters.transports import (
    CloudflareTelegramEgress,
    MultiplexedDeferredTransport,
    VkApiTransport,
)
from larp_bot.adapters.yandex_disk.repository import (
    YandexDiskRegistrationRepository,
    YandexDiskRestClient,
)
from larp_bot.adapters.ydb import YdbEventRepository, YdbExecutor, YdbUserRepository
from larp_bot.adapters.ymq import YmqCommandPublisher, YmqFifoConsumer
from larp_bot.application.conversation import ConversationEngine
from larp_bot.application.services import (
    EventAdministrationService,
    OrderedMutationService,
    RegistrationService,
)
from larp_bot.application.worker import OrderedWorker
from larp_bot.config import Settings
from larp_bot.config.logging import configure_logging


@dataclass
class AppContainer:
    settings: Settings
    lockbox: LockboxConfigProvider
    users: YdbUserRepository
    events: YdbEventRepository
    tables: YandexDiskRegistrationRepository
    publisher: YmqCommandPublisher
    transport: MultiplexedDeferredTransport
    conversation: ConversationEngine
    worker: OrderedWorker


def iam_token_from_context(context: Any) -> str | None:
    token = getattr(context, "token", None)
    access_token = token.get("access_token") if isinstance(token, dict) else getattr(token, "access_token", None)
    return access_token if isinstance(access_token, str) and access_token else None


async def build_container(*, iam_token: str | None = None) -> AppContainer:
    settings = Settings.from_env()
    configure_logging(settings.app_log_level)
    lockbox = LockboxConfigProvider(settings.lockbox_secret_id, iam_token=iam_token)
    secrets = {
        key: await lockbox.get_secret(key)
        for key in (
            "YANDEX_DISK_TOKEN",
            "PARTICIPANT_KEY_HMAC_SECRET",
            "YANDEX_TO_CF_EGRESS_HMAC_SECRET",
            "VK_ACCESS_TOKEN",
            "YMQ_ACCESS_KEY_ID",
            "YMQ_SECRET_ACCESS_KEY",
        )
    }
    db = YdbExecutor(settings.ydb_endpoint, settings.ydb_database)
    users = YdbUserRepository(db)
    events = YdbEventRepository(db)
    tables = YandexDiskRegistrationRepository(YandexDiskRestClient(secrets["YANDEX_DISK_TOKEN"]))
    sqs = boto3.client(
        "sqs",
        endpoint_url=settings.ymq_endpoint,
        region_name="ru-central1",
        aws_access_key_id=secrets["YMQ_ACCESS_KEY_ID"],
        aws_secret_access_key=secrets["YMQ_SECRET_ACCESS_KEY"],
    )
    publisher = YmqCommandPublisher(sqs, settings.ymq_fifo_url, settings.ymq_kick_url)
    consumer = YmqFifoConsumer(sqs, settings.ymq_fifo_url)
    telegram = CloudflareTelegramEgress(settings.telegram_egress_url, secrets["YANDEX_TO_CF_EGRESS_HMAC_SECRET"])
    vk = VkApiTransport(secrets["VK_ACCESS_TOKEN"])
    transport = MultiplexedDeferredTransport(telegram, vk)
    registrations = RegistrationService(events, tables, publisher, secrets["PARTICIPANT_KEY_HMAC_SECRET"])
    administration = EventAdministrationService(events, tables)
    conversation = ConversationEngine(users, events, registrations, administration, lockbox)
    mutations = OrderedMutationService(events, tables)
    worker = OrderedWorker(
        consumer,
        mutations,
        users,
        transport,
        max_seconds=settings.worker_max_seconds,
    )
    return AppContainer(
        settings=settings,
        lockbox=lockbox,
        users=users,
        events=events,
        tables=tables,
        publisher=publisher,
        transport=transport,
        conversation=conversation,
        worker=worker,
    )
