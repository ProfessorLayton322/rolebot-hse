from __future__ import annotations

import json
import logging

from larp_bot.config.logging import JsonFormatter


def test_structured_logger_omits_personal_and_secret_fields() -> None:
    record = logging.LogRecord("larp_bot.test", logging.INFO, __file__, 1, "registration_enqueued", (), None)
    record.request_id = "request-1"
    record.event_id = "event-1"
    record.full_name = "Sensitive Name"
    record.token = "secret-token"
    decoded = json.loads(JsonFormatter().format(record))
    assert decoded["event"] == "registration_enqueued"
    assert decoded["request_id"] == "request-1"
    assert decoded["event_id"] == "event-1"
    assert "full_name" not in decoded
    assert "token" not in decoded
