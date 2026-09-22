"""Встраивание в приложение одной строкой."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable, Iterable

from .asgi import UsageTelemetryMiddleware
from .client import TelemetryClient, get_client
from .sentry import init_sentry

log = logging.getLogger("usage_telemetry")


def install_telemetry(
    app: Any,
    *,
    client: TelemetryClient | None = None,
    identify: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    skip_paths: Iterable[str] = (),
    skip_prefixes: Iterable[str] = (),
    sentry: bool = True,
) -> TelemetryClient:
    """Повесить сбор на приложение FastAPI или Starlette.

    Заодно поднимает отправку ошибок, если задан `SENTRY_DSN`: пакет всё равно
    приезжает во все проекты, а четверть из них сейчас молчит в GlitchTip.
    Отключается параметром `sentry=False` там, где SDK уже поднят своим кодом.

    Остановка отправщика вешается на выключение приложения — при перезапуске
    контейнера накопленное успевает уйти.
    """
    client = client or get_client()
    if sentry:
        init_sentry(service=client.service)

    app.add_middleware(
        UsageTelemetryMiddleware,
        client=client,
        identify=identify,
        skip_paths=skip_paths,
        skip_prefixes=skip_prefixes,
    )
    _attach_shutdown(app, client)
    return client


def _attach_shutdown(app: Any, client: TelemetryClient) -> bool:
    """Отправить накопленное при остановке приложения.

    Способов два, и версия Starlette определяет, какой доступен. В Starlette
    1.x `add_event_handler` и `on_shutdown` удалены, остался только
    `lifespan`, поэтому основной путь — обернуть существующий контекст
    жизненного цикла. В парке есть проекты и на более старых версиях, для них
    оставлен прежний способ.

    Не молчим при неудаче: без этой привязки телеметрия продолжит работать, но
    последняя пачка будет теряться при каждом перезапуске контейнера — а это
    ровно то, что однажды прочтут как «приложением перестали пользоваться».
    """
    router = getattr(app, "router", None)

    if router is not None and hasattr(router, "lifespan_context"):
        previous = router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan_with_telemetry(application: Any):
            async with previous(application) as state:
                try:
                    yield state
                finally:
                    await client.aclose()

        router.lifespan_context = lifespan_with_telemetry
        return True

    if hasattr(app, "add_event_handler"):
        app.add_event_handler("shutdown", client.aclose)
        return True

    log.warning(
        "usage_telemetry: не удалось привязать остановку отправщика — "
        "последняя пачка событий будет теряться при перезапуске"
    )
    return False
