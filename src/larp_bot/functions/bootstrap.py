from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3

from larp_bot.adapters.google_drive import GoogleDriveRestClient, GoogleDriveStatisticsRepository
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
from larp_bot.adapters.yandex_disk.uploader import StagedYandexDiskUploader
from larp_bot.adapters.ydb import YdbEventRepository, YdbExecutor, YdbRegistrationRepository, YdbUserRepository
from larp_bot.adapters.ymq import YmqCommandPublisher, YmqFifoConsumer
from larp_bot.application.chat_management import TelegramChatManagementService
from larp_bot.application.conversation import ConversationEngine
from larp_bot.application.game_closings import ScheduledGameClosingDispatcher, ScheduledGameClosingService
from larp_bot.application.registration_openings import RegistrationOpeningNotificationService
from larp_bot.application.services import (
    ConfirmationNotificationService,
    EventAdministrationService,
    OrderedMutationService,
    RegistrationCatalog,
    RegistrationService,
)
from larp_bot.application.statistics import StatisticsService
from larp_bot.application.upload_completion import UploadCompletionService
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
    disk_uploader: StagedYandexDiskUploader
    publisher: YmqCommandPublisher
    transport: MultiplexedDeferredTransport
    telegram_chat_management: TelegramChatManagementService
    conversation: ConversationEngine
    worker: OrderedWorker
    registration_opening_notifications: RegistrationOpeningNotificationService
    scheduled_game_closing_dispatcher: ScheduledGameClosingDispatcher
    upload_completion: UploadCompletionService


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
            "GOOGLE_SERVICE_ACCOUNT_JSON",
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
    disk = YandexDiskRestClient(secrets["YANDEX_DISK_TOKEN"])
    disk_uploader = StagedYandexDiskUploader(
        bucket=settings.disk_staging_bucket,
        function_url=settings.disk_uploader_url,
        iam_token=iam_token,
    )
    google_drive = GoogleDriveRestClient(secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    player_statistics = GoogleDriveStatisticsRepository(google_drive, settings.google_folder_id)
    master_statistics = GoogleDriveStatisticsRepository.masters(google_drive, settings.google_folder_id)
    showcase = YandexDiskShowcaseRepository(disk, disk_uploader)
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
    registration_service = RegistrationService(
        events,
        catalog,
        publisher,
        secrets["PARTICIPANT_KEY_HMAC_SECRET"],
        users,
    )
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
    mutations = OrderedMutationService(
        events,
        catalog,
        users,
        secrets["PARTICIPANT_KEY_HMAC_SECRET"],
        admins=config,
    )
    notifications = ConfirmationNotificationService(
        events,
        catalog,
        users,
        transport,
        secrets["PARTICIPANT_KEY_HMAC_SECRET"],
        telegram_chat=telegram,
    )
    telegram_chat_management = TelegramChatManagementService(
        events,
        registration_service,
        telegram,
        users,
        config,
    )
    registration_opening_notifications = RegistrationOpeningNotificationService(
        events,
        config,
        transport,
    )
    statistics = StatisticsService(
        player_statistics,
        master_statistics,
        config,
        administration,
        registration_service,
    )
    scheduled_game_closing_dispatcher = ScheduledGameClosingDispatcher(events, publisher)
    scheduled_game_closing = ScheduledGameClosingService(events, statistics, transport, publisher)
    upload_completion = UploadCompletionService(events, users, config, transport)
    worker = OrderedWorker(
        consumer,
        mutations,
        users,
        transport,
        notifications,
        max_seconds=settings.worker_max_seconds,
        statistics=statistics,
        scheduled_game_closing=scheduled_game_closing,
    )
    return AppContainer(
        settings=settings,
        config=config,
        users=users,
        events=events,
        registrations=registrations,
        showcase=showcase,
        disk_uploader=disk_uploader,
        publisher=publisher,
        transport=transport,
        telegram_chat_management=telegram_chat_management,
        conversation=conversation,
        worker=worker,
        registration_opening_notifications=registration_opening_notifications,
        scheduled_game_closing_dispatcher=scheduled_game_closing_dispatcher,
        upload_completion=upload_completion,
    )
