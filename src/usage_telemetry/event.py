"""Событие обращения и его разбор.

Состав полей выбран под один вопрос: каким приложением и каким его разделом
пользуются живые люди. Всё, что на этот вопрос не отвечает, сюда не попадает.

Чего здесь намеренно нет:

* строки запроса и тела — в query у нас ходят токены (разовые ссылки
  обнулятора, ключи абонентского приложения), и хранилище с ретенцией
  шестьдесят дней не должно превращаться в базу утёкших секретов;
* имени и почты пользователя — это метрика приложений, а не сотрудников,
  поэтому наружу идёт только идентификатор из той авторизации, которая в
  приложении уже есть.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Виды аутентификации. Робота от человека на входе не отделяем: пишем, чем
# запрос доказал своё право, а разделяем уже при разборе. Ошибка в эвристике
# на входе молча выбросила бы живых пользователей, и мы бы об этом не узнали.
AUTH_SESSION = "session"  # человек в браузере: сессия в Redis, cookie
AUTH_JWT = "jwt"  # виджет amoCRM: токен от widget_auth
AUTH_TELEGRAM = "telegram"  # бот или Mini App: подписанный initData
AUTH_API_KEY = "api_key"  # сервис к сервису
AUTH_BASIC = "basic"  # админки под basic auth
AUTH_NONE = "none"  # приложение без авторизации либо запрос до входа

SOURCE_HTTP = "http"
SOURCE_BOT = "bot"
SOURCE_NGINX = "nginx"


@dataclass(slots=True)
class Event:
    """Одно обращение к приложению."""

    at: float
    """Момент обращения, unix-время с дробной частью."""

    service: str
    """Машинный код сервиса, человеческое имя подставляет дашборд."""

    source: str
    """Откуда пришло событие: http, bot или разбор журнала nginx."""

    method: str
    """Метод HTTP либо тип обновления Telegram (message, callback)."""

    path: str
    """Шаблон пути, а не сырой адрес: /api/deals/{id}, не /api/deals/451."""

    status: int
    """Код ответа. Для ботов — 200 при успехе, 500 при исключении."""

    duration_ms: int
    """Длительность. Заодно отвечает, не тормозит ли приложение."""

    auth: str = AUTH_NONE
    user_id: str | None = None
    session_id: str | None = None
    user_agent: str | None = None

    client_ip: str | None = None
    """Адрес клиента. Нужен там, где приложение не опознаёт человека само:
    коллектор превращает его в отпечаток и сам адрес не сохраняет. Дальше
    события с этим полем не уезжают."""

    def as_payload(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 3),
            "service": self.service,
            "source": self.source,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "auth": self.auth,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "user_agent": self.user_agent,
            "client_ip": self.client_ip,
        }


_NUMERIC = re.compile(r"^\d+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_LONG_OPAQUE = re.compile(r"^[0-9a-zA-Z_-]{24,}$")


def normalize_path(path: str, *, max_segments: int = 12) -> str:
    """Свести сырой путь к шаблону.

    Нужно там, где шаблон маршрута недоступен: запрос не попал ни в один
    маршрут (404), приложение отдаёт статику само, событие разобрано из
    журнала nginx. Без этого каждая карточка сделки становится отдельной
    строкой в дашборде, и группировка перестаёт что-либо показывать.

    Длинные непрозрачные сегменты сводятся к {token} не только ради
    группировки: именно так выглядят разовые ссылки презентаций обнулятора,
    и попадать в хранилище целиком они не должны.
    """
    if not path:
        return "/"
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = path.split("/")
    out: list[str] = []
    for part in parts[:max_segments + 1]:
        if not part:
            out.append(part)
        elif _NUMERIC.match(part):
            out.append("{id}")
        elif _UUID.match(part):
            out.append("{uuid}")
        elif _LONG_OPAQUE.match(part) and not part.isalpha():
            out.append("{token}")
        else:
            out.append(part)
    normalized = "/".join(out)
    if len(parts) > max_segments + 1:
        normalized += "/…"
    return normalized or "/"
