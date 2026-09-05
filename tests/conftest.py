from __future__ import annotations

from copy import deepcopy

import pytest

from larp_bot.domain.models import Event, EventStatus


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

    async def upload_new(self, path: str, content: bytes) -> None:
        if path in self.files:
            raise FileExistsError(path)
        self.files[path] = bytes(content)

    async def replace(self, path: str, content: bytes) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        self.files[path] = bytes(content)
        self.replace_count[path] = self.replace_count.get(path, 0) + 1

    async def publish(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        url = self.public_urls.setdefault(path, f"https://disk.example/public/{len(self.public_urls) + 1}")
        return url

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
