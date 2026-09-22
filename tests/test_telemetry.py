"""Проверки пакета. Запуск: python tests/test_telemetry.py

Написаны без pytest, чтобы прогоняться там, где его нет, — на серверах при
раскатке.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from fastapi import Depends, FastAPI, Request
from starlette.testclient import TestClient

from usage_telemetry import (
    AUTH_SESSION,
    Event,
    TelemetryClient,
    install_telemetry,
    mark_user,
    normalize_path,
)
from usage_telemetry.bot import BotTelemetryMiddleware

received: list[dict] = []

collector = FastAPI()


@collector.post("/events")
async def accept(payload: dict) -> dict:
    received.extend(payload["events"])
    return {"accepted": len(payload["events"])}


def make_client(**kwargs) -> TelemetryClient:
    client = TelemetryClient(
        url="http://collector",
        service="test-service",
        flush_seconds=0.05,
        batch_size=10,
        **kwargs,
    )
    client._http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=collector), base_url="http://collector"
    )
    return client


def build_app(client: TelemetryClient) -> FastAPI:
    app = FastAPI()

    def current_user(request: Request) -> int:
        mark_user(request, user_id=3157, auth=AUTH_SESSION, session_id="sess-1")
        return 3157

    @app.get("/api/deals/{deal_id}")
    async def deal(deal_id: int, user: int = Depends(current_user)) -> dict:
        return {"deal": deal_id, "user": user}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/static/app.js")
    async def static_file() -> dict:
        return {"js": True}

    install_telemetry(app, client=client, sentry=False)
    return app


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ок: {message}")


def test_http_events() -> None:
    print("HTTP-события")
    received.clear()
    client = make_client()
    app = build_app(client)

    with TestClient(app) as http:
        assert http.get("/api/deals/451").status_code == 200
        assert http.get("/api/deals/452").status_code == 200
        assert http.get("/health").status_code == 200
        assert http.get("/static/app.js").status_code == 200
        assert http.options("/api/deals/451").status_code in (405, 200)
        # Событий ждём после закрытия приложения: отправка фоновая.

    paths = [event["path"] for event in received]
    check(paths.count("/api/deals/{deal_id}") == 2, "две сделки схлопнулись в один шаблон")
    check("/health" not in paths, "проверка живости не пишется")
    check("/static/app.js" not in paths, "статика не пишется")
    check(len(received) == 2, f"лишних событий нет (пришло {len(received)})")

    event = received[0]
    check(event["service"] == "test-service", "имя сервиса проставлено")
    check(event["user_id"] == "3157", "идентификатор пользователя из зависимости")
    check(event["auth"] == AUTH_SESSION, "вид аутентификации от приложения")
    check(event["session_id"] == "sess-1", "идентификатор сессии проставлен")
    check(event["status"] == 200, "код ответа записан")
    check(event["duration_ms"] >= 0, "длительность записана")


def test_unknown_route_normalized() -> None:
    print("Путь без маршрута")
    received.clear()
    client = make_client()
    app = build_app(client)
    with TestClient(app) as http:
        assert http.get("/api/unknown/98765").status_code == 404
    check(received[0]["path"] == "/api/unknown/{id}", "несуществующий путь нормализован")
    check(received[0]["status"] == 404, "код 404 записан")


def test_collector_down_does_not_break_app() -> None:
    print("Коллектор недоступен")
    received.clear()
    client = TelemetryClient(url="http://127.0.0.1:1", service="test-service", flush_seconds=0.05, timeout=0.2)
    app = build_app(client)
    with TestClient(app) as http:
        for _ in range(5):
            check_response = http.get("/api/deals/1")
            assert check_response.status_code == 200
    check(True, "приложение отвечает, пока коллектор лежит")
    check(client.stats["sent"] == 0, "ничего не отправлено")


def test_queue_overflow_drops_everything() -> None:
    print("Переполнение очереди")

    async def scenario() -> TelemetryClient:
        client = TelemetryClient(url="http://127.0.0.1:1", service="test-service", queue_size=3)
        client.enabled = True
        for index in range(10):
            client.submit(
                Event(
                    at=0.0,
                    service="test-service",
                    source="http",
                    method="GET",
                    path=f"/{index}",
                    status=200,
                    duration_ms=1,
                )
            )
        await asyncio.sleep(0)
        return client

    client = asyncio.run(scenario())
    check(client.stats["queued"] <= 3, "очередь не растёт сверх предела")
    check(client.stats["dropped"] > 0, "потери посчитаны")


def test_normalize_path() -> None:
    print("Нормализация путей")
    check(normalize_path("/api/deals/451") == "/api/deals/{id}", "число становится {id}")
    check(
        normalize_path("/presentation/9f1c2d3e4b5a6978c0d1e2f3a4b5c6d7") == "/presentation/{token}",
        "разовый токен не попадает в хранилище",
    )
    check(
        normalize_path("/o/1f7a4b2c-3d4e-5f60-8a9b-0c1d2e3f4a5b") == "/o/{uuid}",
        "uuid становится {uuid}",
    )
    check(normalize_path("/api/deals?token=secret") == "/api/deals", "строка запроса отброшена")


def test_bot_paths() -> None:
    print("Разбор обновлений бота")
    middleware = BotTelemetryMiddleware(client=make_client())

    class Fake:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.__dict__.setdefault("text", None)
            self.__dict__.setdefault("caption", None)

    message_path = middleware._message_path(Fake(text="/start beeline"))
    check(message_path == "/start beeline", "метка источника сохранена")
    token_path = middleware._message_path(Fake(text="/start AAbbCCddEEffGGhhIIjjKK112233"))
    check(token_path == "/start {arg}", "длинный аргумент обезличен")
    check(middleware._message_path(Fake(text="/cancel")) == "/cancel", "команда без аргумента")
    check(middleware._message_path(Fake(text="просто текст")) == "text", "обычное сообщение")
    check(middleware._message_path(Fake(photo=[1])) == "photo", "вложение опознано")
    check(middleware._callback_path("service:1842") == "service:{id}", "кнопки схлопнулись")
    check(middleware._callback_path("report:submit") == "report:submit", "смысловая кнопка сохранена")
    check(middleware._callback_path(None) == "callback", "кнопка без данных")


def test_disabled_client_is_silent() -> None:
    print("Выключенная телеметрия")
    received.clear()
    client = TelemetryClient(url="", service="", enabled=False)
    app = build_app(client)
    with TestClient(app) as http:
        assert http.get("/api/deals/1").status_code == 200
    check(not received, "ничего не собрано при выключенном сборе")


def main() -> int:
    tests = [
        test_http_events,
        test_unknown_route_normalized,
        test_collector_down_does_not_break_app,
        test_queue_overflow_drops_everything,
        test_normalize_path,
        test_bot_paths,
        test_disabled_client_is_silent,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failed += 1
            print(f"  ПРОВАЛ: {error}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА: {type(error).__name__}: {error}")
    print()
    print("провалов нет" if not failed else f"провалов: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
