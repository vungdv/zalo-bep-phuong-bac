"""Orchestrator with fake colleagues (design §1, §11; task T14)."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any, get_args

import pytest

from order_extractor.config import Settings
from order_extractor.events import (
    AnyEvent,
    BrowserDied,
    Conversation,
    Event,
    LoginRequired,
    Message,
    MessagesChecked,
    NewMessages,
    NewOrder,
    NoChatOpen,
    OrderRecorded,
    RecorderBlocked,
    RecordFailed,
    SelectorDrift,
    ZaloReady,
)
from order_extractor.orchestrator import (
    DAY_MS,
    EXIT_BROWSER_DIED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_RECORDER_BLOCKED,
    Orchestrator,
    epoch_ms,
)
from order_extractor.status import LOGIN_PROMPT, NO_CHAT_PROMPT, StatusPrinter
from order_extractor.telemetry import current_tick_id

T0 = 1_800_000_000_000


def message(n: int, *, key: str = "g1") -> Message:
    return Message(conversation_key=key, message_key=f"{n}_1001", sent_at_ms=n, sender_uid="1001", text=f"{n}")


def new_order(n: int) -> NewOrder:
    return NewOrder(conversation_key="g1", message_key=f"{n}_1001", number=n, sent_at_ms=n)


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeZalo:
    """Records calls; `on_poll(n)` runs inside the n-th poll (1-based) and may notify or stop."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.polls = 0
        self.on_poll: Callable[[int], None] = lambda n: None
        self.checked: list[tuple[str, list[str]]] = []
        self.purged: list[int | None] = []
        self.poll_ticks: list[str | None] = []

    def open(self) -> None:
        self.calls.append("zalo.open")

    def poll(self) -> None:
        self.calls.append("zalo.poll")
        self.polls += 1
        self.poll_ticks.append(current_tick_id())
        self.on_poll(self.polls)

    def mark_checked(self, conversation_key: str, message_keys: Iterable[str]) -> int:
        self.calls.append("zalo.mark_checked")
        self.checked.append((conversation_key, list(message_keys)))
        return len(self.checked[-1][1])

    def recover(self) -> None:
        self.calls.append("zalo.recover")

    def purge(self, now_ms: int | None = None) -> object:
        self.calls.append("zalo.purge")
        self.purged.append(now_ms)
        return {}


class FakeExtractor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.handled: list[list[Message]] = []

    def handle(self, messages: Sequence[Message]) -> None:
        self.calls.append("extractor.handle")
        self.handled.append(list(messages))

    def recover(self) -> None:
        self.calls.append("extractor.recover")


class FakeRecorder:
    """`hang=True`: every job started by `kick()` never completes (a dead Sheets call)."""

    def __init__(self, calls: list[str], *, hang: bool = False) -> None:
        self.calls = calls
        self.hang = hang
        self.stored: list[NewOrder] = []
        self.kicks = 0
        self.jobs_started = 0
        self.purged: list[int | None] = []
        self.wait_timeouts: list[float | None] = []
        self.counts_calls = 0
        self._job: asyncio.Task[None] | None = None
        self._never = asyncio.Event()

    def store(self, order: NewOrder) -> int | None:
        self.calls.append("recorder.store")
        if any(o.message_key == order.message_key for o in self.stored):
            return None
        self.stored.append(order)
        return len(self.stored)

    def kick(self) -> None:
        self.calls.append("recorder.kick")
        self.kicks += 1
        if self.hang and (self._job is None or self._job.done()):
            self.jobs_started += 1
            self._job = asyncio.get_running_loop().create_task(self._never.wait())

    def recover(self) -> None:
        self.calls.append("recorder.recover")

    def purge(self, now_ms: int | None = None) -> int:
        self.calls.append("recorder.purge")
        self.purged.append(now_ms)
        return 0

    def counts(self) -> dict[str, int]:
        self.counts_calls += 1
        return {"NEW": 1, "RECORDED": 2, "FAILED": 3}

    @property
    def job_state(self) -> str:
        return "flushing" if self._job is not None and not self._job.done() else "idle"

    async def wait_idle(self, timeout: float | None = None) -> bool:
        self.wait_timeouts.append(timeout)
        if self._job is None or self._job.done():
            return True
        await asyncio.wait({self._job}, timeout=timeout)
        done = self._job.done()
        if not done:
            self._job.cancel()  # test hygiene only; the real recorder never cancels
        return done


