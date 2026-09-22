"""Настройки коллектора из окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    dsn: str
    timezone: str = "Europe/Moscow"
    retention_days: int = 90
    aggregate_window_days: int = 3
    aggregate_interval_seconds: int = 3600
    max_batch: int = 1000
    services_file: str = "/etc/telemetry/services.json"
    fingerprint_salt: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        dsn = (os.getenv("COLLECTOR_DSN") or "").strip()
        if not dsn:
            raise RuntimeError("COLLECTOR_DSN не задан: коллектору некуда писать события")
        return cls(
            dsn=dsn,
            timezone=os.getenv("COLLECTOR_TZ", "Europe/Moscow"),
            retention_days=int(os.getenv("COLLECTOR_RETENTION_DAYS", "90")),
            aggregate_window_days=int(os.getenv("COLLECTOR_AGGREGATE_WINDOW_DAYS", "3")),
            aggregate_interval_seconds=int(os.getenv("COLLECTOR_AGGREGATE_INTERVAL", "3600")),
            max_batch=int(os.getenv("COLLECTOR_MAX_BATCH", "1000")),
            services_file=os.getenv("COLLECTOR_SERVICES_FILE", "/etc/telemetry/services.json"),
            # Соль обязательна: без неё отпечаток — это хеш от адреса, а всё
            # пространство адресов перебирается за секунды, и «необратимость»
            # оказалась бы выдумкой.
            fingerprint_salt=(os.getenv("COLLECTOR_FINGERPRINT_SALT") or "").strip(),
        )
