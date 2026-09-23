from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from larp_bot.functions.bootstrap import AppContainer, build_container, iam_token_from_context

_container: AppContainer | None = None
_runner = asyncio.Runner()


def _payload(event: object) -> Mapping[str, Any]:
    if isinstance(event, bytes):
        parsed = json.loads(event)
    elif isinstance(event, str):
        parsed = json.loads(event)
    elif isinstance(event, dict):
        parsed = event
    else:
        raise ValueError("upload completion payload must be JSON")
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise ValueError("upload completion payload must be an object")
    return parsed


async def _run(event: object, context: Any) -> dict[str, bool]:
    global _container
    iam_token = iam_token_from_context(context)
    if _container is None:
        _container = await build_container(iam_token=iam_token)
    elif iam_token is not None:
        _container.config.set_iam_token(iam_token)
        _container.disk_uploader.set_iam_token(iam_token)
    await _container.upload_completion.handle(_payload(event))
    return {"processed": True}


def handler(event: object, context: Any) -> dict[str, bool]:
    return _runner.run(_run(event, context))