class Harness:
    def __init__(self, *, hang: bool = False, sleep: Any = None, handle_signals: bool = False, **settings: Any):
        settings.setdefault("read_interval_seconds", 0.01)
        settings.setdefault("sheets_timeout_seconds", 0.05)
        self.settings = Settings(**settings)
        self.calls: list[str] = []
        self.out = io.StringIO()
        self.clock = Clock()
        self.orch = Orchestrator(
            self.settings,
            printer=StatusPrinter(self.out, tty=False),
            clock=self.clock,
            sleep=sleep,
            handle_signals=handle_signals,
        )
        self.zalo = FakeZalo(self.calls)
        self.extractor = FakeExtractor(self.calls)
        self.recorder = FakeRecorder(self.calls, hang=hang)
        self.orch.attach(self.zalo, self.extractor, self.recorder)

    def stop_after(self, polls: int) -> None:
        def on_poll(n: int) -> None:
            if n >= polls:
                self.orch.stop()

        self.zalo.on_poll = on_poll

    async def run(self, timeout: float = 5.0) -> int:
        return await asyncio.wait_for(self.orch.run(), timeout)


# --- routing ------------------------------------------------------------------------------------------------


def test_every_catalogue_event_has_a_route():
    union = get_args(AnyEvent)[0]
    assert set(Orchestrator._routes) == set(get_args(union))


def test_new_messages_go_to_the_extractor():
    h = Harness()
    batch = [message(1), message(2)]
    h.orch.notify(NewMessages(messages=batch))
    assert h.extractor.handled == [batch]
    assert h.orch.status.messages_read == 2


async def test_new_order_is_stored_then_kicked():
    h = Harness()
    h.orch.notify(new_order(7))
    h.orch.notify(new_order(7))  # duplicate: stored once, not counted twice
    assert h.calls == ["recorder.store", "recorder.kick", "recorder.store", "recorder.kick"]
    assert [o.number for o in h.recorder.stored] == [7]
    assert h.orch.status.orders_found == 1


def test_messages_checked_goes_to_zalo_mark_checked():
    h = Harness()
    h.orch.notify(MessagesChecked(conversation_key="g9", message_keys=["1_1", "2_1"]))
    assert h.zalo.checked == [("g9", ["1_1", "2_1"])]


def test_page_state_events_update_the_status_line():
    h = Harness()
    h.orch.notify(LoginRequired())
    assert LOGIN_PROMPT in h.out.getvalue().splitlines()[-1]
    h.orch.notify(NoChatOpen())
    assert NO_CHAT_PROMPT in h.out.getvalue().splitlines()[-1]
    h.orch.notify(ZaloReady(conversation=Conversation(key="g1", title="Nhóm khách sỉ")))
    assert "reading · Nhóm khách sỉ" in h.out.getvalue().splitlines()[-1]
    h.orch.notify(ZaloReady(conversation=Conversation(key="u42")))
    assert "reading · u42" in h.out.getvalue().splitlines()[-1]


def test_selector_drift_is_logged_and_flagged(caplog):
    h = Harness()
    with caplog.at_level("WARNING", logger="order_extractor"):
        h.orch.notify(SelectorDrift(reason="0 parsed", snapshot_path="data/snapshots/1.yml"))
    assert h.orch.status.drift is True
    assert any(r.message == "selector_drift" and r.levelname == "WARNING" for r in caplog.records)
    h.orch.notify(NewMessages(messages=[message(1)]))
    assert h.orch.status.drift is False


def test_order_recorded_and_record_failed_refresh_counts(caplog):
    h = Harness()
    h.orch.notify(OrderRecorded(order_id=1, sheet_row=2))
    assert h.recorder.counts_calls == 1
    assert "recorded 2 · failed 3 · pending 1" in h.out.getvalue()
    with caplog.at_level("WARNING", logger="order_extractor"):
        h.orch.notify(RecordFailed(order_ids=[1], error="503", next_attempt_at_ms=T0))
        h.orch.notify(RecordFailed(order_ids=[2], error="503", final=True))
    levels = {r.message: r.levelname for r in caplog.records}
    assert levels == {"record_failed": "WARNING", "record_failed_final": "ERROR"}
    assert h.recorder.counts_calls == 3
    assert not h.orch.stopping


