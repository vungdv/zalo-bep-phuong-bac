"""R5 — Google Sheets output (acceptance). The real `GoogleSheet` adapter over a fake gspread worksheet."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from acceptance.harness import (
    T0,
    App,
    FakeWorksheet,
    all_orders,
    google_sheet,
)
from fake_driver import NO_CHAT, FakeChat, FakeDriver, bubble
from fake_sheet import FixedJitter, FrozenClock
from fakes import FakeMediator
from order_extractor import cli
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import NewOrder
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder import sheet as sheet_module
from order_extractor.recorder.order_recorder import OrderRecorder
from order_extractor.recorder.orders_repo import OrdersRepo
from order_extractor.recorder.sheet import HEADER, GoogleSheet

FAST_RETRY = {"record_backoff_base_seconds": 0.001, "record_backoff_max_seconds": 0.001}


def order_chat(n: int = 1, start: int = T0, uid: str = "1001", name: str = "Lan") -> FakeChat:
    return FakeChat([bubble(start + i * 1000, uid, f"lấy {i + 1} cái", sender=name) for i in range(n)], window=10)


def new_order(key: str, number: int = 1, sent_at: int = T0) -> NewOrder:
    return NewOrder(
        conversation_key="g1",
        message_key=key,
        number=number,
        sent_at_ms=sent_at,
        conversation_title="Nhóm",
        sender="Lan",
        text=f"lấy {number}",
    )


def patch_gspread(monkeypatch: pytest.MonkeyPatch, ws: FakeWorksheet) -> list[float]:
    """`gspread.service_account` → a client whose spreadsheet tab is `ws`; returns the timeouts it was given."""
    timeouts: list[float] = []

    class Book:
        def worksheet(self, name: str) -> FakeWorksheet:
            return ws

    class Client:
        def set_timeout(self, value: float) -> None:
            timeouts.append(value)

        def open_by_key(self, key: str) -> Book:
            return Book()

    monkeypatch.setattr(sheet_module.gspread, "service_account", lambda filename: Client())
    return timeouts


def append_kwargs(ws: FakeWorksheet) -> list[dict[str, Any]]:
    return [k for name, k in ws.calls if name == "append_rows"]


# --- R5.1 ----------------------------------------------------------------------------------------------------


async def test_r5_1_a_stored_order_is_due_immediately(db: Path):
    """R5.1: with the recorder's clock frozen at the moment the order is stored, it is still appended (no wait)."""
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=order_chat(), recorder_clock=FrozenClock(T0 + 10_000))
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK
    assert app.ticks <= 10
    [order] = all_orders(db)
    assert order.attempts == 0 and order.status == "RECORDED"


# --- R5.2 ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ['=IMPORTXML("http://x", "//a") 2', "+84 912 3", "-5 cái", "@all 4"])
async def test_r5_2_values_are_written_raw_so_formulas_are_never_run(db: Path, text: str):
    """R5.2: text starting with = + - @ is stored as typed: every write uses RAW input, the cell keeps the text."""
    ws = FakeWorksheet()
    app = App(db, sheet=google_sheet(ws), page=FakeChat([bubble(T0, "1001", text, sender="Lan")]))
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK
    assert ws.data[0][3] == text
    for name, kwargs in ws.writes():
        if name in ("update", "append_rows"):
            assert str(kwargs["value_input_option"]).upper().endswith("RAW"), (name, kwargs)


async def test_r5_2_columns_number_quantity_customer_message(db: Path):
    """R5.2: columns Number, Quantity, Customer, Message; Customer is the message's sender."""
    ws = FakeWorksheet()
    app = App(db, sheet=google_sheet(ws), page=order_chat())
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK
    assert ws.header == ["Number", "Quantity", "Customer", "Message"]
    assert ws.data[0][1:3] == [1, "Lan"]


# --- R5.3 ----------------------------------------------------------------------------------------------------


