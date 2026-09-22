"""Сбор обращений к приложениям: кто, куда и когда ходил.

Пакет отвечает на один вопрос — каким приложением и каким его разделом
пользуются живые люди, — и устроен так, чтобы не влиять на работу приложения,
которое считает.

Встраивание в FastAPI:

    from usage_telemetry import install_telemetry

    app = FastAPI()
    install_telemetry(app)

В зависимости авторизации, там где сессия уже разобрана:

    from usage_telemetry import mark_user

    mark_user(request, user_id=session.amocrm_user_id, auth="session")

В боте:

    from usage_telemetry import install_bot_telemetry

    install_bot_telemetry(dispatcher)
"""

from .asgi import SCOPE_KEY, UsageTelemetryMiddleware, mark_user
from .bot import BotTelemetryMiddleware, install_bot_telemetry
from .client import TelemetryClient, get_client
from .event import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_JWT,
    AUTH_NONE,
    AUTH_SESSION,
    AUTH_TELEGRAM,
    SOURCE_BOT,
    SOURCE_HTTP,
    SOURCE_NGINX,
    Event,
    normalize_path,
)
from .install import install_telemetry
from .sentry import init_sentry

__all__ = [
    "AUTH_API_KEY",
    "AUTH_BASIC",
    "AUTH_JWT",
    "AUTH_NONE",
    "AUTH_SESSION",
    "AUTH_TELEGRAM",
    "SCOPE_KEY",
    "SOURCE_BOT",
    "SOURCE_HTTP",
    "SOURCE_NGINX",
    "BotTelemetryMiddleware",
    "Event",
    "TelemetryClient",
    "UsageTelemetryMiddleware",
    "get_client",
    "init_sentry",
    "install_bot_telemetry",
    "install_telemetry",
    "mark_user",
    "normalize_path",
]
