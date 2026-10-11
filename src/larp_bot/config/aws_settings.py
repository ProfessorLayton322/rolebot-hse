from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class AwsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dynamodb_tg_users_table: str
    dynamodb_vk_users_table: str
    dynamodb_events_table: str
    dynamodb_event_leaders_table: str
    dynamodb_registrations_table: str
    aws_fifo_queue_url: str
    aws_storage_bucket: str
    aws_disk_uploader_function: str
    aws_parameter_prefix: str
    telegram_egress_url: str
    google_folder_id: str
    google_apps_script_url: str
    inline_safety_margin_ms: int = Field(default=100, ge=50, le=500)
    worker_max_seconds: float = Field(default=120.0, ge=1, le=300)
    app_log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> AwsSettings:
        required = {
            "dynamodb_tg_users_table": "DYNAMODB_TG_USERS_TABLE",
            "dynamodb_vk_users_table": "DYNAMODB_VK_USERS_TABLE",
            "dynamodb_events_table": "DYNAMODB_EVENTS_TABLE",
            "dynamodb_event_leaders_table": "DYNAMODB_EVENT_LEADERS_TABLE",
            "dynamodb_registrations_table": "DYNAMODB_REGISTRATIONS_TABLE",
            "aws_fifo_queue_url": "AWS_FIFO_QUEUE_URL",
            "aws_storage_bucket": "AWS_STORAGE_BUCKET",
            "aws_disk_uploader_function": "AWS_DISK_UPLOADER_FUNCTION",
            "aws_parameter_prefix": "AWS_PARAMETER_PREFIX",
            "telegram_egress_url": "TELEGRAM_EGRESS_URL",
            "google_folder_id": "GOOGLE_FOLDER_ID",
            "google_apps_script_url": "GOOGLE_APPS_SCRIPT_URL",
        }
        missing = [environment for environment in required.values() if not os.getenv(environment)]
        if missing:
            raise RuntimeError(f"missing required AWS configuration: {', '.join(missing)}")
        return cls(
            **{field: os.environ[environment] for field, environment in required.items()},
            inline_safety_margin_ms=int(os.getenv("INLINE_SAFETY_MARGIN_MS", "100")),
            worker_max_seconds=float(os.getenv("WORKER_MAX_SECONDS", "120")),
            app_log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
        )