def test_browser_died_with_retry_keeps_running():
    h = Harness()
    h.orch.notify(BrowserDied(attempt=1, max_attempts=3, will_retry=True))
    assert not h.orch.stopping
    assert "browser restarting (1/3)" in h.out.getvalue()


def test_browser_died_without_retry_stops_non_zero():
    h = Harness()
    h.orch.notify(BrowserDied(attempt=4, max_attempts=3, will_retry=False))
    assert h.orch.stopping
    assert h.orch.exit_code == EXIT_BROWSER_DIED


def test_recorder_blocked_stops_non_zero():
    h = Harness()
    h.orch.notify(RecorderBlocked(reason="header mismatch"))
    assert h.orch.stopping
    assert h.orch.exit_code == EXIT_RECORDER_BLOCKED


def test_first_fatal_exit_code_wins():
    h = Harness()
    h.orch.notify(RecorderBlocked(reason="header mismatch"))
    h.orch.notify(BrowserDied(attempt=4, max_attempts=3, will_retry=False))
    h.orch.stop()
    assert h.orch.exit_code == EXIT_RECORDER_BLOCKED


def test_unknown_event_is_ignored(caplog):
    class Strange(Event):
        type: str = "Strange"

    h = Harness()
    with caplog.at_level("WARNING", logger="order_extractor"):
        h.orch.notify(Strange())
    assert any(r.message == "event_unrouted" for r in caplog.records)


# --- run loop -------------------------------------------------------------------------------------------------


async def test_startup_recovers_each_colleague_purges_then_opens():
    h = Harness()
    h.stop_after(1)
    assert await h.run() == EXIT_OK
    assert h.calls[:6] == [
        "zalo.recover",
        "extractor.recover",
        "recorder.recover",
        "zalo.purge",
        "recorder.purge",
        "zalo.open",
    ]
    assert h.calls[6] == "zalo.poll"
    assert h.zalo.purged == [T0] and h.recorder.purged == [T0]
    assert h.recorder.wait_timeouts == [h.settings.sheets_timeout_seconds]


async def test_recovered_messages_flow_through_the_extractor_at_startup():
    h = Harness()
    h.zalo.recover = lambda: h.orch.notify(NewMessages(messages=[message(5)]))  # type: ignore[method-assign]
    h.stop_after(1)
    await h.run()
    assert h.extractor.handled == [[message(5)]]


async def test_each_tick_polls_then_kicks_with_its_own_tick_id():
    h = Harness()
    h.stop_after(3)
    assert await h.run() == EXIT_OK
    loop_calls = [c for c in h.calls if c in ("zalo.poll", "recorder.kick")]
    assert loop_calls == ["zalo.poll", "recorder.kick", "zalo.poll", "recorder.kick", "zalo.poll"]
    assert len(set(h.zalo.poll_ticks)) == 3
    assert None not in h.zalo.poll_ticks
    assert h.orch.ticks == 3


async def test_browser_died_without_retry_exits_non_zero_and_skips_the_kick():
    h = Harness()

    def on_poll(n: int) -> None:
        if n == 2:
            h.orch.notify(BrowserDied(attempt=4, max_attempts=3, will_retry=False))

    h.zalo.on_poll = on_poll
    assert await h.run() == EXIT_BROWSER_DIED
    assert h.zalo.polls == 2
    assert h.calls[-1] == "zalo.poll"
    assert h.recorder.wait_timeouts  # shutdown still waited for the Sheets job


async def test_recorder_blocked_from_the_background_job_exits_non_zero():
    h = Harness(read_interval_seconds=0.01)
    original_kick = h.recorder.kick

    def kick() -> None:
        original_kick()
        asyncio.get_running_loop().call_soon(h.orch.notify, RecorderBlocked(reason="header mismatch"))

    h.recorder.kick = kick  # type: ignore[method-assign]
    assert await h.run() == EXIT_RECORDER_BLOCKED
    assert h.zalo.polls == 1


