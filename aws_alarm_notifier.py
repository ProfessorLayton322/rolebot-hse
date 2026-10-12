from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

NTFY_URL = "https://ntfy.sh/"
TOPIC_PATTERN = re.compile(r"[-_A-Za-z0-9]{1,64}\Z")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"CloudWatch alarm event is missing {field}")
    return value.strip()


def _notification(event: Mapping[str, Any], topic: str) -> dict[str, object]:
    alarm_data = event.get("alarmData")
    if not isinstance(alarm_data, Mapping):
        raise ValueError("CloudWatch alarm event is missing alarmData")
    state = alarm_data.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("CloudWatch alarm event is missing alarmData.state")

    alarm_name = _required_text(alarm_data.get("alarmName"), "alarmData.alarmName")
    state_value = _required_text(state.get("value"), "alarmData.state.value").upper()
    reason = _required_text(state.get("reason"), "alarmData.state.reason")
    occurred_at = _required_text(event.get("time"), "time")
    region = _required_text(event.get("region"), "region")
    configuration = alarm_data.get("configuration")
    description = configuration.get("description") if isinstance(configuration, Mapping) else None

    lines = [reason, f"Time: {occurred_at}", f"Region: {region}"]
    if isinstance(description, str) and description.strip():
        lines.insert(0, description.strip())

    is_alarm = state_value == "ALARM"
    alarm_url = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#alarmsV2:alarm/{quote(alarm_name, safe='')}"
    )
    return {
        "topic": topic,
        "title": f"{state_value}: {alarm_name}",
        "message": "\n\n".join(lines),
        "priority": 5 if is_alarm else 3,
        "tags": ["rotating_light" if is_alarm else "heavy_check_mark", "cloud"],
        "click": alarm_url,
    }


def handler(event: Mapping[str, Any], context: Any) -> dict[str, object]:
    del context
    topic = os.environ.get("NTFY_TOPIC", "")
    if not TOPIC_PATTERN.fullmatch(topic):
        raise RuntimeError("NTFY_TOPIC must contain 1-64 letters, numbers, underscores, or dashes")

    payload = json.dumps(_notification(event, topic), ensure_ascii=False).encode()
    request = Request(
        NTFY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed trusted HTTPS endpoint
        status = response.status
        if not 200 <= status < 300:
            raise RuntimeError(f"ntfy returned HTTP {status}")
    return {"published": True, "status": status}
