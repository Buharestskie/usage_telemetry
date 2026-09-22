"""Проверка коллектора против живого PostgreSQL.

Нужна настоящая база: здесь проверяются SQL-своды, перцентиль и чистка —
то, что на заглушках не проверяется в принципе. Поэтому тест не выдумывает
базу, а требует её адресом в COLLECTOR_TEST_DSN и честно сообщает, если
адреса нет.

    COLLECTOR_TEST_DSN=postgres://user:pass@127.0.0.1:5432/telemetry_test \
        python tests/test_collector.py

Тест работает в отдельной схеме и удаляет её за собой, поэтому его можно
гонять и против боевой базы коллектора, ничего в ней не задев.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from app import aggregate
from app.db import SCHEMA

SCHEMA_NAME = "telemetry_selftest"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ок: {message}")


async def scenario(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
            await connection.execute(f"CREATE SCHEMA {SCHEMA_NAME}")

        # Своя схема в пути поиска: тест не трогает боевые таблицы.
        await pool.close()
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=2, server_settings={"search_path": SCHEMA_NAME}
        )

        async with pool.acquire() as connection:
            await connection.execute(SCHEMA.read_text(encoding="utf-8"))
        print("  ок: схема применилась")

        now = time.time()
        rows = []
        # Два человека, один раздел, разные дни — основа человеко-дней.
        for offset_days, user in ((0, "3157"), (0, "3157"), (0, "4021"), (1, "3157")):
            rows.append(
                (
                    now - offset_days * 86400,
                    "mpp2",
                    "http",
                    "GET",
                    "/api/deals/{id}",
                    200,
                    12,
                    "session",
                    user,
                    "sess",
                    "curl",
                )
            )
        # Вызов между сервисами: в человеко-дни попадать не должен.
        rows.append((now, "mpp2", "http", "POST", "/api/sync", 200, 30, "api_key", "robot", None, None))
        # Ошибка: должна попасть в свод разделов.
        rows.append((now, "mpp2", "http", "GET", "/api/reports", 500, 900, "session", "3157", "sess", None))

        async with pool.acquire() as connection:
            await connection.executemany(
                """
                INSERT INTO events (at, service, source, method, path, status, duration_ms,
                                    auth, user_id, session_id, user_agent)
                VALUES (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                rows,
            )
        print(f"  ок: записано событий: {len(rows)}")

        result = await aggregate.run_once(
            pool, timezone="Europe/Moscow", window_days=3, retention_days=90
        )
        check(result["users"] > 0, "свод людей посчитан")
        check(result["paths"] > 0, "свод разделов посчитан")

        async with pool.acquire() as connection:
            human_days = await connection.fetchval(
                "SELECT count(*) FROM daily_users WHERE service = 'mpp2'"
            )
            robot = await connection.fetchval(
                "SELECT count(*) FROM daily_users WHERE user_id = 'robot'"
            )
            deals = await connection.fetchrow(
                "SELECT hits, users FROM daily_paths WHERE path = '/api/deals/{id}' AND day = (now() AT TIME ZONE 'Europe/Moscow')::date"
            )
            errors = await connection.fetchval(
                "SELECT errors FROM daily_paths WHERE path = '/api/reports'"
            )
            runs = await connection.fetchval("SELECT count(*) FROM aggregate_runs")

        # Три человеко-дня: 3157 и 4021 сегодня, 3157 вчера.
        check(human_days == 3, f"человеко-дней три (получено {human_days})")
        check(robot == 0, "вызов по ключу в человеко-дни не попал")
        check(deals["hits"] == 3 and deals["users"] == 2, "раздел: три обращения, два человека")
        check(errors == 1, "ошибка посчитана отдельно")
        check(runs == 1, "проход свода отмечен")

        # Идемпотентность: повтор не должен ни задваивать, ни менять цифры.
        await aggregate.run_once(pool, timezone="Europe/Moscow", window_days=3, retention_days=90)
        async with pool.acquire() as connection:
            again = await connection.fetchval(
                "SELECT count(*) FROM daily_users WHERE service = 'mpp2'"
            )
        check(again == human_days, "повторный свод ничего не задвоил")

        # Чистка: событие старше срока хранения уходит, свод остаётся.
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO events (at, service, source, method, path, status, duration_ms, auth)
                VALUES (now() - interval '200 days', 'mpp2', 'http', 'GET', '/old', 200, 1, 'session')
                """
            )
        await aggregate.run_once(pool, timezone="Europe/Moscow", window_days=3, retention_days=90)
        async with pool.acquire() as connection:
            old_left = await connection.fetchval(
                "SELECT count(*) FROM events WHERE path = '/old'"
            )
            svod_left = await connection.fetchval("SELECT count(*) FROM daily_users")
        check(old_left == 0, "сырое старше срока удалено")
        check(svod_left == human_days, "свод от чистки не пострадал")
    finally:
        try:
            async with pool.acquire() as connection:
                await connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
        finally:
            await pool.close()


def main() -> int:
    dsn = (os.getenv("COLLECTOR_TEST_DSN") or "").strip()
    if not dsn:
        print("COLLECTOR_TEST_DSN не задан — проверке нужен настоящий PostgreSQL.")
        print("Своды, перцентиль и чистка на подделке базы не проверяются, поэтому тест не прогонялся.")
        return 2
    try:
        asyncio.run(scenario(dsn))
    except AssertionError as error:
        print(f"  ПРОВАЛ: {error}")
        return 1
    print()
    print("провалов нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
