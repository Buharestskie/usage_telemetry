"""Middleware для aiogram: считает обращения людей к боту.

Считается только входящее от человека. Рассылка по расписанию сюда не
попадает намеренно: число отправленных сообщений не говорит ничего о том,
нужен ли бот, — его читают или не читают молча, и Telegram прочтения ботам
не отдаёт.

Зависимости от aiogram у пакета нет: middleware в aiogram 3 — это любой
вызываемый объект с сигнатурой (handler, event, data), наследовать
`BaseMiddleware` не обязательно.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable

from .client import TelemetryClient, get_client
from .event import AUTH_TELEGRAM, SOURCE_BOT, Event

log = logging.getLogger("usage_telemetry")

# Метки источников короткие: beeline, megafon, yota. Всё длиннее — разовый
# токен приглашения, и в хранилище ему не место.
_ARG_SAFE = re.compile(r"^[a-zA-Z0-9_-]{1,16}$")


class BotTelemetryMiddleware:
    """Внешний middleware диспетчера: одно событие на обновление от человека."""

    def __init__(self, *, client: TelemetryClient | None = None) -> None:
        self.client = client or get_client()

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        if not self.client.enabled:
            return await handler(event, data)

        described = self._describe(event)
        if described is None:
            # Обновление не от человека: изменение прав, свой же пост в канал.
            return await handler(event, data)

        kind, path, user_id = described
        started = time.perf_counter()
        status = 200
        try:
            return await handler(event, data)
        except Exception:
            status = 500
            raise
        finally:
            try:
                self.client.submit(
                    Event(
                        at=time.time(),
                        service=self.client.service,
                        source=SOURCE_BOT,
                        method=kind,
                        path=path,
                        status=status,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        auth=AUTH_TELEGRAM,
                        user_id=str(user_id) if user_id is not None else None,
                        session_id=None,
                        user_agent=None,
                    )
                )
            except Exception:  # noqa: BLE001 — учёт не мешает боту
                log.debug("usage_telemetry: событие бота не собрано", exc_info=True)

    def _describe(self, event: Any) -> tuple[str, str, Any] | None:
        """Свести обновление к паре «тип, что именно нажали»."""
        message = getattr(event, "message", None)
        if message is not None:
            user = getattr(message, "from_user", None)
            return "message", self._message_path(message), getattr(user, "id", None)

        callback = getattr(event, "callback_query", None)
        if callback is not None:
            user = getattr(callback, "from_user", None)
            return "callback", self._callback_path(getattr(callback, "data", None)), getattr(user, "id", None)

        return None

    @staticmethod
    def _message_path(message: Any) -> str:
        """Команда с аргументом-меткой, иначе — вид вложения.

        Аргумент у `/start` важен: в ботах активации по нему приходит источник
        (beeline, megafon, yota), и это готовая разбивка по когортам. Берём
        его, только если он похож на метку, а не на разовый токен.
        """
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        text = text.strip()
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command = parts[0].split("@", 1)[0].lower()[:32]
            if len(parts) > 1:
                arg = parts[1].strip()
                if _ARG_SAFE.match(arg):
                    return f"{command} {arg}"
                return f"{command} {{arg}}"
            return command

        for attribute, name in (
            ("photo", "photo"),
            ("video", "video"),
            ("video_note", "video_note"),
            ("document", "document"),
            ("voice", "voice"),
            ("audio", "audio"),
            ("contact", "contact"),
            ("location", "location"),
            ("web_app_data", "web_app_data"),
        ):
            if getattr(message, attribute, None) is not None:
                return name
        return "text"

    @staticmethod
    def _callback_path(data: Any) -> str:
        """Свести данные кнопки к шаблону.

        `service:1842` и `service:1843` — одна и та же кнопка, и в дашборде
        они должны быть одной строкой, а не двумя тысячами.
        """
        if not isinstance(data, str) or not data:
            return "callback"
        head, sep, tail = data.partition(":")
        head = head[:48]
        if not sep:
            return head
        if tail.isdigit() or len(tail) > 16:
            return f"{head}:{{id}}"
        return f"{head}:{tail[:32]}"


def install_bot_telemetry(dispatcher: Any, *, client: TelemetryClient | None = None) -> BotTelemetryMiddleware:
    """Повесить сбор на диспетчер.

    Внешним middleware (`outer_middleware`), а не обычным: внешний вызывается
    до фильтров, поэтому в учёт попадут и те нажатия, которые не подошли ни
    одному обработчику. Это как раз интересный случай — человек нажал, а бот
    не ответил.
    """
    middleware = BotTelemetryMiddleware(client=client)
    dispatcher.update.outer_middleware(middleware)
    return middleware
