"""Wiring for the acceptance suite: the real Orchestrator, colleagues and SQLite, fakes only at the edges.

External boundaries replaced here:

* `FakePlaywrightCli` stands in for the `playwright-cli` binary (the `runner` of the real `BrowserDriver`).
* `PageWorker` runs the real `reader.js` and scroll scripts in headless Chromium on a DOM fixture.
* `FakeWorksheet` stands in for a gspread `Worksheet` (the real `GoogleSheet` adapter wraps it).
* `FakeDriver` / `InMemorySheet` from the existing test helpers, where the adapters are not the subject.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from fake_driver import FakeDriver
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.extractor.order_extractor import OrderExtractor
from order_extractor.extractor.rules import OrderRule
from order_extractor.orchestrator import Orchestrator
from order_extractor.recorder.order_recorder import OrderRecorder
from order_extractor.recorder.orders_repo import Order, OrdersRepo
from order_extractor.recorder.sheet import GoogleSheet, Sheet
from order_extractor.status import StatusPrinter
from order_extractor.zalo.browser import Driver
from order_extractor.zalo.messages_repo import MessagesRepo
from order_extractor.zalo.zalo_web import ZaloWeb
from rules_for_tests import FirstIntegerRule

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "dom"
DAY_MS = 86_400_000
READER = "READER()"  # the reader script handed to ZaloWeb when the driver is fake


def at(text: str) -> int:
    """'YYYY-MM-DD HH:MM[:SS]' in Asia/Ho_Chi_Minh → epoch ms."""
    return round(datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp() * 1000)


T0 = at("2026-11-19 10:38")  # the requirements §1 example time


class OffsetClock:
    """An epoch-ms clock that starts at `start_ms`, follows real time, and can jump forward."""

    def __init__(self, start_ms: int) -> None:
        self.base = start_ms
        self.started = time.monotonic()

    def __call__(self) -> int:
        return self.base + int((time.monotonic() - self.started) * 1000)

    def jump(self, ms: int) -> None:
        self.base += ms


class AlwaysRule:
    """A rule that calls every message it sees an order of `number`."""

    def __init__(self, number: int = 7) -> None:
        self.number = number
        self.seen: list[str] = []

    def extract(self, message: Any) -> int | None:
        self.seen.append(message.message_key)
        return self.number


# --- the application under test ---------------------------------------------------------------------------


class App:
    """One process lifetime: a fresh Orchestrator and real colleagues over a database file."""

    def __init__(
        self,
        db_path: Path,
        *,
        sheet: Sheet,
        page: Any = None,
        driver: Driver | None = None,
        rule: OrderRule | None = None,
        since_ms: int | None = 0,
        clock: Callable[[], int] | None = None,
        recorder_clock: Callable[[], int] | None = None,
        printer: StatusPrinter | None = None,
        reader_script: str = READER,
        pace: bool = False,
        **settings: Any,
    ) -> None:
        values: dict[str, Any] = {
            "db_path": db_path,
            "log_dir": db_path.parent / "logs",
            "read_interval_seconds": 0.01,
            "sheets_timeout_seconds": 2.0,
        }
        values.update(settings)
        self.settings = Settings(**values)
        self.conn = open_database(db_path)
        self.sheet = sheet
        self.rule = rule if rule is not None else FirstIntegerRule()
        self.clock = clock if clock is not None else OffsetClock(T0 + 3_600_000)
        self.pace = pace  # True: wait out the real READ_INTERVAL_SECONDS between ticks
        self.out = io.StringIO()
        self.ticks = 0
        self.max_ticks = 400
        self.until: Callable[[], bool] = lambda: False
        self.on_tick: list[Callable[[int], None]] = []
        self.orch = Orchestrator(
            self.settings,
            printer=printer if printer is not None else StatusPrinter(self.out, tty=False),
            clock=self.clock,
            sleep=self._sleep,
            handle_signals=False,
        )
        max_restarts = self.settings.browser_max_restarts
        self.driver = driver if driver is not None else FakeDriver(page, max_restarts=max_restarts)
        self.messages = MessagesRepo(self.conn)
        self.orders = OrdersRepo(self.conn)
        self.zalo = ZaloWeb(
            self.orch,
            self.driver,
            self.messages,
            self.settings,
            reader_script=reader_script,
            since_ms=since_ms,
            clock=self.clock,
            sleep=lambda s: None,
        )
        recorder_kwargs: dict[str, Any] = {}
        if recorder_clock is not None:
            recorder_kwargs["clock"] = recorder_clock
        self.recorder = OrderRecorder.from_settings(self.orch, self.orders, sheet, self.settings, **recorder_kwargs)
        self.orch.attach(self.zalo, OrderExtractor(self.orch, self.rule), self.recorder)
        self.events: list[Any] = []
        route = self.orch.notify

        def spy(event: Any) -> None:
            self.events.append(event)
            route(event)

        self.orch.notify = spy  # type: ignore[method-assign]

    async def _sleep(self, seconds: float) -> None:
        self.ticks += 1
        await asyncio.sleep(seconds if self.pace else 0.005)  # also lets the recorder's background job resume
        for callback in self.on_tick:
            callback(self.ticks)
        if self.until() or self.ticks >= self.max_ticks:
            self.orch.stop()

    async def run(self, until: Callable[[], bool] = lambda: False, *, max_ticks: int = 400, close: bool = True) -> int:
        self.until = until
        self.max_ticks = max_ticks
        try:
            return await asyncio.wait_for(self.orch.run(), 30)
        finally:
            if close:
                self.conn.close()

    def of_type(self, name: str) -> list[Any]:
        return [e for e in self.events if e.type == name]

    @property
    def status_lines(self) -> list[str]:
        return self.out.getvalue().splitlines()


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = open_database(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def all_orders(db_path: Path) -> list[Order]:
    conn = open_database(db_path)
    try:
        return [Order.from_row(r) for r in conn.execute("SELECT * FROM orders ORDER BY id")]
    finally:
        conn.close()


def all_messages(db_path: Path) -> list[dict[str, Any]]:
    return db_rows(db_path, "SELECT * FROM messages ORDER BY sent_at, id")


# --- gspread worksheet boundary ---------------------------------------------------------------------------


class FakeWorksheet:
    """A gspread `Worksheet` stand-in: row 1 is the header, data from row 2; every call is recorded.

    `fault(method, kind, times)` injects, in order per method: `error` (connection error), `timeout` (read
    timeout, nothing written), `land_timeout` (append writes the rows, then times out), `hang` (block the worker
    thread until `release()`, then behave normally).
    """

    def __init__(self, header: list[Any] | None = None, rows: list[list[Any]] | None = None) -> None:
        self.cells: list[list[Any]] = [list(header or [])] + [list(r) for r in rows or []]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.validations: list[tuple[str, Any]] = []
        self._faults: dict[str, list[str]] = {}
        self._released = threading.Event()
        self.hanging = threading.Event()
        self.lock = threading.Lock()

    # injection
    def fault(self, method: str, kind: str = "error", times: int = 1) -> None:
        self._faults.setdefault(method, []).extend([kind] * times)

    def release(self) -> None:
        self._released.set()

    def _enter(self, method: str, **kwargs: Any) -> str | None:
        self.calls.append((method, kwargs))
        faults = self._faults.get(method)
        if not faults:
            return None
        kind = faults.pop(0)
        if kind == "hang":
            self.hanging.set()
            self._released.wait(5.0)
            self.hanging.clear()
            return None
        if kind == "error":
            raise requests.exceptions.ConnectionError(f"{method}: connection refused")
        if kind == "timeout":
            raise requests.exceptions.ReadTimeout(f"{method}: read timed out")
        return kind

    # gspread API used by GoogleSheet
    def row_values(self, row: int) -> list[str]:
        self._enter("row_values", row=row)
        values = self.cells[row - 1] if row <= len(self.cells) else []
        return ["" if v is None else str(v) for v in values]

    def col_values(self, col: int) -> list[Any]:
        self._enter("col_values", col=col)
        values = [r[col - 1] if len(r) >= col else "" for r in self.cells]
        while values and values[-1] in ("", None):
            values.pop()
        return values

    def update(self, values: list[list[Any]], range_name: str, **kwargs: Any) -> dict[str, Any]:
        self._enter("update", values=values, range_name=range_name, **kwargs)
        match = re.fullmatch(r"A(\d+):[A-Z]+\d+", range_name)
        assert match, f"unexpected update range {range_name}"
        row = int(match.group(1))
        while len(self.cells) < row:
            self.cells.append([])
        self.cells[row - 1] = list(values[0])
        return {}

    def add_validation(self, range_name: str, condition_type: Any, values: Any, **kwargs: Any) -> dict[str, Any]:
        self._enter("add_validation", range_name=range_name, condition_type=condition_type, **kwargs)
        self.validations.append((range_name, condition_type))
        return {}

    def append_rows(self, values: list[list[Any]], **kwargs: Any) -> dict[str, Any]:
        kind = self._enter("append_rows", values=values, **kwargs)
        with self.lock:
            first = len(self.cells) + 1
            self.cells.extend(list(v) for v in values)
            last = len(self.cells)
        if kind == "land_timeout":
            raise requests.exceptions.ReadTimeout("append_rows: read timed out after the write")
        return {"updates": {"updatedRange": f"'Orders'!A{first}:G{last}"}}

    # helpers
    @property
    def header(self) -> list[Any]:
        return self.cells[0]

    @property
    def data(self) -> list[list[Any]]:
        return self.cells[1:]

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(n, k) for n, k in self.calls if n in ("update", "append_rows", "add_validation")]


def google_sheet(ws: FakeWorksheet) -> GoogleSheet:
    """The real Sheets adapter over a fake worksheet."""
    return GoogleSheet(lambda: ws, TZ)  # type: ignore[arg-type, return-value]


# --- playwright-cli boundary ------------------------------------------------------------------------------


def _done(argv: list[str], stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


_SCROLL_DY = re.compile(r"scrollTop \+ \((-?\d+)\)")
_SCROLL_BOTTOM = re.compile(r"if \((true|false)\)")


def chat_evaluator(chat: Any, reader_script: str = READER) -> Callable[[str], Any]:
    """Answer `eval` calls from a `FakeChat`: the reader script renders, the scroll script scrolls."""

    def evaluate(js: str) -> Any:
        if js == reader_script:
            return chat.render()
        bottom, dy = _SCROLL_BOTTOM.search(js), _SCROLL_DY.search(js)
        assert bottom and dy, f"unexpected script: {js[:80]}"
        if bottom.group(1) == "true":
            chat.to_bottom()
        else:
            chat.scroll(int(dy.group(1)))
        pos = chat.position()
        return {"scroll_top": pos.scroll_top, "scroll_height": pos.scroll_height, "client_height": pos.client_height}

    return evaluate


class FakePlaywrightCli:
    """Stands in for the `playwright-cli` executable (`subprocess.run` signature) for the real `BrowserDriver`."""

    def __init__(self, evaluate: Callable[[str], Any] | None = None) -> None:
        self.evaluate = evaluate or (lambda js: {"state": "NO_CHAT"})
        self.argv: list[list[str]] = []
        self.open_sessions: set[str] = set()
        self.can_open = True
        self.failed_opens = 0

    def kill(self, session: str = "zalo") -> None:
        """The human closed the browser window / the daemon died."""
        self.open_sessions.discard(session)

    def commands(self) -> list[str]:
        out = []
        for argv in self.argv:
            words = [a for a in argv[1:] if not a.startswith("-")]
            out.append(words[0] if words else "?")
        return out

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.argv.append(argv)
        args = argv[1:]
        if args[:2] == ["list", "--json"]:
            browsers = [{"name": s, "status": "open"} for s in sorted(self.open_sessions)]
            return _done(argv, json.dumps({"browsers": browsers}))
        session = args[0].removeprefix("-s=")
        command = [a for a in args[1:] if not a.startswith("-")]
        if command and command[0] == "open":
            if not self.can_open:
                self.failed_opens += 1
                return _done(argv, returncode=1, stderr="browser failed to launch")
            self.open_sessions.add(session)
            return _done(argv, "opened")
        if session not in self.open_sessions:
            return _done(argv, returncode=1, stderr=f"session '{session}' not found")
        if args[1:3] == ["--raw", "eval"]:
            value = self.evaluate(args[3])
            return _done(argv, value if isinstance(value, str) else json.dumps(value))
        if command and command[0] == "snapshot":
            name = next(a for a in args if a.startswith("--filename=")).split("=", 1)[1]
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            Path(name).write_text("- snapshot\n")
            return _done(argv, "saved")
        return _done(argv, returncode=2, stderr=f"unsupported: {args}")


class PageWorker:
    """Headless Chromium on its own thread (the sync API cannot run inside the test's event loop)."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[Callable[[Any], Any], concurrent.futures.Future[Any]] | None] = queue.Queue()
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()
        self._ready.result(timeout=60)

    def _main(self) -> None:
        try:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.launch()
            page = browser.new_page()
        except Exception as exc:
            self._ready.set_exception(exc)
            return
        self._ready.set_result(None)
        while (job := self._jobs.get()) is not None:
            func, future = job
            try:
                future.set_result(func(page))
            except Exception as exc:
                future.set_exception(exc)
        browser.close()
        pw.stop()

    def call(self, func: Callable[[Any], Any]) -> Any:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._jobs.put((func, future))
        return future.result(timeout=30)

    def load(self, fixture: str) -> None:
        html = (FIXTURES / fixture).read_text(encoding="utf-8")
        self.call(lambda page: page.set_content(html))

    def evaluate(self, js: str) -> Any:
        return self.call(lambda page: page.evaluate(js))

    def body(self) -> str:
        return self.call(lambda page: page.evaluate("() => document.body.innerHTML"))

    def close(self) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=30)
