from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from larp_bot.adapters.transports import MultiplexedDeferredTransport
from larp_bot.adapters.yandex_disk.repository import DiskUploader, YandexDiskShowcaseRepository
from larp_bot.application.chat_management import TelegramChatManagementService
from larp_bot.application.conversation import ConversationEngine
from larp_bot.application.ports import (
    AdminConfigProvider,
    EventRepository,
    OrderedCommandPublisher,
    RegistrationRepository,
    UserRepository,
)
from larp_bot.application.upload_completion import UploadCompletionService
from larp_bot.application.worker import OrderedWorker


class MutableRuntimeConfig(AdminConfigProvider, Protocol):
    def set_iam_token(self, token: str) -> None: ...


class MutableDiskUploader(DiskUploader, Protocol):
    def set_iam_token(self, token: str) -> None: ...


class ContinuableOrderedCommandPublisher(OrderedCommandPublisher, Protocol):
    async def continue_if_pending(self, *, processed: int) -> None: ...


@dataclass
class AppContainer:
    settings: Any
    config: MutableRuntimeConfig
    users: UserRepository
    events: EventRepository
    registrations: RegistrationRepository
    showcase: YandexDiskShowcaseRepository
    disk_uploader: MutableDiskUploader
    publisher: ContinuableOrderedCommandPublisher
    transport: MultiplexedDeferredTransport
    telegram_chat_management: TelegramChatManagementService
    conversation: ConversationEngine
    worker: OrderedWorker
    upload_completion: UploadCompletionService


def iam_token_from_context(context: Any) -> str | None:
    token = getattr(context, "token", None)
    access_token = token.get("access_token") if isinstance(token, dict) else getattr(token, "access_token", None)
    return access_token if isinstance(access_token, str) and access_token else None
