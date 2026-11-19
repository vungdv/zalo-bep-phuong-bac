"""Non-functional requirements (acceptance), measured on the real loop with real wall-clock pacing."""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

import pytest

from acceptance.harness import REPO_ROOT, T0, App, FakeWorksheet, google_sheet
from fake_driver import FakeChat, bubble
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder.sheet import HEADER

INTERVAL = 0.3


@pytest.mark.parametrize("outage", ["hang", "error"])
async def test_nfr1_new_message_stored_within_interval_plus_1s_while_sheets_is_down(db: Path, outage: str):
    """NFR1: with Google Sheets hung (a call that never returns) or failing every call, each new message is stored
    within READ_INTERVAL_SECONDS + 1 s of appearing."""
    ws = FakeWorksheet(header=list(HEADER))
    if outage == "hang":
        ws.fault("append_rows", "hang")
    else:
        ws.fault("append_rows", "error", times=10_000)
        ws.fault("col_values", "error", times=10_000)
    chat = FakeChat([bubble(T0, "1001", "lấy 1", sender="Lan")], window=10)
    app = App(
        db,
        sheet=google_sheet(ws),
        page=chat,
        pace=True,
        read_interval_seconds=INTERVAL,
        record_backoff_base_seconds=0.01,
        record_backoff_max_seconds=0.01,
        record_max_attempts=10_000,
    )
    shown: dict[str, float] = {}
    latency: dict[str, float] = {}

    def tick(n: int) -> None:
        now = time.monotonic()
        for key, t in shown.items():
            if key not in latency and app.messages.exists("g1", key):
                latency[key] = now - t
        if 2 <= n <= 7:
            m = bubble(T0 + n * 1000, "1002", f"lấy {n}", sender="Minh")
            chat.append(m)
            shown[m["message_key"]] = time.monotonic()

    app.on_tick.append(tick)
    try:
        assert await app.run(until=lambda: len(latency) == 6, max_ticks=30) == EXIT_OK
    finally:
        ws.release()
    if outage == "hang":
        assert ws.data == []  # Sheets really was stuck the whole time
    assert len(latency) == 6
    assert max(latency.values()) <= INTERVAL + 1.0, latency


async def test_nfr2_order_found_to_sheet_row_p95_within_10s(db: Path):
    """NFR2: with Sheets reachable, the p95 delay from an order being found to its row existing is ≤ 10 s."""
    ws = FakeWorksheet(header=list(HEADER))
    chat = FakeChat([], window=10)
    app = App(db, sheet=google_sheet(ws), page=chat, pace=True, read_interval_seconds=0.1)
    found_at: dict[int, float] = {}
    written_at: dict[str, float] = {}
    original_store = app.recorder.store

    def store(order):  # the moment the order is found
        order_id = original_store(order)
        if order_id is not None:
            found_at[order_id] = time.monotonic()
        return order_id

    app.recorder.store = store  # type: ignore[method-assign]

    def tick(n: int) -> None:
        for row in ws.data:
            written_at.setdefault(str(row[0]), time.monotonic())
        if n <= 20:
            chat.append(bubble(T0 + n * 1000, "1001", f"lấy {n}", sender="Lan"))

    app.on_tick.append(tick)
    assert await app.run(until=lambda: len(written_at) == 20, max_ticks=200) == EXIT_OK
    delays = sorted(written_at[str(i)] - t for i, t in found_at.items())
    p95 = delays[int(0.95 * (len(delays) - 1))]
    assert len(delays) == 20 and p95 <= 10.0, delays


def test_nfr3_coverage_floor_is_enforced_by_the_test_run():
    """NFR3: the pytest configuration fails the run below 80 % coverage of the package."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--cov=order_extractor" in addopts and "--cov-fail-under=80" in addopts


async def test_nfr4_idle_poll_costs_well_under_5_percent_cpu_on_the_python_side(db: Path):
    """NFR4 (Python side only): an idle poll (page unchanged, everything stored) costs far less than 5 % of a 2 s
    interval in CPU. The playwright-cli / browser side needs a live session (task T16) and is not measured here."""
    chat = FakeChat([bubble(T0 + i, "1001", f"tin {i}", sender="Lan") for i in range(40)], window=40)
    app = App(db, sheet=google_sheet(FakeWorksheet(header=list(HEADER))), page=chat)
    assert await app.run(max_ticks=2, close=False) == EXIT_OK
    polls = 100
    started = time.process_time()
    for _ in range(polls):
        app.zalo.poll()
    per_poll = (time.process_time() - started) / polls
    app.conn.close()
    assert per_poll < 0.05 * 2.0 / 10, per_poll  # 10x headroom under the budget