async def test_r5_3_empty_header_is_created(db: Path):
    """R5.3: an empty header row is written."""
    ws = FakeWorksheet()
    app = App(db, sheet=google_sheet(ws), page=NO_CHAT)
    assert await app.run(until=lambda: bool(ws.header), max_ticks=100) == EXIT_OK
    assert ws.header == list(HEADER)


@pytest.mark.parametrize(
    "header",
    [
        ["Number", "Quantity", "Customer", "Message", "notes"],
        ["Quantity", "Number", "Customer", "Message"],
        ["Number", "Quantity", "Customer", "Messages"],
    ],
)
async def test_r5_3_header_mismatch_exits_non_zero_and_writes_nothing(db: Path, header: list[str], caplog):
    """R5.3: a header that differs (extra, reordered or renamed column) → non-zero exit naming the mismatch,
    and nothing is written to the sheet, even with orders waiting."""
    caplog.set_level(logging.ERROR, logger="order_extractor")
    ws = FakeWorksheet(header=header)
    app = App(db, sheet=google_sheet(ws), page=order_chat(3))
    code = await app.run(max_ticks=100)
    assert code != 0
    assert app.ticks < 100
    assert ws.writes() == []
    assert ws.header == header
    reasons = [str(getattr(r, "reason", "")) for r in caplog.records if r.getMessage() == "recorder_blocked"]
    assert reasons and all(col in reasons[0] for col in header)


def test_r5_3_cli_start_names_the_mismatch_and_exits_non_zero(cli_env: Path, monkeypatch):
    """R5.3 through `order-extractor start`: non-zero exit code, the mismatch is named on stderr, no write."""
    ws = FakeWorksheet(header=["order_id", "when"])
    monkeypatch.setattr(cli, "build_sheet", lambda settings: google_sheet(ws))
    monkeypatch.setattr(cli, "build_driver", lambda settings, selectors: FakeDriver(order_chat()))
    result = CliRunner().invoke(cli.app, ["start"])
    assert result.exit_code != 0
    assert "when" in result.stderr
    assert ws.writes() == []


async def test_r5_3_header_with_trailing_blank_cells_is_accepted(db: Path):
    """R5.3: trailing empty cells after the last column are not a mismatch."""
    ws = FakeWorksheet(header=[*HEADER, "", ""])
    app = App(db, sheet=google_sheet(ws), page=order_chat())
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK


# --- R5.4 ----------------------------------------------------------------------------------------------------


async def test_r5_4_existing_rows_are_never_rewritten(db: Path):
    """R5.4: later work only appends; a cell a human added next to an existing row is never overwritten."""
    ws = FakeWorksheet(header=list(HEADER))
    chat = order_chat(1)
    app = App(db, sheet=google_sheet(ws), page=chat)
    edited = {"done": False}

    def human(n: int) -> None:
        if ws.data and not edited["done"]:
            ws.data[0].append("done")  # staff note to the right of the app's columns
            edited["done"] = True
            chat.append(bubble(T0 + 60_000, "1002", "lấy 8", sender="Minh"))

    app.on_tick.append(human)
    assert await app.run(until=lambda: len(ws.data) == 2, max_ticks=200) == EXIT_OK
    assert ws.data[0][4:] == ["done"]
    assert [name for name, _ in ws.writes()] == ["append_rows", "append_rows"]


# --- R5.5 ----------------------------------------------------------------------------------------------------


async def test_r5_5_startup_reconciles_new_and_failed_orders_found_in_column_a(db: Path):
    """R5.5: on startup column A is read; NEW and FAILED orders found there (compared as trimmed strings) are
    marked RECORDED without appending; a FAILED order not in column A stays FAILED (not retried automatically)."""
    conn = open_database(db)
    repo = OrdersRepo(conn)
    ids = [repo.store(new_order(f"{T0 + i}_1001", i + 1), T0) for i in range(3)]
    repo.mark_failed(ids[1], attempts=5, error="timeout")
    repo.mark_failed(ids[2], attempts=5, error="timeout")
    conn.close()
    ws = FakeWorksheet(header=list(HEADER), rows=[[f" {ids[0]} ", "x"], [ids[1], "y"]])  # padded text, a number

    app = App(db, sheet=google_sheet(ws), page=NO_CHAT)
    assert await app.run(max_ticks=15) == EXIT_OK
    status = {o.id: o.status for o in all_orders(db)}
    assert status == {ids[0]: "RECORDED", ids[1]: "RECORDED", ids[2]: "FAILED"}
    assert ws.count("append_rows") == 0
    assert len(ws.data) == 2


