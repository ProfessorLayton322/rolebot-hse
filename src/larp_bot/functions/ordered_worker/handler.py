from __future__ import annotations

import asyncio
from typing import Any

from larp_bot.functions.bootstrap import AppContainer, build_container, iam_token_from_context

_container: AppContainer | None = None
# The cached container includes async transports. Reuse one event loop for the
# lifetime of the warm worker process so their connections remain valid.
_runner = asyncio.Runner()


async def _run(context: Any) -> dict[str, int]:
    global _container
    iam_token = iam_token_from_context(context)
    if _container is None:
        _container = await build_container(iam_token=iam_token)
    elif iam_token is not None:
        _container.lockbox.set_iam_token(iam_token)
    return {"processed": await _container.worker.run()}


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    del event
    return _runner.run(_run(context))
