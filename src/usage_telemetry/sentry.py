"""Инициализация GlitchTip.

Лежит здесь, потому что ту же строку `init_sentry()` двадцать пять проектов
уже скопировали себе по одной на каждый, а двадцать один проект из сорока
одного в GlitchTip просто молчит — SDK им никто не добавил. Раз пакет всё
равно приезжает во все проекты, пусть чинит и это.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("usage_telemetry")


def init_sentry(
    *,
    dsn: str | None = None,
    service: str | None = None,
    environment: str | None = None,
    traces_sample_rate: float = 0.0,
) -> bool:
    """Поднять отправку ошибок. Возвращает True, если получилось.

    Без DSN молчит и возвращает False: это нормальный режим для локального
    запуска, а не ошибка.
    """
    dsn = (dsn or os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        log.warning("usage_telemetry: SENTRY_DSN задан, но sentry-sdk не установлен")
        return False

    service = (service or os.getenv("TELEMETRY_SERVICE") or "").strip() or None
    environment = (environment or os.getenv("SENTRY_ENVIRONMENT") or "production").strip()

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=os.getenv("SENTRY_RELEASE") or None,
        traces_sample_rate=traces_sample_rate,
    )
    if service:
        sentry_sdk.set_tag("service", service)
    return True