async def test_r5_5_append_that_landed_but_timed_out_is_not_appended_again(db: Path):
    """R5.5: the append reached Google but the response was lost; before the retry column A is re-read, the id is
    found, and the order becomes RECORDED with exactly one row."""
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "land_timeout")
    app = App(db, sheet=google_sheet(ws), page=order_chat(), **FAST_RETRY)
    assert await app.run(until=lambda: all_orders(db)[0].status == "RECORDED" if all_orders(db) else False) == 0
    assert len(ws.data) == 1
    assert ws.count("append_rows") == 1
    assert ws.count("col_values") >= 2  # startup + the re-read before the retry


async def test_r5_5_last_attempt_lands_but_times_out_failed_then_reconciled_at_next_startup(db: Path):
    """R5.5: the last attempt lands but times out → FAILED; the next startup finds it in column A → RECORDED, still
    exactly one row."""
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "error")
    ws.fault("append_rows", "land_timeout")
    first = App(db, sheet=google_sheet(ws), page=order_chat(), record_max_attempts=2, **FAST_RETRY)
    assert await first.run(until=lambda: all_orders(db)[0].status == "FAILED" if all_orders(db) else False) == 0
    assert len(ws.data) == 1

    second = App(db, sheet=google_sheet(ws), page=NO_CHAT, record_max_attempts=2, **FAST_RETRY)
    assert await second.run(until=lambda: all_orders(db)[0].status == "RECORDED", max_ticks=100) == EXIT_OK
    assert len(ws.data) == 1
    assert ws.count("append_rows") == 2


async def test_r5_5_r5_6_sheets_down_for_a_while_orders_land_once_without_duplicates(db: Path):
    """R5.5, R5.6: Sheets fails (errors and timeouts, one of which landed) for several attempts while messages keep
    coming; once Sheets is back every order is in the sheet exactly once."""
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "error", times=2)
    ws.fault("append_rows", "land_timeout")
    ws.fault("col_values", "timeout")
    ws.fault("append_rows", "timeout")
    chat = order_chat(2)
    app = App(db, sheet=google_sheet(ws), page=chat, record_max_attempts=20, **FAST_RETRY)
    app.on_tick.append(
        lambda n: (
            chat.append(bubble(T0 + 100_000 + n, "1003", f"lấy {n} hộp", sender="Hoa"))
            if n % 4 == 0 and n <= 40
            else None
        )
    )

    def settled() -> bool:
        orders = all_orders(db)
        return app.ticks > 41 and len(orders) == 12 and all(o.status == "RECORDED" for o in orders)

    assert await app.run(until=settled, max_ticks=400) == EXIT_OK
    orders = all_orders(db)
    ids = [str(row[0]) for row in ws.data]
    assert sorted(ids, key=int) == [str(o.id) for o in orders]
    assert len(ids) == len(set(ids)) == 12


# --- R5.6 ----------------------------------------------------------------------------------------------------


async def test_r5_6_order_failed_after_record_max_attempts_and_logged(db: Path, caplog):
    """R5.6: after RECORD_MAX_ATTEMPTS failed calls the order is FAILED, logged at ERROR, and not retried."""
    caplog.set_level(logging.WARNING, logger="order_extractor")
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "error", times=3)
    app = App(db, sheet=google_sheet(ws), page=order_chat(), record_max_attempts=3, **FAST_RETRY)
    assert await app.run(max_ticks=40) == EXIT_OK
    [order] = all_orders(db)
    assert (order.status, order.attempts) == ("FAILED", 3)
    assert ws.count("append_rows") == 3
    assert any(r.levelno == logging.ERROR and getattr(r, "order_id", None) == order.id for r in caplog.records)
    assert app.of_type("RecordFailed")[-1].final is True
    assert any("failed 1" in line for line in app.status_lines)


