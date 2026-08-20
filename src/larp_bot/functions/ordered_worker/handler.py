from __future__ import annotations

import asyncio
from typing import Any

from larp_bot.functions.bootstrap import AppContainer, build_container

_container: AppContainer | None = None


async def _run() -> dict[str, int]:
    global _container
    if _container is None:
        _container = await build_container()
    return {"processed": await _container.worker.run()}


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    del event, context
    return asyncio.run(_run())
