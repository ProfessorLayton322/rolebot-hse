from __future__ import annotations

import json
from typing import Any

import pytest

import aws_alarm_notifier


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def alarm_event(state: str = "ALARM") -> dict[str, Any]:
    return {
        "source": "aws.cloudwatch",
        "time": "2026-10-13T12:34:56Z",
        "region": "eu-central-1",
        "alarmData": {
            "alarmName": "larp-bot-prod-registration-retry-storm",
            "state": {
                "value": state,
                "reason": "Threshold Crossed: retry amplification was 4.2.",
            },
            "configuration": {
                "description": "Ordered events are retrying faster than they complete.",
            },
        },
    }


def test_alarm_notification_is_published_as_urgent_ntfy_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    def fake_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        sent["url"] = request.full_url
        sent["content_type"] = request.headers["Content-type"]
        sent["payload"] = json.loads(request.data)
        sent["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("NTFY_TOPIC", "N95oOJQuYvLC1wir")
    monkeypatch.setattr(aws_alarm_notifier, "urlopen", fake_urlopen)

    assert aws_alarm_notifier.handler(alarm_event(), None) == {"published": True, "status": 200}
    assert sent == {
        "url": "https://ntfy.sh/",
        "content_type": "application/json",
        "payload": {
            "topic": "N95oOJQuYvLC1wir",
            "title": "ALARM: larp-bot-prod-registration-retry-storm",
            "message": (
                "Ordered events are retrying faster than they complete.\n\n"
                "Threshold Crossed: retry amplification was 4.2.\n\n"
                "Time: 2026-10-13T12:34:56Z\n\nRegion: eu-central-1"
            ),
            "priority": 5,
            "tags": ["rotating_light", "cloud"],
            "click": (
                "https://eu-central-1.console.aws.amazon.com/cloudwatch/home"
                "?region=eu-central-1#alarmsV2:alarm/larp-bot-prod-registration-retry-storm"
            ),
        },
        "timeout": 10,
    }


def test_recovery_notification_uses_normal_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, object]] = []

    def fake_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        del timeout
        payloads.append(json.loads(request.data))
        return FakeResponse(201)

    monkeypatch.setenv("NTFY_TOPIC", "test-alerts")
    monkeypatch.setattr(aws_alarm_notifier, "urlopen", fake_urlopen)

    assert aws_alarm_notifier.handler(alarm_event("OK"), None) == {"published": True, "status": 201}
    assert payloads[0]["priority"] == 3
    assert payloads[0]["tags"] == ["heavy_check_mark", "cloud"]
    assert payloads[0]["title"] == "OK: larp-bot-prod-registration-retry-storm"


@pytest.mark.parametrize("topic", ["", "contains/slash", "a" * 65])
def test_invalid_ntfy_topic_is_rejected(monkeypatch: pytest.MonkeyPatch, topic: str) -> None:
    monkeypatch.setenv("NTFY_TOPIC", topic)

    with pytest.raises(RuntimeError, match="NTFY_TOPIC"):
        aws_alarm_notifier.handler(alarm_event(), None)


def test_malformed_cloudwatch_event_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "test-alerts")

    with pytest.raises(ValueError, match="alarmData"):
        aws_alarm_notifier.handler({}, None)