@pytest.mark.parametrize(("jitter", "expected_s"), [(1.0, [2, 4, 5, 5]), (0.0, [1, 2, 2.5, 2.5])])
async def test_r5_6_retries_back_off_exponentially_with_jitter_and_cap(db: Path, jitter: float, expected_s):
    """R5.6: next_attempt_at = now + min(BASE * 2^(n-1), MAX) * U(0.5, 1); not retried before it is due."""
    clock = FrozenClock(T0)
    conn = open_database(db)
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "error", times=10)
    mediator = FakeMediator()
    recorder = OrderRecorder(
        mediator,
        OrdersRepo(conn),
        google_sheet(ws),
        max_attempts=5,
        backoff_base_seconds=2,
        backoff_max_seconds=5,
        clock=clock,
        rng=FixedJitter(jitter),
    )
    recorder.store(new_order(f"{T0}_1001"))

    async def kick() -> None:
        recorder.kick()
        await recorder.wait_idle(2)

    await kick()  # header + column A
    delays = []
    for attempt in range(1, 5):
        await kick()
        assert ws.count("append_rows") == attempt
        order = OrdersRepo(conn).get(1)
        assert order is not None and order.attempts == attempt
        delays.append((order.next_attempt_at_ms - clock()) / 1000)
        clock.advance(order.next_attempt_at_ms - clock() - 1)
        await kick()
        assert ws.count("append_rows") == attempt  # not due yet
        clock.advance(1)
    assert delays == expected_s
    await kick()
    assert OrdersRepo(conn).get(1).status == "FAILED"  # type: ignore[union-attr]
    conn.close()


async def test_r5_6_one_call_in_flight_and_the_loop_never_waits_for_it(db: Path):
    """R5.6, NFR1: while an append hangs, polling continues, new messages and orders are stored, and no second
    Sheets call starts; when it completes every order is appended exactly once."""
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "hang")
    chat = order_chat(1)
    app = App(db, sheet=google_sheet(ws), page=chat)
    seen: dict[str, int] = {}

    def tick(n: int) -> None:
        if ws.hanging.is_set():
            seen.setdefault("from", n)
            chat.append(bubble(T0 + 10_000 + n, "1002", f"lấy {n}", sender="Minh"))
            assert ws.count("append_rows") == 1
            if n - seen["from"] >= 10:
                ws.release()

    app.on_tick.append(tick)
    assert await app.run(until=lambda: len(ws.data) >= 12, max_ticks=300) == EXIT_OK
    orders = all_orders(db)
    assert len(orders) >= 12
    ids = [str(r[0]) for r in ws.data]
    assert len(ids) == len(set(ids))
    assert seen["from"] < 300


def test_r5_6_sheets_client_timeout_is_sheets_timeout_seconds(monkeypatch):
    """R5.6: each Sheets call is bounded by SHEETS_TIMEOUT_SECONDS (default 10) on the HTTP client."""
    timeouts = patch_gspread(monkeypatch, FakeWorksheet(header=list(HEADER)))
    assert Settings().sheets_timeout_seconds == 10
    GoogleSheet.from_settings(Settings(sheet_id="x")).read_header()
    GoogleSheet.from_settings(Settings(sheet_id="x", sheets_timeout_seconds=3)).read_header()
    assert timeouts == [10, 3]


# --- R5.7 ----------------------------------------------------------------------------------------------------


async def test_r5_7_order_recorded_is_emitted_for_each_written_row(db: Path):
    """R5.7: one OrderRecorded per row written, carrying the order id and the sheet row."""
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=order_chat(3))
    assert await app.run(until=lambda: len(ws.data) == 3, max_ticks=100) == EXIT_OK
    recorded = {(e.order_id, e.sheet_row) for e in app.of_type("OrderRecorded")}
    assert recorded == {(int(row[0]), i + 2) for i, row in enumerate(ws.data)}
    assert any("recorded 3" in line for line in app.status_lines)
