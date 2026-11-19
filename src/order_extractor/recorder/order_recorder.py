"""OrderRecorder colleague (design §6, §6.1, §6.2): stores orders and appends them to Google Sheets.

How the Orchestrator drives it (all calls on the event-loop thread):

* `store(order)` on `NewOrder` — synchronous SQLite commit, no network [R3.4].
* `kick()` every tick (and after `store`) — starts **at most one** background Sheets job and returns at once; the
  job is never awaited by the caller [R5.6, NFR1]. The first job is `_prepare` (header check [R5.3] + column-A
  reconciliation of NEW/FAILED [R5.5]); after that each job is a `_flush` of the due orders.
* `purge(now)` at startup and every 24 h [R6.3].
* `recover()` at startup — queues the reconciliation (it runs in the first job) [R3.5].
* `await wait_idle(SHEETS_TIMEOUT_SECONDS)` on shutdown.

Events: `OrderRecorded` (appended, or found in column A with `sheet_row=None`), `RecordFailed` (one per failed
append: retry scheduled, or `final=True` when the order became FAILED), `RecorderBlocked` (header mismatch; the
recorder then does nothing more).

The blocking gspread calls run via `asyncio.to_thread`; the coroutine resumes on the loop, so every SQLite write
and every `notify` stays on the loop thread. Clock and randomness are injected for deterministic tests.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol

from order_extractor.events import Colleague, Mediator, NewOrder, OrderRecorded, RecorderBlocked, RecordFailed
from order_extractor.recorder.orders_repo import Order, OrdersRepo, OrderStatus
from order_extractor.recorder.sheet import HEADER, Sheet, SheetsError
from order_extractor.telemetry import get_logger

if TYPE_CHECKING:
    from order_extractor.config import Settings

log = get_logger(__name__)


class Uniform(Protocol):
    """The part of `random.Random` the backoff jitter needs (injectable for tests)."""

    def uniform(self, a: float, b: float) -> float: ...


JobState = Literal["idle", "preparing", "flushing", "backoff", "blocked"]


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class OrderRecorder(Colleague):
    def __init__(
        self,
        mediator: Mediator,
        repo: OrdersRepo,
        sheet: Sheet,
        *,
        batch_max: int = 50,
        max_attempts: int = 5,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 300.0,
        retention_days: int = 30,
        clock: Callable[[], int] = wall_clock_ms,
        rng: Uniform | None = None,
    ) -> None:
        super().__init__(mediator)
        self.repo = repo
        self.sheet = sheet
        self.batch_max = batch_max
        self.max_attempts = max_attempts
        self.backoff_base_ms = backoff_base_seconds * 1000
        self.backoff_max_ms = backoff_max_seconds * 1000
        self.retention_days = retention_days
        self.clock = clock
        self.rng: Uniform = rng or random.Random()

        self.written: set[str] = set()  # column A as trimmed strings (§6.1)
        self._ready = False  # header checked and column A reconciled
        self._blocked = False
        self._job: asyncio.Task[None] | None = None
        self._job_kind: JobState = "idle"
        self._prepare_failures = 0
        self._prepare_not_before_ms = 0

    @classmethod
    def from_settings(
        cls,
        mediator: Mediator,
        repo: OrdersRepo,
        sheet: Sheet,
        settings: Settings,
        *,
        clock: Callable[[], int] = wall_clock_ms,
        rng: Uniform | None = None,
    ) -> OrderRecorder:
        return cls(
            mediator,
            repo,
            sheet,
            batch_max=settings.record_batch_max,
            max_attempts=settings.record_max_attempts,
            backoff_base_seconds=settings.record_backoff_base_seconds,
            backoff_max_seconds=settings.record_backoff_max_seconds,
            retention_days=settings.retention_days,
            clock=clock,
            rng=rng,
        )

    # --- synchronous API (called by the Orchestrator on the loop) -------------------------------------------

    def store(self, order: NewOrder) -> int | None:
        """Commit `order` as NEW, due at once; idempotent. Returns the new id, or `None` for a duplicate."""
        order_id = self.repo.store(order, self.clock())
        log.info(
            "order_stored" if order_id is not None else "order_duplicate",
            extra={"order_id": order_id, "conversation_key": order.conversation_key, "message_key": order.message_key},
        )
        return order_id

    def kick(self) -> None:
        """Start one background Sheets job if none is in flight and there is work. Never blocks; needs a loop."""
        if self._blocked or self.busy:
            return
        if not self._ready:
            if self.clock() < self._prepare_not_before_ms:
                return
            self._start(self._prepare(), "preparing")
            return
        due = self.repo.due(self.clock(), limit=self.batch_max)
        if due:
            self._start(self._flush(due), "flushing")

    def recover(self) -> None:
        """Queue column-A reconciliation of NEW and FAILED orders; it runs in the next job [R3.5, R5.5]."""
        if not self._blocked:
            self._ready = False
            self._prepare_not_before_ms = 0

    def purge(self, now_ms: int | None = None) -> int:
        """Drop copied content of RECORDED orders older than the retention window [R6.3]."""
        count = self.repo.purge(self.clock() if now_ms is None else now_ms, self.retention_days)
        log.info("orders_purged", extra={"count": count})
        return count

    def counts(self) -> dict[OrderStatus, int]:
        """Orders per status, for the status line."""
        return self.repo.counts()

    @property
    def busy(self) -> bool:
        return self._job is not None and not self._job.done()

    @property
    def blocked(self) -> bool:
        return self._blocked

    @property
    def job_state(self) -> JobState:
        """For the status line: blocked, preparing/flushing (job in flight), backoff (header check waiting), idle."""
        if self._blocked:
            return "blocked"
        if self.busy:
            return self._job_kind
        if not self._ready and self._prepare_failures:
            return "backoff"
        return "idle"

    async def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait up to `timeout` s for the in-flight job (never cancels it). Returns True if nothing is in flight."""
        job = self._job
        if job is None or job.done():
            return True
        await asyncio.wait({job}, timeout=timeout)
        return job.done()

    # --- background jobs -----------------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], kind: JobState) -> None:
        self._job_kind = kind
        self._job = asyncio.get_running_loop().create_task(self._guard(coro, kind))

    async def _guard(self, coro: Coroutine[Any, Any, None], kind: str) -> None:
        """Run a job; an unexpected error is logged instead of being lost in an unawaited task."""
        try:
            await coro
        except Exception:
            log.exception("recorder_job_crashed", extra={"job": kind})

    async def _prepare(self) -> None:
        """Header check [R5.3], then mark NEW/FAILED orders already in column A as RECORDED [R5.5]."""
        try:
            header = await asyncio.to_thread(self.sheet.read_header)
            if not header:
                await asyncio.to_thread(self.sheet.write_header)
                log.info("sheet_header_created")
            elif header != list(HEADER):
                self._block(f"sheet header mismatch: expected {list(HEADER)}, found {header}")
                return
            written = await asyncio.to_thread(self.sheet.read_order_ids)
        except SheetsError as exc:
            self._prepare_failures += 1
            delay = self._backoff_ms(self._prepare_failures)
            self._prepare_not_before_ms = self.clock() + delay
            log.warning(
                "sheet_prepare_failed",
                extra={"attempt": self._prepare_failures, "error": str(exc), "timeout": exc.timeout, "delay_ms": delay},
            )
            return
        self.written = written
        found = [o for o in self.repo.unrecorded() if str(o.id) in written]
        self.repo.mark_recorded_many(o.id for o in found)
        for o in found:
            log.info("order_reconciled", extra={"order_id": o.id, "previous_status": o.status})
            self.mediator.notify(OrderRecorded(order_id=o.id, sheet_row=None))
        self._ready = True
        self._prepare_failures = 0
        log.info("sheet_ready", extra={"reconciled": len(found), "column_a_ids": len(written)})

    async def _flush(self, due: Sequence[Order]) -> None:
        """Append the due orders in one call; re-read column A first if any is a retry [R5.5, R5.6]."""
        todo = list(due)
        try:
            if any(o.attempts > 0 for o in due):  # an earlier attempt may have landed (lost response)
                self.written = await asyncio.to_thread(self.sheet.read_order_ids)
            landed = [o for o in due if str(o.id) in self.written]
            for o in landed:
                self.repo.mark_recorded(o.id, None)
                log.info("order_reconciled", extra={"order_id": o.id, "attempt": o.attempts})
                self.mediator.notify(OrderRecorded(order_id=o.id, sheet_row=None))
            todo = [o for o in due if str(o.id) not in self.written]
            if not todo:
                return
            rows = await asyncio.to_thread(self.sheet.append_rows, todo)
        except SheetsError as exc:
            self._on_failure(todo, exc)
            return
        for o, row in zip(todo, rows, strict=True):
            self.repo.mark_recorded(o.id, row)
            self.written.add(str(o.id))
            log.info("order_recorded", extra={"order_id": o.id, "sheet_row": row, "attempt": o.attempts + 1})
            self.mediator.notify(OrderRecorded(order_id=o.id, sheet_row=row))

    def _on_failure(self, orders: Sequence[Order], exc: SheetsError) -> None:
        now = self.clock()
        error = str(exc)
        retry_ids: list[int] = []
        final_ids: list[int] = []
        next_at: int | None = None
        for o in orders:
            n = o.attempts + 1
            if n >= self.max_attempts:
                self.repo.mark_failed(o.id, attempts=n, error=error)
                final_ids.append(o.id)
                log.error("order_failed", extra={"order_id": o.id, "attempt": n, "error": error})
            else:
                at = now + self._backoff_ms(n)
                self.repo.schedule_retry(o.id, attempts=n, next_attempt_at_ms=at, error=error)
                retry_ids.append(o.id)
                next_at = at if next_at is None else min(next_at, at)
                log.warning(
                    "order_retry_scheduled",
                    extra={"order_id": o.id, "attempt": n, "next_attempt_at": at, "timeout": exc.timeout},
                )
        if retry_ids:
            self.mediator.notify(RecordFailed(order_ids=retry_ids, error=error, next_attempt_at_ms=next_at))
        if final_ids:
            self.mediator.notify(RecordFailed(order_ids=final_ids, error=error, final=True))

    def _backoff_ms(self, attempt: int) -> int:
        """Jittered exponential backoff: min(base * 2^(attempt-1), max) * U(0.5, 1.0), in ms [R5.6]."""
        delay = min(self.backoff_base_ms * 2 ** (attempt - 1), self.backoff_max_ms)
        return max(1, int(delay * self.rng.uniform(0.5, 1.0)))

    def _block(self, reason: str) -> None:
        self._blocked = True
        log.error("recorder_blocked", extra={"reason": reason})
        self.mediator.notify(RecorderBlocked(reason=reason))
