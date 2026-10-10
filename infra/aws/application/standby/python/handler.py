from __future__ import annotations

from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    del event, context
    return {
        "statusCode": 503,
        "headers": {"content-type": "application/json"},
        "body": '{"status":"standby","mutations_enabled":false}',
    }
