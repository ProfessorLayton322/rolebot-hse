from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ydb_endpoint: str
    ydb_database: str
    ymq_endpoint: str = "https://message-queue.api.cloud.yandex.net"
    ymq_fifo_url: str
    ymq_kick_url: str
    runtime_config_url: str
    runtime_config_audience: str
    runtime_service_account_id: str
    telegram_egress_url: str
    inline_safety_margin_ms: int = Field(default=100, ge=50, le=500)
    worker_max_seconds: float = Field(default=40.0, ge=1, le=300)
    app_log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        required = {
            "ydb_endpoint": "YDB_ENDPOINT",
            "ydb_database": "YDB_DATABASE",
            "ymq_fifo_url": "YMQ_FIFO_URL",
            "ymq_kick_url": "YMQ_KICK_URL",
            "runtime_config_url": "RUNTIME_CONFIG_URL",
            "runtime_config_audience": "RUNTIME_CONFIG_AUDIENCE",
            "runtime_service_account_id": "RUNTIME_SERVICE_ACCOUNT_ID",
            "telegram_egress_url": "TELEGRAM_EGRESS_URL",
        }
        missing = [environment for environment in required.values() if not os.getenv(environment)]
        if missing:
            raise RuntimeError(f"missing required configuration: {', '.join(missing)}")
        return cls(
            **{field: os.environ[environment] for field, environment in required.items()},
            ymq_endpoint=os.getenv("YMQ_ENDPOINT", "https://message-queue.api.cloud.yandex.net"),
            inline_safety_margin_ms=int(os.getenv("INLINE_SAFETY_MARGIN_MS", "100")),
            worker_max_seconds=float(os.getenv("WORKER_MAX_SECONDS", "40")),
            app_log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
        )
