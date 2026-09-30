from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from pathlib import Path

import pytest

from larp_bot.adapters.yandex_disk.repository import DiskUpload
from larp_bot.domain.models import Event, EventStatus


def achievement_template_bytes() -> bytes:
    return (Path(__file__).parents[1] / "src/larp_bot/assets/pdf_achievements_usual.pdf").read_bytes()


class MemoryDiskStore:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.public_urls: dict[str, str] = {}
        self.replace_count: dict[str, int] = {}

    async def download(self, path: str) -> bytes:
        return bytes(self.files[path])

    async def exists(self, path: str) -> bool:
        return path in self.files

    async def list_files(self, path: str) -> list[str]:
        return [name for name in self.files if name.rsplit("/", 1)[0] == path]

    async def submit(
        self,
        files: Sequence[DiskUpload],
        *,
        callback: dict[str, object] | None = None,
        before_invoke: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, str]:
        del callback
        if before_invoke is not None:
            await before_invoke()
        result: dict[str, str] = {}
        for upload in files:
            if not upload.overwrite and upload.disk_path in self.files:
                raise FileExistsError(upload.disk_path)
            if upload.overwrite and upload.disk_path not in self.files:
                raise FileNotFoundError(upload.disk_path)
            self.files[upload.disk_path] = bytes(upload.content)
            if upload.overwrite:
                self.replace_count[upload.disk_path] = self.replace_count.get(upload.disk_path, 0) + 1
            if upload.publish:
                result[upload.role] = self.public_urls.setdefault(
                    upload.disk_path,
                    f"https://disk.example/public/{len(self.public_urls) + 1}",
                )
        return result

    async def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.public_urls.pop(path, None)

    def clone(self) -> MemoryDiskStore:
        clone = MemoryDiskStore()
        clone.files = deepcopy(self.files)
        clone.public_urls = deepcopy(self.public_urls)
        clone.replace_count = deepcopy(self.replace_count)
        return clone


@pytest.fixture
def disk_store() -> MemoryDiskStore:
    return MemoryDiskStore()


@pytest.fixture
def event() -> Event:
    return Event(
        event_id="event-a1",
        name="Лесной предел",
        disk_resource_path="disk:/larp-bot/events/event-a1-les.xlsx",
        public_registration_url="https://disk.example/public/1",
        status=EventStatus.CONFIRMATION_OPEN,
    )
