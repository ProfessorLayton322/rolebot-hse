from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class ApplicationMode(StrEnum):
    STANDBY = "standby"
    ACTIVE = "active"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class RuntimeGate:
    mode: ApplicationMode
    mutations_enabled: bool

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> RuntimeGate:
        values = os.environ if environment is None else environment
        raw_mode = values.get("APPLICATION_MODE", "standby")
        try:
            mode = ApplicationMode(raw_mode)
        except ValueError as exc:
            raise RuntimeError(f"invalid APPLICATION_MODE: {raw_mode}") from exc
        raw_mutations = values.get("MUTATIONS_ENABLED", "false").casefold()
        if raw_mutations not in {"true", "false"}:
            raise RuntimeError("MUTATIONS_ENABLED must be true or false")
        enabled = raw_mutations == "true"
        if mode is not ApplicationMode.ACTIVE and enabled:
            raise RuntimeError("mutations can be enabled only in active mode")
        return cls(mode=mode, mutations_enabled=enabled)

    @property
    def accepts_application_work(self) -> bool:
        return self.mode is ApplicationMode.ACTIVE and self.mutations_enabled


def runtime_controls_configured(environment: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environment is None else environment
    return "APPLICATION_MODE" in values or "MUTATIONS_ENABLED" in values
