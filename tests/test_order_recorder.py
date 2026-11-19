"""OrderRecorder (design §6, §6.1, §6.2, §11) against `InMemorySheet`, a frozen clock and fixed jitter."""

from __future__ import annotations

import asyncio
import time

import pytest

from fake_sheet import FixedJitter, FrozenClock, InMemorySheet
from fakes import FakeMediator
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import Colleague, NewOrder, OrderRecorded, RecorderBlocked, RecordFailed
from order_extractor.recorder.order_recorder import OrderRecorder, wall_clock_ms
from order_extractor.recorder.orders_repo import DAY_MS, OrdersRepo
from order_extractor.recorder.sheet import HEADER

T0 = 1_800_000_000_000


def new_order(n: int, **kw) -> NewOrder:
    fields = {
        "conversation_key": "g1",
        "message_key": f"{T0 + n}_77",
        "number": n,
        "sent_at_ms": T0 + n,
        "conversation_title": "Nhóm khách sỉ",
        "sender": "Lan",
        "text": f"cho em {n} áo",
    }
    fields.update(kw)
    return NewOrder(**fields)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def repo() -> OrdersRepo:
    return OrdersRepo(open_database(":memory:"))


@pytest.fixture
def sheet() -> InMemorySheet:
    s = InMemorySheet(header=list(HEADER))
    yield s
    s.release()  # never leave a worker thread blocked


@pytest.fixture
def mediator() -> FakeMediator:
    return FakeMediator()


def make(mediator, repo, sheet, clock, **kw) -> OrderRecorder:
    kw.setdefault("max_attempts", 5)
    kw.setdefault("backoff_base_seconds", 2.0)
    kw.setdefault("backoff_max_seconds", 300.0)
    kw.setdefault("batch_max", 50)
    return OrderRecorder(mediator, repo, sheet, clock=clock, rng=FixedJitter(1.0), **kw)


async def run(recorder: OrderRecorder) -> None:
    """One tick's kick, then let the job finish (tests only; the Orchestrator never awaits it)."""
    recorder.kick()
    assert await recorder.wait_idle(2.0)


async def ready(recorder: OrderRecorder) -> None:
    """Run the startup job (header check + reconciliation)."""
    recorder.recover()
    await run(recorder)
    assert recorder.job_state == "idle"


# --- basics -------------------------------------------------------------------------------------------------


