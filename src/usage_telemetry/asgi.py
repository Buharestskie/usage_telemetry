"""ASGI-middleware: одно событие на обращение к приложению.

Написан на голом ASGI, а не на `BaseHTTPMiddleware`, сознательно: последний
оборачивает каждый запрос в дополнительную задачу и ломает передачу
исключений, а стоять этому коду предстоит перед каждым запросом всех
приложений парка.

Шаблон пути берётся из маршрута, который поставил роутер, — поэтому событие
собирается ПОСЛЕ обработки запроса, когда `scope["route"]` уже заполнен.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

from .client import TelemetryClient, get_client
from .event import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_JWT,
    AUTH_NONE,
    AUTH_SESSION,
    SOURCE_HTTP,
    Event,
    normalize_path,
)

log = logging.getLogger("usage_telemetry")

SCOPE_KEY = "usage_telemetry"

# Что не пишем никогда.
#
# Статика и проверки живости — это четыре пятых объёма и ноль смысла: один
# заход на дашборд тянет десятки файлов, а /health дёргает мониторинг каждые
# тридцать секунд. OPTIONS — предполётный запрос браузера, он не обращение
# человека, а его технический предвестник.
SKIP_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/health/",
        "/metrics",
        "/metrics/",
        "/ping",
        "/favicon.ico",
        "/robots.txt",
    }
)
SKIP_PREFIXES = ("/static/", "/assets/", "/_next/", "/_app/")
SKIP_SUFFIXES = (
    ".js", ".mjs", ".css", ".map", ".ico", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm",
)


def default_identify(scope: dict[str, Any]) -> dict[str, Any]:
    """Определить вид аутентификации по заголовкам.

    Это запасной вариант, а не основной: идентификатор пользователя живёт в
    сессии или в токене, и разбирает их само приложение. Приложение сообщает
    разобранное через `mark_user`; пока оно этого не сделало, мы знаем хотя бы,
    чем запрос доказал своё право — а значит сумеем отделить человека от
    сервиса при разборе.
    """
    headers = {}
    for raw_name, raw_value in scope.get("headers") or ():
        try:
            headers[raw_name.decode("latin-1").lower()] = raw_value.decode("latin-1")
        except Exception:  # noqa: BLE001 — кривой заголовок не повод падать
            continue

    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return {"auth": AUTH_JWT}
    if auth.lower().startswith("basic "):
        return {"auth": AUTH_BASIC}
    if headers.get("x-api-key") or headers.get("x-auth-token"):
        return {"auth": AUTH_API_KEY}
    if headers.get("cookie"):
        return {"auth": AUTH_SESSION}
    return {"auth": AUTH_NONE}


class UsageTelemetryMiddleware:
    """Считает обращения к приложению и отдаёт их клиенту телеметрии."""

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        client: TelemetryClient | None = None,
        identify: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        skip_paths: Iterable[str] = (),
        skip_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.client = client or get_client()
        self.identify = identify or default_identify
        self.skip_paths = SKIP_PATHS | set(skip_paths)
        self.skip_prefixes = SKIP_PREFIXES + tuple(skip_prefixes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.client.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        method = scope.get("method") or "GET"
        if self._skip(method, path):
            await self.app(scope, receive, send)
            return

        # Сюда приложение кладёт разобранного пользователя через mark_user.
        # Свой ключ, а не scope["state"]: state принадлежит Starlette, и его
        # содержимое — не наше дело.
        marks: dict[str, Any] = {}
        scope[SCOPE_KEY] = marks

        status = 500
        started = time.perf_counter()

        async def send_wrapper(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            try:
                self._record(scope, method, path, status, duration_ms, marks)
            except Exception:  # noqa: BLE001 — учёт не мешает ответу
                log.debug("usage_telemetry: событие не собрано", exc_info=True)

    def _skip(self, method: str, path: str) -> bool:
        if method == "OPTIONS":
            return True
        if path in self.skip_paths:
            return True
        if path.startswith(self.skip_prefixes):
            return True
        return path.endswith(SKIP_SUFFIXES)

    def _record(
        self,
        scope: dict[str, Any],
        method: str,
        path: str,
        status: int,
        duration_ms: int,
        marks: dict[str, Any],
    ) -> None:
        identity = dict(self.identify(scope) or {})
        # Разобранное приложением перекрывает угаданное по заголовкам.
        identity.update({k: v for k, v in marks.items() if v is not None})

        event = Event(
            at=time.time(),
            service=self.client.service,
            source=SOURCE_HTTP,
            method=method,
            path=self._template(scope, path),
            status=status,
            duration_ms=duration_ms,
            auth=str(identity.get("auth") or AUTH_NONE),
            user_id=_as_str(identity.get("user_id")),
            session_id=_as_str(identity.get("session_id")),
            user_agent=_header(scope, b"user-agent"),
            client_ip=_client_ip(scope),
        )
        self.client.submit(event)

    @staticmethod
    def _template(scope: dict[str, Any], path: str) -> str:
        """Шаблон маршрута, если запрос куда-то попал, иначе — нормализованный путь."""
        route = scope.get("route")
        template = getattr(route, "path_format", None) or getattr(route, "path", None)
        if isinstance(template, str) and template:
            return template
        return normalize_path(path)


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name.lower() == name:
            try:
                return raw_value.decode("latin-1")[:200]
            except Exception:  # noqa: BLE001
                return None
    return None


def _client_ip(scope: dict[str, Any]) -> str | None:
    """Адрес клиента с поправкой на обратный прокси.

    Перед приложениями стоит nginx, поэтому адрес в соединении — его
    собственный. Настоящий приходит в заголовках, которые он проставляет;
    берём первый адрес цепочки, то есть исходного клиента.

    Само значение до хранилища не доезжает: коллектор считает по нему
    отпечаток и адрес отбрасывает.
    """
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first[:45]
    real = _header(scope, b"x-real-ip")
    if real:
        return real.strip()[:45]
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])[:45]
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:128] or None


def mark_user(
    request_or_scope: Any,
    *,
    user_id: Any = None,
    auth: str | None = None,
    session_id: Any = None,
) -> None:
    """Сообщить телеметрии, кого приложение опознало в этом запросе.

    Вызывается там, где приложение уже разобрало сессию или токен, — то есть
    в зависимости авторизации, одной строкой. Middleware к тому моменту уже
    отработал начало запроса, но событие собирает после ответа, поэтому
    увидит проставленное.

    Ничего не делает, если телеметрия выключена или запрос не попал под сбор.
    """
    scope = getattr(request_or_scope, "scope", request_or_scope)
    if not isinstance(scope, dict):
        return
    marks = scope.get(SCOPE_KEY)
    if marks is None:
        return
    if user_id is not None:
        marks["user_id"] = user_id
    if auth is not None:
        marks["auth"] = auth
    if session_id is not None:
        marks["session_id"] = session_id
