from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_REDACTED_FIELDS = {
    "authorization",
    "body",
    "dialog_context",
    "email",
    "full_name",
    "payload",
    "secret",
    "token",
}


class JsonFormatter(logging.Formatter):
    """Small JSON formatter that excludes known personal and secret fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and key.casefold() not in _REDACTED_FIELDS:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not any(getattr(handler, "_larp_json", False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._larp_json = True  # type: ignore[attr-defined]
        root.handlers.clear()
        root.addHandler(handler)
    root.setLevel(level.upper())
