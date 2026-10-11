from __future__ import annotations

import asyncio
import logging
from typing import Any

from larp_bot.adapters.aws import LambdaBatchConsumer
from larp_bot.functions.aws_bootstrap import build_aws_container
from larp_bot.functions.container import AppContainer
from larp_bot.functions.runtime_mode import RuntimeGate

LOGGER = logging.getLogger("larp_bot.aws_ordered_worker")
_consumer = LambdaBatchConsumer()
_container: AppContainer | None = None
_runner = asyncio.Runner()


async def _run(event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    global _container
    _consumer.reset(event)
    if not RuntimeGate.from_environment().accepts_application_work:
        return _consumer.batch_response()
    if _container is None:
        _container = await build_aws_container(consumer=_consumer)
    try:
        bulk_read_snapshot = getattr(_container.users, "bulk_read_snapshot", None)
        if bulk_read_snapshot is None:
            await _container.worker.run()
        else:
            async with bulk_read_snapshot():
                await _container.worker.run()
    except Exception:
        LOGGER.exception("aws_ordered_batch_failed")
    return _consumer.batch_response()


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    del context
    return _runner.run(_run(event))
