from __future__ import annotations

from typing import cast

import boto3
from botocore.config import Config

from larp_bot.adapters.aws import (
    AwsStagedYandexDiskUploader,
    DynamoEventRepository,
    DynamoRegistrationRepository,
    DynamoUserRepository,
    LambdaBatchConsumer,
    SqsFifoCommandPublisher,
    SsmRuntimeConfigProvider,
)
from larp_bot.adapters.google_drive import (
    GoogleDriveAppsScriptClient,
    GoogleDriveMirroredUploader,
    GoogleDriveRestClient,
    GoogleDriveStatisticsRepository,
    GoogleDriveTableStorage,
)
from larp_bot.adapters.transports import CloudflareTelegramEgress, MultiplexedDeferredTransport, VkApiTransport
from larp_bot.adapters.yandex_disk.repository import YandexDiskRestClient, YandexDiskShowcaseRepository
from larp_bot.application.chat_management import TelegramChatManagementService
from larp_bot.application.conversation import ConversationEngine
from larp_bot.application.game_closings import ScheduledGameClosingService
from larp_bot.application.ports import EventRepository, RegistrationRepository, UserRepository
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
from larp_bot.config import AwsSettings
from larp_bot.config.logging import configure_logging
from larp_bot.functions.container import (
    AppContainer,
    ContinuableOrderedCommandPublisher,
    MutableDiskUploader,
    MutableRuntimeConfig,
)

AWS_DYNAMODB_CONFIG = Config(
    retries={
        # Production uses bounded provisioned capacity inside the DynamoDB free
        # allowance. Adaptive mode still paces short bursts instead of
        # exhausting the standard retry budget and sending otherwise valid
        # commands back to SQS.
        "mode": "adaptive",
        "total_max_attempts": 20,
    }
)


async def build_aws_container(*, consumer: LambdaBatchConsumer | None = None) -> AppContainer:
    settings = AwsSettings.from_env()
    configure_logging(settings.app_log_level)
    dynamodb = boto3.client("dynamodb", config=AWS_DYNAMODB_CONFIG)
    users = cast(
        UserRepository,
        DynamoUserRepository(
            dynamodb,
            telegram_table=settings.dynamodb_tg_users_table,
            vk_table=settings.dynamodb_vk_users_table,
        ),
    )
    events = cast(
        EventRepository,
        DynamoEventRepository(
            dynamodb,
            events_table=settings.dynamodb_events_table,
            leaders_table=settings.dynamodb_event_leaders_table,
        ),
    )
    registrations = cast(
        RegistrationRepository,
        DynamoRegistrationRepository(dynamodb, registrations_table=settings.dynamodb_registrations_table),
    )
    config = SsmRuntimeConfigProvider(boto3.client("ssm"), settings.aws_parameter_prefix)
    secrets = {
        key: await config.get_secret(key)
        for key in (
            "YANDEX_DISK_TOKEN",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_APPS_SCRIPT_HMAC_SECRET",
            "PARTICIPANT_KEY_HMAC_SECRET",
            "AWS_TO_CF_EGRESS_HMAC_SECRET",
            "VK_ACCESS_TOKEN",
        )
    }
    disk = YandexDiskRestClient(secrets["YANDEX_DISK_TOKEN"])
    disk_uploader = AwsStagedYandexDiskUploader(
        s3_client=boto3.client("s3"),
        lambda_client=boto3.client("lambda"),
        bucket=settings.aws_storage_bucket,
        function_name=settings.aws_disk_uploader_function,
    )
    google_drive = GoogleDriveRestClient(secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    google_table_bootstrap = GoogleDriveAppsScriptClient(
        settings.google_apps_script_url,
        secrets["GOOGLE_APPS_SCRIPT_HMAC_SECRET"],
        settings.google_folder_id,
    )
    google_tables = GoogleDriveTableStorage(google_drive, google_table_bootstrap)
    mirrored_uploader = GoogleDriveMirroredUploader(disk_uploader, google_tables, settings.google_folder_id)
    showcase = YandexDiskShowcaseRepository(disk, mirrored_uploader)
    player_statistics = GoogleDriveStatisticsRepository(google_drive, settings.google_folder_id)
    master_statistics = GoogleDriveStatisticsRepository.masters(google_drive, settings.google_folder_id)
    publisher = SqsFifoCommandPublisher(boto3.client("sqs"), settings.aws_fifo_queue_url)
    telegram = CloudflareTelegramEgress(
        settings.telegram_egress_url,
        secrets["AWS_TO_CF_EGRESS_HMAC_SECRET"],
    )
    vk = VkApiTransport(secrets["VK_ACCESS_TOKEN"])
    transport = MultiplexedDeferredTransport(telegram, vk)
    catalog = RegistrationCatalog(
        events,
        registrations,
        showcase,
        users=users,
        participant_secret=secrets["PARTICIPANT_KEY_HMAC_SECRET"],
        legacy_registration_imports=False,
    )
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
    statistics = StatisticsService(
        player_statistics,
        master_statistics,
        config,
        administration,
        registration_service,
        telegram_chat=telegram,
    )
    scheduled_game_closing = ScheduledGameClosingService(events, statistics, transport, publisher, administration)
    upload_completion = UploadCompletionService(events, users, config, transport, publisher)
    batch_consumer = consumer or LambdaBatchConsumer()
    worker = OrderedWorker(
        batch_consumer,
        mutations,
        users,
        transport,
        notifications,
        max_seconds=settings.worker_max_seconds,
        statistics=statistics,
        scheduled_game_closing=scheduled_game_closing,
        administration=administration,
    )
    return AppContainer(
        settings=settings,
        config=cast(MutableRuntimeConfig, config),
        users=users,
        events=events,
        registrations=registrations,
        showcase=showcase,
        disk_uploader=cast(MutableDiskUploader, mirrored_uploader),
        publisher=cast(ContinuableOrderedCommandPublisher, publisher),
        transport=transport,
        telegram_chat_management=telegram_chat_management,
        conversation=conversation,
        worker=worker,
        upload_completion=upload_completion,
    )
