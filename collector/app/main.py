"""Коллектор телеметрии: приём событий и своды.

Секрета на приёме нет намеренно. Он охранял бы право писать статистику
посещений — цена подделки этих данных примерно нулевая, а цена секрета —
двадцать пять мест, где он заведётся и разойдётся, при том что механизма
раздачи `.env` в проекты у нас не существует. Защита здесь другая: сервис
слушает внутренний интерфейс и наружу через nginx не отдаётся.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone as dt_timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field

from . import aggregate
from .config import Settings
from .db import Database, load_services

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("collector")

settings = Settings.from_env()
database = Database(settings.dsn)

INSERT_SQL = """
INSERT INTO events (at, service, source, method, path, status, duration_ms, auth, user_id, session_id, user_agent)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""


class EventIn(BaseModel):
    at: float
    service: str = Field(max_length=64)
    source: str = Field(max_length=16)
    method: str = Field(max_length=16)
    path: str = Field(max_length=512)
    status: int = Field(ge=0, le=999)
    duration_ms: int = Field(ge=0, le=3_600_000)
    auth: str = Field(max_length=16)
    user_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    user_agent: str | None = Field(default=None, max_length=256)


class Batch(BaseModel):
    events: list[EventIn] = Field(max_length=settings.max_batch)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    await load_services(database.pool, settings.services_file)
    task = asyncio.create_task(
        aggregate.loop(
            database.pool,
            timezone=settings.timezone,
            window_days=settings.aggregate_window_days,
            retention_days=settings.retention_days,
            interval_seconds=settings.aggregate_interval_seconds,
        ),
        name="aggregate",
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await database.close()


app = FastAPI(title="Коллектор телеметрии", lifespan=lifespan)


@app.post("/events")
async def accept(batch: Batch) -> dict[str, int]:
    """Принять пачку событий.

    Приложение-отправитель ошибку не переслушивает и пачку не повторяет —
    так задумано, поэтому здесь важно не отказывать зря: событие с кривым
    полем отбрасывается валидацией, остальная пачка проходит.
    """
    if not batch.events:
        return {"accepted": 0}

    rows = [
        (
            datetime.fromtimestamp(event.at, tz=dt_timezone.utc),
            event.service,
            event.source,
            event.method,
            event.path,
            event.status,
            event.duration_ms,
            event.auth,
            event.user_id,
            event.session_id,
            event.user_agent,
        )
        for event in batch.events
    ]
    seen = {event.service for event in batch.events}
    async with database.pool.acquire() as connection:
        async with connection.transaction():
            await connection.executemany(INSERT_SQL, rows)
            # Отметка нужна для вопроса «кто ещё ни разу не написал»: по ней
            # видно, раскатан ли сбор, а не только пользуются ли приложением.
            await connection.executemany(
                """
                INSERT INTO services (code, title, expected, seen_at)
                VALUES ($1, $1, false, now())
                ON CONFLICT (code) DO UPDATE SET seen_at = now()
                """,
                [(service,) for service in seen],
            )
    return {"accepted": len(rows)}


@app.get("/health")
async def health() -> dict[str, str]:
    """Проба для мониторинга: жив процесс и отвечает база."""
    async with database.pool.acquire() as connection:
        await connection.fetchval("SELECT 1")
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict[str, object]:
    """Короткая сводка для человека: что накоплено и когда был последний свод."""
    async with database.pool.acquire() as connection:
        events = await connection.fetchval("SELECT count(*) FROM events")
        services = await connection.fetchval("SELECT count(DISTINCT service) FROM events")
        oldest = await connection.fetchval("SELECT min(at) FROM events")
        last_run = await connection.fetchrow(
            "SELECT started_at, rows_users, rows_paths, deleted FROM aggregate_runs ORDER BY id DESC LIMIT 1"
        )
    return {
        "events": events,
        "services": services,
        "oldest": oldest.isoformat() if oldest else None,
        "last_aggregate": dict(last_run) if last_run else None,
    }
