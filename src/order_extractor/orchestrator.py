"""Orchestrator: the mediator (design §1, §6.2, §7.1, §8; R3.1-R3.5, R6.3, R6.4, NFR1).

Everything runs on one asyncio loop thread:

* **Startup** (one tick id): `recover()` on each colleague (ZaloWeb re-emits unchecked messages, OrderRecorder
  queues column-A reconciliation) [R3.5], the retention purges [R6.3], then `zalo.open()` [R3.3].
* **Tick** every `READ_INTERVAL_SECONDS`, each with a new `tick_id`: `zalo.poll()` (synchronous, on the loop
  thread, so every SQLite write and `notify` stays on this thread), then `recorder.kick()`, then the daily purges.
  The rest of the interval is spent in `await`, which is when the recorder's background Sheets job resumes. The
  tick never awaits that job, so a slow or dead Sheets API cannot delay `poll()` [R5.6, NFR1].
* **Routing** (`notify`): events are dispatched synchronously, in order, per the §1 event catalogue.
* **Shutdown** on `stop()` (SIGINT/SIGTERM) or a fatal event (`BrowserDied(will_retry=False)`, `RecorderBlocked`):
  leave the loop, wait up to `SHEETS_TIMEOUT_SECONDS` for the in-flight Sheets job, and return the exit code.
  Anything unfinished is reconciled by `recover()` on the next start.

Colleagues are attached after construction (they need the Orchestrator as their mediator) and are typed by the
small protocols below, so tests can substitute fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import ClassVar, Protocol

from order_extractor.config import Settings
from order_extractor.events import (
    BrowserDied,
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
from order_extractor.status import LOGIN_PROMPT, NO_CHAT_PROMPT, Status, StatusPrinter
from order_extractor.telemetry import get_logger, start_tick

log = get_logger(__name__)

DAY_MS = 86_400_000

EXIT_OK = 0
EXIT_ERROR = 1  # unexpected exception
EXIT_BROWSER_DIED = 3  # BROWSER_MAX_RESTARTS consecutive relaunches failed [R1.5]
EXIT_RECORDER_BLOCKED = 4  # sheet header mismatch [R5.3]


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


class ZaloPort(Protocol):
    def open(self) -> None: ...
    def poll(self) -> None: ...
    def mark_checked(self, conversation_key: str, message_keys: Iterable[str]) -> int: ...
    def recover(self) -> None: ...
    def purge(self, now_ms: int | None = None) -> object: ...


class ExtractorPort(Protocol):
    def handle(self, messages: Sequence[Message]) -> None: ...
    def recover(self) -> None: ...


class RecorderPort(Protocol):
    def store(self, order: NewOrder) -> int | None: ...
    def kick(self) -> None: ...
    def recover(self) -> None: ...
    def purge(self, now_ms: int | None = None) -> int: ...
    def counts(self) -> Mapping[str, int]: ...
    @property
    def job_state(self) -> str: ...
    async def wait_idle(self, timeout: float | None = None) -> bool: ...


class Orchestrator:
    """Implements `events.Mediator`. Use: `o = Orchestrator(settings)`, build the colleagues with `o` as their
    mediator, `o.attach(zalo, extractor, recorder)`, then `exit_code = await o.run()`."""

    def __init__(
        self,
        settings: Settings,
        *,
        printer: StatusPrinter | None = None,
        clock: Callable[[], int] = epoch_ms,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        handle_signals: bool = True,
    ) -> None:
        self.settings = settings
        self.printer = printer if printer is not None else StatusPrinter()
        self.status = Status()
        self.clock = clock
        self.monotonic = monotonic
        self._sleep = sleep  # None: wait on the stop event, so stop() ends the pause at once
        self.handle_signals = handle_signals
        self.zalo: ZaloPort | None = None
        self.extractor: ExtractorPort | None = None
        self.recorder: RecorderPort | None = None
        self.exit_code = EXIT_OK
        self.stopping = False
        self.ticks = 0
        self._stop_event: asyncio.Event | None = None
        self._last_purge_ms: int | None = None

    def attach(self, zalo: ZaloPort, extractor: ExtractorPort, recorder: RecorderPort) -> None:
        self.zalo = zalo
        self.extractor = extractor
        self.recorder = recorder

    # --- lifecycle --------------------------------------------------------------------------------------------

    def stop(self, exit_code: int | None = None) -> None:
        """Ask the loop to end after the current step. The first non-zero code wins."""
        if exit_code is not None and self.exit_code == EXIT_OK:
            self.exit_code = exit_code
        self.stopping = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self) -> int:
        """Start up, tick until stopped, shut down; returns the process exit code."""
        if self.zalo is None or self.extractor is None or self.recorder is None:
            raise RuntimeError("attach() the colleagues before run()")
        self._stop_event = asyncio.Event()
        if self.stopping:
            self._stop_event.set()
        installed = self._install_signals()
        try:
            self._startup()
            while not self.stopping:
                started = self.monotonic()
                self.tick()
                if self.stopping:
                    break
                elapsed = self.monotonic() - started
                await self._pause(max(0.0, self.settings.read_interval_seconds - elapsed))
        except Exception:
            log.exception("orchestrator_crashed")
            self.stop(EXIT_ERROR)
        finally:
            await self._shutdown()
            self._remove_signals(installed)
        log.info("orchestrator_stopped", extra={"exit_code": self.exit_code})
        return self.exit_code

    def _startup(self) -> None:
        start_tick()
        assert self.zalo and self.extractor and self.recorder
        log.info("orchestrator_starting")
        self.zalo.recover()
        self.extractor.recover()
        self.recorder.recover()
        self._purge()
        self.zalo.open()
        self._refresh()

    def tick(self) -> None:
        """One poll cycle (design §1 rules): poll, kick the recorder, daily purge, redraw the status line."""
        assert self.zalo and self.recorder
        start_tick()
        self.ticks += 1
        self.zalo.poll()
        if self.stopping:
            return
        self.recorder.kick()
        if self._last_purge_ms is None or self.clock() - self._last_purge_ms >= DAY_MS:
            self._purge()
        self._refresh()

    async def _pause(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        assert self._stop_event is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    async def _shutdown(self) -> None:
        if self.recorder is not None:
            try:
                idle = await self.recorder.wait_idle(self.settings.sheets_timeout_seconds)
            except Exception:
                log.exception("recorder_wait_failed")
                idle = False
            if not idle:
                log.warning("sheets_job_unfinished", extra={"timeout": self.settings.sheets_timeout_seconds})
            with contextlib.suppress(Exception):
                self._refresh()
        self.printer.close()

    def _purge(self) -> None:
        assert self.zalo and self.recorder
        now = self.clock()
        self.zalo.purge(now)
        self.recorder.purge(now)
        self._last_purge_ms = now

    def _refresh(self) -> None:
        if self.recorder is not None:
            self.status.set_counts(self.recorder.counts())
            self.status.sheets = self.recorder.job_state
        self.printer.show(self.status)

    def _install_signals(self) -> list[signal.Signals]:
        if not self.handle_signals:
            return []
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):  # not the main thread / unsupported platform
                continue
            installed.append(sig)
        return installed

    def _remove_signals(self, installed: list[signal.Signals]) -> None:
        if not installed:
            return
        loop = asyncio.get_running_loop()
        for sig in installed:
            loop.remove_signal_handler(sig)

    def _on_signal(self, sig: signal.Signals) -> None:
        log.info("signal_received", extra={"signal": sig.name})
        self.stop()

    # --- Mediator ---------------------------------------------------------------------------------------------

    def notify(self, event: Event) -> None:
        """Route one event to its handler (design §1 event catalogue). Synchronous; never blocks on the network."""
        handler = self._routes.get(type(event))
        if handler is None:
            log.warning("event_unrouted", extra={"event_type": event.type})
            return
        handler(self, event)

    def _on_login_required(self, event: LoginRequired) -> None:
        self.status.login = LOGIN_PROMPT
        self.status.conversation = None
        self.printer.show(self.status)

    def _on_no_chat_open(self, event: NoChatOpen) -> None:
        self.status.login = NO_CHAT_PROMPT
        self.status.conversation = None
        self.printer.show(self.status)

    def _on_zalo_ready(self, event: ZaloReady) -> None:
        self.status.login = "reading"
        self.status.conversation = event.conversation.title or event.conversation.key
        self.status.drift = False
        self.printer.show(self.status)

    def _on_new_messages(self, event: NewMessages) -> None:
        assert self.extractor is not None
        self.status.messages_read += len(event.messages)
        self.status.drift = False
        self.extractor.handle(event.messages)

    def _on_selector_drift(self, event: SelectorDrift) -> None:
        log.warning("selector_drift", extra={"reason": event.reason, "snapshot_path": event.snapshot_path})
        self.status.drift = True

    def _on_browser_died(self, event: BrowserDied) -> None:
        log.error(
            "browser_died",
            extra={"attempt": event.attempt, "max_attempts": event.max_attempts, "will_retry": event.will_retry},
        )
        self.status.conversation = None
        if event.will_retry:
            self.status.login = f"browser restarting ({event.attempt}/{event.max_attempts})"
        else:
            self.status.login = "browser down"
            self.stop(EXIT_BROWSER_DIED)
        self.printer.show(self.status)

    def _on_new_order(self, event: NewOrder) -> None:
        assert self.recorder is not None
        order_id = self.recorder.store(event)  # committed before the extractor emits MessagesChecked [R3.4]
        if order_id is not None:
            self.status.orders_found += 1
        self.recorder.kick()  # due at once [R5.1]; starts a background job only if none is in flight

    def _on_messages_checked(self, event: MessagesChecked) -> None:
        assert self.zalo is not None
        self.zalo.mark_checked(event.conversation_key, event.message_keys)

    def _on_order_recorded(self, event: OrderRecorded) -> None:
        self._refresh()

    def _on_record_failed(self, event: RecordFailed) -> None:
        extra = {"order_ids": event.order_ids, "error": event.error, "next_attempt_at": event.next_attempt_at_ms}
        if event.final:
            log.error("record_failed_final", extra=extra)
        else:
            log.warning("record_failed", extra=extra)
        self._refresh()

    def _on_recorder_blocked(self, event: RecorderBlocked) -> None:
        log.error("recorder_blocked", extra={"reason": event.reason})
        self.status.sheets = "blocked"
        self.stop(EXIT_RECORDER_BLOCKED)

    _routes: ClassVar[dict[type[Event], Callable[[Orchestrator, Event], None]]] = {  # type: ignore[dict-item]
        LoginRequired: _on_login_required,
        NoChatOpen: _on_no_chat_open,
        ZaloReady: _on_zalo_ready,
        NewMessages: _on_new_messages,
        SelectorDrift: _on_selector_drift,
        BrowserDied: _on_browser_died,
        NewOrder: _on_new_order,
        MessagesChecked: _on_messages_checked,
        OrderRecorded: _on_order_recorded,
        RecordFailed: _on_record_failed,
        RecorderBlocked: _on_recorder_blocked,
    }
