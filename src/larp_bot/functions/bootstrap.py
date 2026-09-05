from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3

from larp_bot.adapters.runtime_config import RuntimeConfigProvider
from larp_bot.adapters.transports import (
    CloudflareTelegramEgress,
    MultiplexedDeferredTransport,
    VkApiTransport,
)
from larp_bot.adapters.yandex_disk.repository import (
    YandexDiskRestClient,
    YandexDiskShowcaseRepository,
)
from larp_bot.adapters.ydb import YdbEventRepository, YdbExecutor, YdbRegistrationRepository, YdbUserRepository
from larp_bot.adapters.ymq import YmqCommandPublisher, YmqFifoConsumer
from larp_bot.application.conversation import ConversationEngine
from larp_bot.application.services import (
    ConfirmationNotificationService,
    EventAdministrationService,
    OrderedMutationService,
    RegistrationCatalog,
    RegistrationService,
)
from larp_bot.application.worker import OrderedWorker
from larp_bot.config import Settings
from larp_bot.config.logging import configure_logging


@dataclass
class AppContainer:
    settings: Settings
    config: RuntimeConfigProvider
    users: YdbUserRepository
    events: YdbEventRepository
    registrations: YdbRegistrationRepository
    showcase: YandexDiskShowcaseRepository
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
    config = RuntimeConfigProvider(
        settings.runtime_config_url,
        settings.runtime_config_audience,
        settings.runtime_service_account_id,
        iam_token=iam_token,
    )
    secrets = {
        key: await config.get_secret(key)
        for key in (
            "YANDEX_DISK_TOKEN",
            "PARTICIPANT_KEY_HMAC_SECRET",
            "YANDEX_TO_CF_EGRESS_HMAC_SECRET",
            "VK_ACCESS_TOKEN",
            "YMQ_ACCESS_KEY_ID",
            "YMQ_SECRET_ACCESS_KEY",
        )
    }
    db = YdbExecutor(settings.ydb_endpoint, settings.ydb_database, iam_token=iam_token)
    users = YdbUserRepository(db)
    events = YdbEventRepository(db)
    registrations = YdbRegistrationRepository(db)
    showcase = YandexDiskShowcaseRepository(YandexDiskRestClient(secrets["YANDEX_DISK_TOKEN"]))
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
    catalog = RegistrationCatalog(events, registrations, showcase)
    registration_service = RegistrationService(events, catalog, publisher, secrets["PARTICIPANT_KEY_HMAC_SECRET"])
    administration = EventAdministrationService(
        events,
        showcase,
        catalog,
        users,
        secrets["PARTICIPANT_KEY_HMAC_SECRET"],
    )
    conversation = ConversationEngine(
        users,
        events,
        registration_service,
        administration,
        config,
        transport=transport,
        vk_user_ids=vk,
    )
    mutations = OrderedMutationService(events, catalog, users, secrets["PARTICIPANT_KEY_HMAC_SECRET"])
    notifications = ConfirmationNotificationService(
        events,
        catalog,
        users,
        transport,
        secrets["PARTICIPANT_KEY_HMAC_SECRET"],
    )
    worker = OrderedWorker(
        consumer,
        mutations,
        users,
        transport,
        notifications,
        max_seconds=settings.worker_max_seconds,
    )
    return AppContainer(
        settings=settings,
        config=config,
        users=users,
        events=events,
        registrations=registrations,
        showcase=showcase,
        publisher=publisher,
        transport=transport,
        conversation=conversation,
        worker=worker,
    )
