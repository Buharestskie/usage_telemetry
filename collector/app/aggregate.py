"""Суточные своды и чистка сырых событий.

Своды пересчитываются не «за вчера», а за последние несколько суток целиком:
события приходят с задержкой, коллектор может простоять, и пересчёт
перекрытием избавляет от целого класса дыр, которые иначе пришлось бы
замечать глазами. Операция идемпотентна — повторный проход даёт тот же
результат.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger("collector.aggregate")

# Свод людей по дням. Считаются только обращения, за которыми стоит человек:
# вызовы по ключу между сервисами в вопрос «пользуются ли приложением» не
# входят. Разделение делается здесь, а не на входе, — по признаку, который
# приложение само сообщило.
HUMAN_AUTH = ("session", "jwt", "telegram", "basic")

USERS_SQL = """
INSERT INTO daily_users (day, service, auth, user_id, hits)
SELECT (at AT TIME ZONE $1)::date AS day,
       service,
       auth,
       user_id,
       count(*)::int
  FROM events
 WHERE at >= $2
   AND user_id IS NOT NULL
   AND auth = ANY($3::text[])
 GROUP BY 1, 2, 3, 4
ON CONFLICT (day, service, auth, user_id)
DO UPDATE SET hits = EXCLUDED.hits
"""

PATHS_SQL = """
INSERT INTO daily_paths (day, service, method, path, hits, users, errors, duration_p95)
SELECT (at AT TIME ZONE $1)::date AS day,
       service,
       method,
       path,
       count(*)::int,
       count(DISTINCT user_id)::int,
       count(*) FILTER (WHERE status >= 500)::int,
       coalesce(
           percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms)::int,
           0
       )
  FROM events
 WHERE at >= $2
 GROUP BY 1, 2, 3, 4
ON CONFLICT (day, service, method, path)
DO UPDATE SET hits = EXCLUDED.hits,
              users = EXCLUDED.users,
              errors = EXCLUDED.errors,
              duration_p95 = EXCLUDED.duration_p95
"""


async def run_once(pool: asyncpg.Pool, *, timezone: str, window_days: int, retention_days: int) -> dict[str, int]:
    """Пересчитать своды за окно и удалить сырьё старше срока хранения."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            since = await connection.fetchval(
                "SELECT date_trunc('day', now() AT TIME ZONE $1) - make_interval(days => $2)",
                timezone,
                window_days,
            )
            users = await connection.execute(USERS_SQL, timezone, since, list(HUMAN_AUTH))
            paths = await connection.execute(PATHS_SQL, timezone, since)
            deleted = await connection.execute(
                "DELETE FROM events WHERE at < now() - make_interval(days => $1)",
                retention_days,
            )
            rows_users = _affected(users)
            rows_paths = _affected(paths)
            rows_deleted = _affected(deleted)
            await connection.execute(
                "INSERT INTO aggregate_runs (days, rows_users, rows_paths, deleted) VALUES ($1, $2, $3, $4)",
                window_days,
                rows_users,
                rows_paths,
                rows_deleted,
            )
    result = {"users": rows_users, "paths": rows_paths, "deleted": rows_deleted}
    log.info("свод: людей %(users)d, разделов %(paths)d, удалено сырых %(deleted)d", result)
    return result


def _affected(status: str) -> int:
    """Число строк из ответа вида 'INSERT 0 42' или 'DELETE 7'."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


async def loop(
    pool: asyncpg.Pool,
    *,
    timezone: str,
    window_days: int,
    retention_days: int,
    interval_seconds: int,
) -> None:
    """Гонять свод по кругу, пока жив процесс.

    Сбой не гасит цикл: своды — не пользовательский путь, и разовая ошибка
    базы не повод оставлять систему без агрегатов до следующего перезапуска.
    """
    while True:
        try:
            await run_once(
                pool, timezone=timezone, window_days=window_days, retention_days=retention_days
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("свод не прошёл")
        await asyncio.sleep(interval_seconds)