async def test_a_job_that_never_completes_does_not_delay_poll():
    interval = 0.02
    h = Harness(hang=True, read_interval_seconds=interval, sheets_timeout_seconds=0.05)
    h.stop_after(6)
    started = time.monotonic()
    assert await h.run() == EXIT_OK
    elapsed = time.monotonic() - started
    assert h.zalo.polls == 6
    assert h.recorder.jobs_started == 1  # one call in flight at most; it never finished
    assert h.recorder.kicks == 5
    # 5 pauses of `interval` + the shutdown wait of SHEETS_TIMEOUT_SECONDS, never blocked by the job.
    assert elapsed < 5 * interval + 0.05 + 1.0
    assert h.recorder.wait_timeouts == [0.05]


async def test_ticks_are_paced_by_the_read_interval():
    sleeps: list[float] = []
    ticks = iter([0.0, 0.3, 1.0, 1.5, 5.0, 5.0])

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    h = Harness(sleep=sleep, read_interval_seconds=2.0)
    h.orch.monotonic = lambda: next(ticks)
    h.stop_after(3)
    await h.run()
    assert sleeps == [pytest.approx(1.7), pytest.approx(1.5)]


async def test_stop_interrupts_the_pause():
    h = Harness(read_interval_seconds=30)

    def on_poll(n: int) -> None:
        asyncio.get_running_loop().call_later(0.01, h.orch.stop)

    h.zalo.on_poll = on_poll
    started = time.monotonic()
    assert await h.run(timeout=5) == EXIT_OK
    assert time.monotonic() - started < 2
    assert h.zalo.polls == 1


async def test_daily_purge_runs_once_per_day_from_the_tick():
    h = Harness()

    def on_poll(n: int) -> None:
        h.clock.now += DAY_MS // 2
        if n >= 4:
            h.orch.stop()

    h.zalo.on_poll = on_poll
    await h.run()
    # startup at T0; the tick at T0 + 1 day purges again; T0 + 1.5 d does not.
    assert h.zalo.purged == [T0, T0 + DAY_MS]
    assert h.recorder.purged == [T0, T0 + DAY_MS]


async def test_unexpected_exception_exits_with_error(caplog):
    h = Harness()

    def on_poll(n: int) -> None:
        raise RuntimeError("boom")

    h.zalo.on_poll = on_poll
    with caplog.at_level("ERROR", logger="order_extractor"):
        assert await h.run() == EXIT_ERROR
    assert any(r.message == "orchestrator_crashed" for r in caplog.records)
    assert h.recorder.wait_timeouts


async def test_unfinished_job_at_shutdown_is_logged(caplog):
    h = Harness(hang=True)
    h.stop_after(2)
    with caplog.at_level("WARNING", logger="order_extractor"):
        assert await h.run() == EXIT_OK
    assert any(r.message == "sheets_job_unfinished" for r in caplog.records)


async def test_sigterm_stops_cleanly():
    h = Harness(handle_signals=True)

    def on_poll(n: int) -> None:
        if n == 2:
            os.kill(os.getpid(), signal.SIGTERM)

    h.zalo.on_poll = on_poll
    assert await h.run() == EXIT_OK
    assert h.zalo.polls == 2
    loop = asyncio.get_running_loop()
    assert not loop.remove_signal_handler(signal.SIGTERM)  # handler was removed on shutdown


async def test_stop_before_run_still_starts_up_and_shuts_down():
    h = Harness()
    h.orch.stop()
    assert await h.run() == EXIT_OK
    assert h.zalo.polls == 0
    assert "zalo.open" in h.calls


async def test_run_requires_attached_colleagues():
    orch = Orchestrator(Settings(), printer=StatusPrinter(io.StringIO(), tty=False), handle_signals=False)
    with pytest.raises(RuntimeError, match="attach"):
        await orch.run()


async def test_status_line_is_printed_each_tick_on_stdout_stream():
    h = Harness()
    h.stop_after(2)
    await h.run()
    lines = h.out.getvalue().splitlines()
    assert lines  # plain lines (not a TTY), only when the text changes
    assert lines[-1].startswith("Zalo: starting")
    assert "sheets: idle" in lines[-1]


def test_epoch_ms_is_close_to_now():
    assert abs(epoch_ms() - time.time() * 1000) < 1000
