"""Пул соединений и применение схемы."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import asyncpg

log = logging.getLogger("collector")

SCHEMA = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self.dsn, min_size=self.min_size, max_size=self.max_size, command_timeout=30
        )
        await self.apply_schema()

    async def apply_schema(self) -> None:
        """Применить схему. Все операции идемпотентны, можно на каждом старте."""
        assert self.pool is not None
        sql = SCHEMA.read_text(encoding="utf-8")
        async with self.pool.acquire() as connection:
            await connection.execute(sql)
        log.info("схема применена")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None


async def load_services(pool: asyncpg.Pool, path: str) -> int:
    """Загрузить справочник приложений из файла, разложенного Ansible.

    Записи не удаляются, а помечаются как неожидаемые: сервис, выключенный
    по итогам аудита, должен остаться в истории вместе со своими сводами,
    иначе через полгода никто не вспомнит, что он вообще был.
    """
    import json
    from pathlib import Path

    source = Path(path)
    if not source.exists():
        log.warning("справочник приложений %s не найден, имена будут машинными", path)
        return 0

    try:
        entries = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("справочник приложений %s не читается", path)
        return 0

    rows = [
        (str(entry["code"]).strip(), str(entry.get("title") or entry["code"]).strip())
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("code") or "").strip()
    ]
    if not rows:
        return 0

    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("UPDATE services SET expected = false")
            await connection.executemany(
                """
                INSERT INTO services (code, title, expected)
                VALUES ($1, $2, true)
                ON CONFLICT (code) DO UPDATE
                   SET title = EXCLUDED.title, expected = true
                """,
                rows,
            )
    log.info("справочник приложений: %d записей", len(rows))
    return len(rows)
