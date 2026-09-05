from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from larp_bot.adapters.yandex_disk.repository import DiskObjectStore
from larp_bot.adapters.yandex_disk.statistics_workbook import StatisticsWorkbook
from larp_bot.application.services import DomainError

STATS_DIRECTORY = "disk:/larp-bot/stats"
BACKUP_RE = re.compile(r"backup-\d{20}-[a-f0-9-]{36}\.xlsx")


class StatisticsStore(DiskObjectStore, Protocol):
    async def exists(self, path: str) -> bool: ...

    async def list_files(self, path: str) -> list[str]: ...


def table_filename(name: str) -> str:
    clean = name.strip()
    if not clean.endswith(".xlsx"):
        clean += ".xlsx"
    if not re.fullmatch(r"[^/\\:*?\"<>|\x00-\x1f]{1,180}\.xlsx", clean) or clean.startswith("."):
        raise DomainError("Укажите только имя XLSX-файла из папки larp-bot/stats.")
    if clean in {"current.xlsx", "showcase.xlsx", "pending.xlsx"}:
        raise DomainError("Это служебный файл. Укажите initial или имя резервной копии.")
    return clean


class StatisticsState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = "initial.xlsx"
    last_operation: str | None = None
    pending_operation: str | None = None


class YandexDiskStatisticsRepository:
    """All calls run in the one global statistics FIFO group.

    The durable pending marker commits a staged result. Any retry completes that
    commit before executing another action. Backups and selected source files are
    never overwritten; only bot-generated backups enter the retention policy.
    """

    def __init__(self, store: StatisticsStore, retention: int = 20) -> None:
        if retention < 1:
            raise ValueError("retention must be positive")
        self.store = store
        self.retention = retention

    @staticmethod
    def path(name: str) -> str:
        return f"{STATS_DIRECTORY}/{name}"

    async def _put(self, name: str, content: bytes) -> None:
        path = self.path(name)
        if await self.store.exists(path):
            await self.store.replace(path, content)
        else:
            await self.store.upload_new(path, content)

    async def _save_state(self, state: StatisticsState) -> None:
        await self._put("state.json", state.model_dump_json().encode())

    async def _state(self) -> StatisticsState:
        if await self.store.exists(self.path("state.json")):
            state = StatisticsState.model_validate_json(await self.store.download(self.path("state.json")))
        else:
            state = StatisticsState()
        if state.source != "current.xlsx":
            table_filename(state.source)
        if state.pending_operation:
            content = await self.store.download(self.path("pending.xlsx"))
            await self._put("current.xlsx", content)
            await self._put("showcase.xlsx", content)
            await self.store.publish(self.path("showcase.xlsx"))
            state.source = "current.xlsx"
            state.last_operation = state.pending_operation
            state.pending_operation = None
            await self._save_state(state)
        return state

    async def _content(self, state: StatisticsState) -> bytes:
        path = self.path(state.source)
        if not await self.store.exists(path):
            raise DomainError(f"Файл {state.source} не найден в larp-bot/stats. Загрузите его на Яндекс Диск.")
        return await self.store.download(path)

    async def _prune(self) -> None:
        files = await self.store.list_files(STATS_DIRECTORY)
        backups = sorted(
            p for p in files if p.rsplit("/", 1)[0] == STATS_DIRECTORY and BACKUP_RE.fullmatch(p.rsplit("/", 1)[1])
        )
        for path in backups[: -self.retention]:
            await self.store.delete(path)
        if await self.store.exists(self.path("pending.xlsx")):
            await self.store.delete(self.path("pending.xlsx"))

    async def select(self, name: str, operation_id: str) -> str:
        name = table_filename(name)
        state = await self._state()
        if state.last_operation != operation_id:
            selected = StatisticsState(source=name)
            content = await self._content(selected)
            await asyncio.to_thread(StatisticsWorkbook, content)
            # Selecting a backup pins its bytes in current before retention can delete it.
            await self._put("pending.xlsx", content)
            state.pending_operation = operation_id
            await self._save_state(state)
            await self._state()
        await self._prune()
        return f"Актуальная таблица выбрана: {name}."

    async def link(self) -> str:
        state = await self._state()
        if not await self.store.exists(self.path("showcase.xlsx")):
            content = await self._content(state)
            await asyncio.to_thread(StatisticsWorkbook, content)
            await self._put("showcase.xlsx", content)
        url = await self.store.publish(self.path("showcase.xlsx"))
        await self._prune()
        return url

    async def edit(
        self, operation_id: str, stamp: str, transform: Callable[[StatisticsWorkbook], bytes | None]
    ) -> bool:
        state = await self._state()
        if state.last_operation == operation_id:
            await self._prune()
            return False
        content = await self._content(state)
        result = await asyncio.to_thread(lambda: transform(StatisticsWorkbook(content)))
        if result is None:
            await self._prune()
            return False
        backup = f"backup-{stamp}-{operation_id}.xlsx"
        if not BACKUP_RE.fullmatch(backup):
            raise ValueError("invalid backup identity")
        if not await self.store.exists(self.path(backup)):
            await self.store.upload_new(self.path(backup), content)
        await self._put("pending.xlsx", result)
        state.pending_operation = operation_id
        await self._save_state(state)
        await self._state()
        await self._prune()
        return True