def test_is_colleague_and_store_is_idempotent(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    assert isinstance(recorder, Colleague)
    assert recorder.store(new_order(1)) == 1
    assert recorder.store(new_order(1)) is None
    assert recorder.counts() == {"NEW": 1, "RECORDED": 0, "FAILED": 0}
    assert sheet.calls == []  # store does no network I/O
    assert mediator.events == []


def test_from_settings_maps_config(mediator, repo, sheet):
    settings = Settings(
        record_batch_max=7,
        record_max_attempts=3,
        record_backoff_base_seconds=1.5,
        record_backoff_max_seconds=60,
        retention_days=10,
    )
    recorder = OrderRecorder.from_settings(mediator, repo, sheet, settings)
    assert (recorder.batch_max, recorder.max_attempts, recorder.retention_days) == (7, 3, 10)
    assert (recorder.backoff_base_ms, recorder.backoff_max_ms) == (1500, 60_000)
    assert abs(recorder.clock() - wall_clock_ms()) < 5_000


async def test_kick_without_work_starts_no_flush(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.kick()
    assert not recorder.busy
    assert await recorder.wait_idle(0.1)
    assert sheet.count("append_rows") == 0


# --- header check [R5.3] ------------------------------------------------------------------------------------


async def test_empty_header_is_created_then_rows_are_number_quantity_customer_message(mediator, repo, sheet, clock):
    sheet.header = []
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    assert sheet.header == list(HEADER)
    recorder.store(new_order(2, sent_at_ms=1_795_059_480_000))  # 2026-11-19 03:38 UTC
    await run(recorder)
    assert sheet.rows == [["1", 2, "Lan", "cho em 2 áo"]]
    assert mediator.events == [OrderRecorded(order_id=1, sheet_row=2)]
    assert repo.get(1).status == "RECORDED"
    assert repo.get(1).sheet_row == 2


async def test_header_mismatch_blocks_and_writes_nothing(mediator, repo, sheet, clock):
    sheet.header = ["Quantity", "Number", "Customer", "Message"]
    recorder = make(mediator, repo, sheet, clock)
    recorder.store(new_order(1))
    await ready_blocked(recorder)
    blocked = mediator.of_type(RecorderBlocked)
    assert len(blocked) == 1
    assert "mismatch" in blocked[0].reason and "'Quantity', 'Number'" in blocked[0].reason
    assert sheet.writes == []
    assert sheet.calls == ["read_header"]
    # blocked for good: no further jobs, recover() does not unblock
    recorder.recover()
    recorder.kick()
    assert not recorder.busy
    assert recorder.job_state == "blocked"
    assert repo.get(1).status == "NEW"


async def ready_blocked(recorder: OrderRecorder) -> None:
    recorder.recover()
    await run(recorder)
    assert recorder.blocked


async def test_matching_header_is_not_rewritten(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    assert sheet.calls == ["read_header", "read_order_ids"]
    assert sheet.writes == []


async def test_prepare_failure_backs_off_without_record_failed(mediator, repo, sheet, clock):
    sheet.fail("read_header", times=1, timeout=True)
    recorder = make(mediator, repo, sheet, clock)
    recorder.store(new_order(1))
    await run(recorder)
    assert recorder.job_state == "backoff"
    assert mediator.events == []
    recorder.kick()  # before the backoff delay (2 s with jitter 1.0) → nothing
    assert not recorder.busy
    clock.advance(2_000)
    await run(recorder)  # header check succeeds
    assert recorder.job_state == "idle"
    await run(recorder)  # flush
    assert mediator.events == [OrderRecorded(order_id=1, sheet_row=2)]


async def test_prepare_failure_in_read_order_ids_retries_whole_prepare(mediator, repo, sheet, clock):
    sheet.fail("read_order_ids")
    recorder = make(mediator, repo, sheet, clock)
    await run(recorder)
    clock.advance(10_000)
    await run(recorder)
    assert sheet.calls == ["read_header", "read_order_ids", "read_header", "read_order_ids"]
    assert recorder.job_state == "idle"


# --- normal path [R5.1, R5.7] -------------------------------------------------------------------------------


async def test_fresh_order_is_due_at_once_and_recorded_in_one_call(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    recorder.store(new_order(2))
    await run(recorder)  # no clock advance needed
    assert sheet.count("append_rows") == 1
    assert sheet.count("read_order_ids") == 1  # first attempts trust the cached set
    assert sheet.column_a == ["1", "2"]
    assert mediator.events == [OrderRecorded(order_id=1, sheet_row=2), OrderRecorded(order_id=2, sheet_row=3)]
    assert recorder.counts() == {"NEW": 0, "RECORDED": 2, "FAILED": 0}


async def test_batch_is_capped_at_batch_max(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock, batch_max=3)
    await ready(recorder)
    for n in range(1, 8):
        recorder.store(new_order(n))
    await run(recorder)
    assert sheet.column_a == ["1", "2", "3"]
    await run(recorder)
    await run(recorder)
    assert sheet.column_a == [str(n) for n in range(1, 8)]
    assert sheet.count("append_rows") == 3


# --- idempotency [R5.5] -------------------------------------------------------------------------------------


async def test_crash_after_append_no_duplicate_after_restart(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    recorder.store(new_order(2))
    # the append reached the sheet, then the process died before status = RECORDED was committed
    sheet.append_rows(repo.due(clock(), 50)[:1])
    assert repo.get(1).status == "NEW"

    restarted = make(mediator, repo, sheet, clock)
    await ready(restarted)
    assert mediator.events == [OrderRecorded(order_id=1, sheet_row=None)]
    await run(restarted)
    assert sheet.column_a == ["1", "2"]  # order 1 not appended twice
    assert repo.get(1).status == repo.get(2).status == "RECORDED"


async def test_append_lands_but_times_out_no_duplicate_on_retry(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    sheet.land_then_timeout()
    await run(recorder)
    failed = mediator.of_type(RecordFailed)
    assert failed == [RecordFailed(order_ids=[1], error=failed[0].error, next_attempt_at_ms=T0 + 2_000)]
    assert "timeout" in failed[0].error
    order = repo.get(1)
    assert (order.status, order.attempts, order.next_attempt_at_ms) == ("NEW", 1, T0 + 2_000)

    clock.advance(2_000)
    reads_before = sheet.count("read_order_ids")
    await run(recorder)
    assert sheet.count("read_order_ids") == reads_before + 1  # re-read before the retry
    assert sheet.count("append_rows") == 1  # not appended again
    assert sheet.column_a == ["1"]
    assert mediator.events[-1] == OrderRecorded(order_id=1, sheet_row=None)
    assert repo.get(1).status == "RECORDED"


async def test_retry_reread_splits_landed_and_missing_orders(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    sheet.land_then_timeout()
    await run(recorder)  # order 1 landed, response lost
    recorder.store(new_order(2))
    clock.advance(2_000)
    await run(recorder)  # due: 1 (retry) and 2 (fresh) → one re-read, one append of 2 only
    assert sheet.column_a == ["1", "2"]
    assert mediator.events[-2:] == [OrderRecorded(order_id=1, sheet_row=None), OrderRecorded(order_id=2, sheet_row=3)]


async def test_last_attempt_lands_but_times_out_failed_then_recorded_at_next_startup(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock, max_attempts=2)
    await ready(recorder)
    recorder.store(new_order(1))
    sheet.fail("append_rows")
    await run(recorder)  # attempt 1: plain failure
    clock.advance(2_000)
    sheet.land_then_timeout()
    await run(recorder)  # attempt 2 (last): lands, response lost
    assert repo.get(1).status == "FAILED"
    assert repo.get(1).attempts == 2
    final = mediator.of_type(RecordFailed)[-1]
    assert final.final and final.order_ids == [1] and final.next_attempt_at_ms is None

    clock.advance(DAY_MS)
    recorder.kick()  # FAILED is never retried automatically
    assert not recorder.busy

    mediator.clear()
    restarted = make(mediator, repo, sheet, clock, max_attempts=2)
    await ready(restarted)
    assert mediator.events == [OrderRecorded(order_id=1, sheet_row=None)]
    assert repo.get(1).status == "RECORDED"
    assert sheet.column_a == ["1"]


async def test_ids_compared_as_trimmed_strings(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    for n in range(1, 5):
        recorder.store(new_order(n))
    repo.mark_failed(3, attempts=5, error="old")
    # column A as staff/Sheets may leave it: padded text, a number, a FAILED order's id, a lookalike
    sheet.rows = [[" 1 "], [2], ["3"], ["41"], [""]]
    await ready(recorder)
    assert [e.order_id for e in mediator.of_type(OrderRecorded)] == [1, 2, 3]
    assert repo.get(4).status == "NEW"  # "41" is not "4"
    await run(recorder)
    assert sheet.column_a[-1] == "4"


async def test_recover_reconciles_failed_after_reprocess_like_reset(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    repo.mark_failed(1, attempts=5, error="lost")
    sheet.rows.append(["1"])  # it did land
    repo.reset_failed(1, clock())  # reprocess --order
    recorder.recover()  # next start
    await run(recorder)
    assert mediator.events == [OrderRecorded(order_id=1, sheet_row=None)]
    await run(recorder)
    assert sheet.column_a == ["1"]


# --- backoff and the cap [R5.6] -----------------------------------------------------------------------------


async def test_jittered_exponential_backoff_and_failed_at_cap(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock, max_attempts=4, backoff_base_seconds=2, backoff_max_seconds=5)
    recorder.rng = FixedJitter(0.0)  # jitter factor 0.5
    await ready(recorder)
    recorder.store(new_order(1))
    sheet.fail("append_rows", times=4)
    delays = []
    for _ in range(3):
        start = clock()
        await run(recorder)
        nxt = repo.get(1).next_attempt_at_ms
        delays.append(nxt - start)
        recorder.kick()  # not due yet → nothing
        assert not recorder.busy
        clock.now_ms = nxt
    assert delays == [1_000, 2_000, 2_500]  # 2 s·0.5, 4 s·0.5, min(8 s, 5 s)·0.5
    await run(recorder)
    order = repo.get(1)
    assert (order.status, order.attempts) == ("FAILED", 4)
    assert "503" in order.error
    events = mediator.of_type(RecordFailed)
    assert [e.final for e in events] == [False, False, False, True]
    assert sheet.count("append_rows") == 4
    assert sheet.count("read_order_ids") == 1 + 3  # startup + before each retry


async def test_mixed_batch_reports_retry_and_final_separately(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock, max_attempts=2)
    await ready(recorder)
    recorder.store(new_order(1))
    sheet.fail("append_rows")
    await run(recorder)  # order 1 → attempts 1
    clock.advance(2_000)
    recorder.store(new_order(2))
    sheet.fail("append_rows")
    mediator.clear()
    await run(recorder)
    assert mediator.events == [
        RecordFailed(order_ids=[2], error=mediator.events[0].error, next_attempt_at_ms=clock() + 2_000),
        RecordFailed(order_ids=[1], error=mediator.events[1].error, final=True),
    ]


async def test_failed_reread_counts_as_attempt(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    sheet.fail("append_rows")
    await run(recorder)
    clock.advance(2_000)
    sheet.fail("read_order_ids")
    await run(recorder)
    assert repo.get(1).attempts == 2
    assert sheet.count("append_rows") == 1


async def test_unexpected_error_is_logged_and_does_not_kill_the_recorder(mediator, repo, sheet, clock, caplog):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))

    def boom(orders):
        raise RuntimeError("bug")

    sheet.append_rows = boom
    await run(recorder)
    assert any(r.getMessage() == "recorder_job_crashed" for r in caplog.records)
    assert recorder.job_state == "idle"
    assert repo.get(1).status == "NEW"


class BrokenPipeMediator(FakeMediator):
    """Raises `BrokenPipeError` on the first `OrderRecorded` (e.g. the status line's stdout went away)."""

    def __init__(self) -> None:
        super().__init__()
        self.raised = False

    def notify(self, event) -> None:
        super().notify(event)
        if isinstance(event, OrderRecorded) and not self.raised:
            self.raised = True
            raise BrokenPipeError("stdout closed")


async def test_error_after_append_never_duplicates_rows(repo, sheet, clock, caplog):
    mediator = BrokenPipeMediator()
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    for n in range(1, 4):
        recorder.store(new_order(n))
    await run(recorder)
    assert any(r.getMessage() == "recorder_job_crashed" for r in caplog.records)
    assert sheet.column_a == ["1", "2", "3"]
    assert repo.counts() == {"NEW": 0, "RECORDED": 3, "FAILED": 0}  # all marked before the first notify
    assert [o.sheet_row for o in repo.orders_for_ids([1, 2, 3])] == [2, 3, 4]

    for _ in range(3):
        clock.advance(10_000)
        await run(recorder)
    assert sheet.column_a == ["1", "2", "3"]
    assert sheet.count("append_rows") == 1


async def test_unexpected_error_makes_the_next_flush_reread_column_a(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    real_append = sheet.append_rows

    def lands_then_breaks(orders):
        real_append(orders)
        raise RuntimeError("bug after the write")  # not a SheetsError: written/DB were never updated

    sheet.append_rows = lands_then_breaks
    await run(recorder)
    assert repo.get(1).status == "NEW" and repo.get(1).attempts == 0
    sheet.append_rows = real_append
    reads = sheet.count("read_order_ids")
    await run(recorder)
    assert sheet.count("read_order_ids") == reads + 1
    assert sheet.column_a == ["1"]  # found in column A: reconciled, not appended again
    assert repo.get(1).status == "RECORDED"
    assert mediator.of_type(OrderRecorded)[-1] == OrderRecorded(order_id=1, sheet_row=None)

    recorder.store(new_order(2))
    await run(recorder)
    assert sheet.count("read_order_ids") == reads + 1  # flag cleared after the successful re-read
    assert sheet.column_a == ["1", "2"]


# --- off-loop, one job in flight [R5.6, NFR1] ---------------------------------------------------------------


async def test_burst_with_sheets_hanging_one_job_and_kick_returns_immediately(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    sheet.hang("append_rows")
    for n in range(1, 51):
        recorder.store(new_order(n))

    polls = 0

    async def poll_loop():  # stands in for the Orchestrator's tick: poll() then kick()
        nonlocal polls
        for _ in range(20):
            polls += 1
            started = time.perf_counter()
            recorder.kick()
            assert time.perf_counter() - started < 0.05
            await asyncio.sleep(0.005)

    recorder.kick()
    job = recorder._job
    await poll_loop()
    assert polls == 20
    assert sheet.hanging.is_set()
    assert recorder._job is job  # still the same single job
    assert recorder.job_state == "flushing"
    assert sheet.count("append_rows") == 1
    assert await recorder.wait_idle(0.05) is False  # shutdown wait is bounded

    sheet.release()
    assert await recorder.wait_idle(2.0)
    assert len(mediator.of_type(OrderRecorded)) == 50
    assert sheet.column_a == [str(n) for n in range(1, 51)]


async def test_burst_with_sheets_down_one_call_carries_the_batch(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    for n in range(1, 51):
        recorder.store(new_order(n))
    sheet.fail("append_rows", timeout=True)
    started = time.perf_counter()
    recorder.kick()
    recorder.kick()
    recorder.kick()
    assert time.perf_counter() - started < 0.05
    assert await recorder.wait_idle(2.0)
    assert sheet.count("append_rows") == 1
    failed = mediator.of_type(RecordFailed)
    assert len(failed) == 1 and failed[0].order_ids == list(range(1, 51))
    recorder.kick()  # all backed off
    assert not recorder.busy


async def test_hanging_header_check_does_not_block_kick(mediator, repo, sheet, clock):
    sheet.hang("read_header")
    recorder = make(mediator, repo, sheet, clock)
    recorder.kick()
    await asyncio.sleep(0.02)
    assert recorder.job_state == "preparing"
    recorder.kick()
    assert sheet.count("read_header") == 1
    sheet.release()
    assert await recorder.wait_idle(2.0)


async def test_wait_idle_without_job_is_true(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock)
    assert await recorder.wait_idle(0)
    assert recorder.job_state == "idle"


# --- existing rows are never rewritten [R5.4] ---


async def test_existing_rows_never_written_after_append(mediator, repo, sheet, clock):
    sheet.header = []
    recorder = make(mediator, repo, sheet, clock)
    await ready(recorder)
    recorder.store(new_order(1))
    await run(recorder)
    sheet.rows[0].append(True)  # a human adds a cell to the right of the app's columns

    # more traffic: a retry that re-reads, a lost response, a restart with reconciliation
    recorder.store(new_order(2))
    sheet.land_then_timeout()
    await run(recorder)
    clock.advance(10_000)
    await run(recorder)
    restarted = make(mediator, repo, sheet, clock)
    await ready(restarted)
    restarted.store(new_order(3))
    await run(restarted)

    assert sheet.rows[0][4:] == [True]
    assert all(len(r) == 4 for r in sheet.rows[1:])
    # the only writes ever made: the header once, then appends of new rows below the existing ones
    assert sheet.writes == [("header", 1, 1), ("append", 2, 1), ("append", 3, 1), ("append", 4, 1)]


# --- purge [R6.3] -------------------------------------------------------------------------------------------


def test_purge_uses_clock_and_retention(mediator, repo, sheet, clock):
    recorder = make(mediator, repo, sheet, clock, retention_days=30)
    recorder.store(new_order(1, sent_at_ms=T0 - 31 * DAY_MS))
    recorder.store(new_order(2, sent_at_ms=T0 - 31 * DAY_MS))
    repo.mark_recorded(1, 2)
    assert recorder.purge() == 1
    assert repo.get(1).text is None
    assert repo.get(2).text is not None  # NEW keeps its copy
    assert recorder.purge(T0) == 0
