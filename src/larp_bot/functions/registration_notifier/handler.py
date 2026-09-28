from __future__ import annotations

import asyncio
from typing import Any

from larp_bot.functions.bootstrap import AppContainer, build_container, iam_token_from_context

_container: AppContainer | None = None
_runner = asyncio.Runner()


async def _run(context: Any) -> dict[str, int]:
    global _container
    iam_token = iam_token_from_context(context)
    if _container is None:
        _container = await build_container(iam_token=iam_token)
    elif iam_token is not None:
        _container.config.set_iam_token(iam_token)
        _container.disk_uploader.set_iam_token(iam_token)
    result, closings = await asyncio.gather(
        _container.registration_opening_notifications.notify_due(),
        _container.scheduled_game_closing_dispatcher.dispatch_due(),
    )
    return {
        "events_completed": result.events_completed,
        "notifications_delivered": result.notifications_delivered,
        "closing_commands_enqueued": closings.commands_enqueued,
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    del event
    return _runner.run(_run(context))
