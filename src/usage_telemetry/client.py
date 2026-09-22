"""Буфер событий и фоновая отправка в коллектор.

Главное свойство, ради которого всё устроено именно так: телеметрия не имеет
права влиять на работу приложения. Middleware стоит на пути каждого запроса во
всём парке, поэтому здесь нет ни одного места, где обращение пользователя
могло бы подождать телеметрию.

Из этого следует:

* отправка идёт в фоновой задаче, обработчик запроса только кладёт событие
  в очередь и сразу возвращается;
* при недоступности коллектора события выбрасываются молча — без повторов,
  без записи на диск, без роста очереди. Потеря куска статистики стоит ноль,
  лежащий из-за неё дашборд у РОПа — нет;
* переполнение очереди сбрасывает её целиком, а не ждёт места: очередь,
  которая растёт, — это утечка памяти в двадцати пяти сервисах сразу.

Выключатель — переменная окружения. Не запрос к коллектору за настройкой:
такой запрос создал бы ровно ту зависимость приложения от телеметрии, которую
здесь запрещено создавать, и перестал бы работать как раз тогда, когда
коллектор мёртв.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from .event import Event

log = logging.getLogger("usage_telemetry")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    log.warning("usage_telemetry: непонятное значение %s=%r, беру %s", name, raw, default)
    return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("usage_telemetry: непонятное значение %s=%r, беру %d", name, raw, default)
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("usage_telemetry: непонятное значение %s=%r, беру %s", name, raw, default)
        return default


class TelemetryClient:
    """Очередь событий и фоновый отправщик."""

    def __init__(
        self,
        *,
        url: str,
        service: str,
        enabled: bool = True,
        queue_size: int = 5000,
        batch_size: int = 200,
        flush_seconds: float = 5.0,
        timeout: float = 3.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.service = service
        self.enabled = bool(enabled and url and service)
        self.queue_size = queue_size
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.timeout = timeout

        # В очереди либо событие, либо None — сигнал «отправь накопленное и
        # выходи». Признаком простоя это не решается: пачка, уже вынутая
        # отправщиком, по очереди не видна, и отличить «ещё не начал» от
        # «уже всё отдал» снаружи нельзя — при остановке пачка терялась.
        self._queue: asyncio.Queue[Event | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None
        self._dropped = 0
        self._sent = 0
        self._last_complaint = 0.0

    @classmethod
    def from_env(cls) -> "TelemetryClient":
        """Собрать клиент из переменных окружения.

        Имя сервиса обязательно: событие без него бесполезно, а угадывать по
        имени процесса — верный способ получить в дашборде строку «python».
        """
        service = (os.getenv("TELEMETRY_SERVICE") or "").strip()
        url = (os.getenv("TELEMETRY_URL") or "").strip()
        enabled = env_flag("TELEMETRY_ENABLED", True)
        if enabled and not service:
            log.warning("usage_telemetry: TELEMETRY_SERVICE не задан, сбор выключен")
            enabled = False
        if enabled and not url:
            log.warning("usage_telemetry: TELEMETRY_URL не задан, сбор выключен")
            enabled = False
        return cls(
            url=url,
            service=service,
            enabled=enabled,
            queue_size=env_int("TELEMETRY_QUEUE_SIZE", 5000),
            batch_size=env_int("TELEMETRY_BATCH_SIZE", 200),
            flush_seconds=env_float("TELEMETRY_FLUSH_SECONDS", 5.0),
            timeout=env_float("TELEMETRY_TIMEOUT", 3.0),
        )

    # -- приём событий ----------------------------------------------------

    def submit(self, event: Event) -> None:
        """Поставить событие в очередь. Никогда не блокирует и не бросает."""
        if not self.enabled:
            return
        try:
            self._ensure_worker()
            queue = self._queue
            if queue is None:
                return
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop_everything()
        except Exception:  # noqa: BLE001 — телеметрия не роняет приложение
            log.debug("usage_telemetry: событие потеряно", exc_info=True)

    def _drop_everything(self) -> None:
        """Сбросить очередь целиком.

        Очередь переполняется в одном случае: коллектор не принимает, а
        запросы идут. Выбрасываем накопленное и продолжаем с чистого места —
        так объём памяти остаётся ограниченным, а в дашборде появляется
        честный разрыв вместо тихо растущего отставания.
        """
        queue = self._queue
        if queue is None:
            return
        lost = 0
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            lost += 1
        self._dropped += lost
        self._complain("очередь переполнена, сброшено %d событий" % lost)

    def _complain(self, message: str) -> None:
        """Пожаловаться в журнал не чаще раза в минуту.

        Когда коллектор недоступен, ошибка повторяется на каждом батче.
        Без ограничения телеметрия забьёт журнал того самого приложения,
        которому обещано не мешать.
        """
        now = time.monotonic()
        if now - self._last_complaint < 60:
            return
        self._last_complaint = now
        log.warning("usage_telemetry: %s (потеряно всего %d)", message, self._dropped)

    # -- фоновая отправка -------------------------------------------------

    def _ensure_worker(self) -> None:
        """Поднять очередь и задачу при первом событии.

        Лениво, а не в конструкторе: middleware создаётся при импорте модуля,
        когда цикла событий ещё нет.
        """
        if self._worker is not None and not self._worker.done():
            return
        loop = asyncio.get_running_loop()
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._worker = loop.create_task(self._run(), name="usage-telemetry")

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            try:
                batch, stop = await self._collect_batch()
                if batch:
                    await self._send(batch)
                if stop:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.debug("usage_telemetry: сбой отправщика", exc_info=True)
                await asyncio.sleep(1.0)

    async def _collect_batch(self) -> tuple[list[Event], bool]:
        """Дождаться первого события и добрать остальные до порога или срока.

        Вторым значением возвращает признак остановки: в очередь пришёл
        сигнал завершения, накопленное надо отдать и выйти.
        """
        assert self._queue is not None
        first = await self._queue.get()
        self._queue.task_done()
        if first is None:
            return [], True
        batch = [first]
        deadline = time.monotonic() + self.flush_seconds
        while len(batch) < self.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            self._queue.task_done()
            if event is None:
                return batch, True
            batch.append(event)
        return batch, False

    async def _send(self, batch: list[Event]) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout)
        payload = {"events": [event.as_payload() for event in batch]}
        try:
            response = await self._http.post(f"{self.url}/events", json=payload)
        except Exception as exc:  # noqa: BLE001 — сеть, DNS, таймаут
            self._dropped += len(batch)
            self._complain(f"коллектор недоступен ({type(exc).__name__}), пачка из {len(batch)} потеряна")
            return
        if response.status_code >= 400:
            self._dropped += len(batch)
            self._complain(f"коллектор ответил {response.status_code}, пачка из {len(batch)} потеряна")
            return
        self._sent += len(batch)

    async def aclose(self) -> None:
        """Остановить отправщик, отдав накопленное.

        Единственное место, где ожидание телеметрии оправдано: приложение уже
        останавливается, пользовательских запросов нет. Сигнал кладётся в ту
        же очередь, поэтому отправщик сначала доотправит всё, что перед ним,
        и только потом выйдет — гонки между остановкой и последней пачкой
        не остаётся. Не уложился в таймаут — снимаем силой, события того не
        стоят.
        """
        worker = self._worker
        self._worker = None
        queue = self._queue
        if worker is not None and not worker.done() and queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # Очередь забита, значит коллектор и так не принимает.
                # Освобождаем место под сигнал, накопленное уже не спасти.
                self._drop_everything()
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:  # pragma: no cover — место точно есть
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=self.timeout + self.flush_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001
                pass
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http = None

    @property
    def stats(self) -> dict[str, int]:
        """Отправлено и потеряно — для отладки и для проверки после раскатки."""
        queued = self._queue.qsize() if self._queue is not None else 0
        return {"sent": self._sent, "dropped": self._dropped, "queued": queued}


_default: TelemetryClient | None = None


def get_client() -> TelemetryClient:
    """Общий клиент процесса, собранный из окружения."""
    global _default
    if _default is None:
        _default = TelemetryClient.from_env()
    return _default
