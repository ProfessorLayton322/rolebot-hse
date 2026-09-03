from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import pytest

from larp_bot.adapters.memory import (
    MemoryDeferredTransport,
    MemoryEventRepository,
    MemoryRegistrationRepository,
    MemoryUserRepository,
)
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.adapters.ymq.client import QueueEnvelope
from larp_bot.application.services import OrderedMutationService, RegistrationCatalog
from larp_bot.application.worker import OrderedWorker
from larp_bot.domain.models import (
    AttendanceStatus,
    CharacterWishPayload,
    EmptyPayload,
    EnlistPayload,
    Event,
    Operation,
    OrderedRegistrationCommand,
    Platform,
    TelegramUser,
)
from tests.conftest import MemoryDiskStore


class FakeConsumer:
    def __init__(self, envelopes: Sequence[QueueEnvelope]) -> None:
        self.envelopes = list(envelopes)
        self.deleted: list[str] = []

    async def receive(self, *, max_messages: int, wait_seconds: int) -> Sequence[object]:
        del wait_seconds
        batch, self.envelopes = self.envelopes[:max_messages], self.envelopes[max_messages:]
        return batch

    async def delete(self, receipt_handle: str) -> None:
        self.deleted.append(receipt_handle)


class FailingTransport(MemoryDeferredTransport):
    async def send(self, **kwargs: object) -> None:
        del kwargs
        raise RuntimeError("transport unavailable")


def queued(
    event: Event,
    operation: Operation,
    payload: EnlistPayload | CharacterWishPayload | EmptyPayload,
    index: int,
) -> QueueEnvelope:
    command = OrderedRegistrationCommand(
        operation_id=str(uuid4()),
        event_id=event.event_id,
        operation=operation,
        platform=Platform.TELEGRAM,
        platform_user_id=1,
        participant_key="a" * 43,
        payload=payload,
    )
    return QueueEnvelope(command=command, receipt_handle=f"receipt-{index}")


@pytest.mark.asyncio
async def test_worker_processes_order_and_suppresses_duplicate_delivery(
    disk_store: MemoryDiskStore, event: Event
) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    events = MemoryEventRepository([event])
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    envelopes = [
        queued(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A"), 1),
        queued(event, Operation.CONFIRM, CharacterWishPayload(character_wish="A"), 2),
        queued(event, Operation.UPDATE_CHARACTER_WISH, CharacterWishPayload(character_wish="B"), 3),
        queued(event, Operation.CANCEL, EmptyPayload(), 4),
    ]
    consumer = FakeConsumer(envelopes)
    transport = MemoryDeferredTransport()
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(events, RegistrationCatalog(events, tables, showcase)),
        users,
        transport,
        max_seconds=2,
    )
    assert await worker.run() == 4
    result = await tables.get(event.event_id, "a" * 43)
    assert result is not None
    assert result.character_wish == "B"
    assert result.attendance_status is AttendanceStatus.CANCELLED
    assert consumer.deleted == [f"receipt-{index}" for index in range(1, 5)]
    assert len(transport.sent) == 4


@pytest.mark.asyncio
async def test_delivery_failure_keeps_fifo_message_retryable(disk_store: MemoryDiskStore, event: Event) -> None:
    tables = MemoryRegistrationRepository()
    showcase = YandexDiskShowcaseRepository(disk_store)
    await showcase.create_event_workbook(event.disk_resource_path)
    users = MemoryUserRepository()
    await users.save(TelegramUser(tg_id=1))
    consumer = FakeConsumer([queued(event, Operation.ENLIST, EnlistPayload(display_name="Player", wish_play="A"), 1)])
    worker = OrderedWorker(
        consumer,
        OrderedMutationService(
            events := MemoryEventRepository([event]),
            RegistrationCatalog(events, tables, showcase),
        ),
        users,
        FailingTransport(),
        max_seconds=2,
    )
    with pytest.raises(RuntimeError, match="transport unavailable"):
        await worker.run()
    assert consumer.deleted == []
    registration = await tables.get(event.event_id, "a" * 43)
    assert registration is not None
