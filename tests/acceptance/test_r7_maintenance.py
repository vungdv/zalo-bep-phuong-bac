"""R7 — Maintenance commands, through the real typer CLI (acceptance)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from acceptance.harness import (
    T0,
    AlwaysRule,
    App,
    FakeWorksheet,
    all_messages,
    all_orders,
    at,
    google_sheet,
)
from fake_driver import NO_CHAT, FakeChat, FakeDriver, bubble
from order_extractor import cli
from order_extractor.db.connection import open_database
from order_extractor.lock import AppLock, LockHeld, lock_path_for
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder.sheet import HEADER
from order_extractor.zalo.messages_repo import MessagesRepo
from rules_for_tests import FirstIntegerRule

runner = CliRunner()
FAST = {"record_backoff_base_seconds": 0.001, "record_backoff_max_seconds": 0.001}


async def make_failed_and_recorded(db: Path, ws: FakeWorksheet) -> None:
    """A real run: the first order fails its only attempt (FAILED), the second is recorded."""
    ws.fault("append_rows", "error")
    chat = FakeChat([bubble(T0, "1001", "lấy 1", sender="Lan")])
    app = App(db, sheet=google_sheet(ws), page=chat, record_max_attempts=1, **FAST)
    app.on_tick.append(lambda n: chat.append(bubble(T0 + 60_000, "1002", "lấy 2", sender="Minh")) if n == 5 else None)
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK
    assert [o.status for o in all_orders(db)] == ["FAILED", "RECORDED"]


async def test_r7_1_status_prints_counts_per_status_and_the_oldest_pending_order(cli_env: Path):
    """R7.1: `status` prints, from the local database, the count per order status and the oldest NEW/FAILED."""
    db = cli_env
    await make_failed_and_recorded(db, FakeWorksheet(header=list(HEADER)))
    failed = all_orders(db)[0]

    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    lines = [line.split() for line in result.stdout.splitlines()]
    assert ["NEW", "0"] in lines and ["RECORDED", "1"] in lines and ["FAILED", "1"] in lines
    oldest = next(line for line in result.stdout.splitlines() if line.startswith("oldest NEW/FAILED"))
    assert f"#{failed.id}" in oldest and "FAILED" in oldest


async def test_r7_2_reprocess_order_resets_failed_and_next_start_does_not_duplicate(cli_env: Path):
    """R7.2: `reprocess --order` turns a FAILED order into NEW with attempts 0; the next start re-checks column A,
    so the order is appended exactly once."""
    db = cli_env
    ws = FakeWorksheet(header=list(HEADER))
    await make_failed_and_recorded(db, ws)
    failed = all_orders(db)[0]

    result = runner.invoke(cli.app, ["reprocess", "--order", str(failed.id)])
    assert result.exit_code == 0, result.output
    again = all_orders(db)[0]
    assert (again.status, again.attempts) == ("NEW", 0)

    app = App(db, sheet=google_sheet(ws), page=NO_CHAT)
    assert await app.run(until=lambda: all_orders(db)[0].status == "RECORDED", max_ticks=100) == EXIT_OK
    assert sorted(int(r[0]) for r in ws.data) == sorted(o.id for o in all_orders(db))


async def test_r7_2_reprocess_order_whose_row_did_land_is_reconciled_not_appended(cli_env: Path):
    """R7.2 + R5.5: the FAILED order's last attempt had actually landed; after `reprocess --order` the next start
    finds it in column A and marks it RECORDED without a second row."""
    db = cli_env
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "land_timeout")
    app = App(db, sheet=google_sheet(ws), page=FakeChat([bubble(T0, "1001", "lấy 1")]), record_max_attempts=1)
    assert await app.run(until=lambda: bool(all_orders(db)) and all_orders(db)[0].status == "FAILED") == EXIT_OK
    order = all_orders(db)[0]

    assert runner.invoke(cli.app, ["reprocess", "--order", str(order.id)]).exit_code == 0
    second = App(db, sheet=google_sheet(ws), page=NO_CHAT)
    assert await second.run(until=lambda: all_orders(db)[0].status == "RECORDED", max_ticks=100) == EXIT_OK
    assert len(ws.data) == 1 and ws.count("append_rows") == 1


async def test_r7_2_reprocess_order_refuses_a_non_failed_order(cli_env: Path):
    """R7.2: only FAILED orders are reset; a RECORDED order is left alone and the command fails."""
    db = cli_env
    await make_failed_and_recorded(db, FakeWorksheet(header=list(HEADER)))
    recorded = all_orders(db)[1]
    result = runner.invoke(cli.app, ["reprocess", "--order", str(recorded.id)])
    assert result.exit_code != 0
    assert all_orders(db)[1].status == "RECORDED"


async def test_r7_3_reprocess_conversation_rechecks_live_messages_since_time(cli_env: Path):
    """R7.3: `reprocess --conversation K --since T` marks the stored, not purged messages of K sent at/after T as
    unchecked; the next start runs the rule on them again; a message that already has an order keeps it."""
    db = cli_env
    msgs = [
        bubble(at("2026-11-19 08:00"), "1001", "lấy 7", sender="Lan"),
        bubble(at("2026-11-19 09:00"), "1001", "lấy 1"),
        bubble(at("2026-11-19 10:00"), "1001", "xin chào"),
        bubble(at("2026-11-19 11:00"), "1001", "lấy 3"),
    ]
    chat = FakeChat(msgs)
    first = App(db, sheet=google_sheet(FakeWorksheet(header=list(HEADER))), page=chat, rule=FirstIntegerRule())
    assert await first.run(max_ticks=4) == EXIT_OK
    assert [o.number for o in all_orders(db)] == [7, 1, 3]
    conn = open_database(db)
    MessagesRepo(conn).purge_messages(at("2026-11-19 08:30"), T0)  # the 08:00 message is a tombstone
    conn.close()

    result = runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19 07:00"])
    assert result.exit_code == 0, result.output
    assert "3 message(s)" in result.stdout
    assert [m["extracted"] for m in all_messages(db)] == [1, 0, 0, 0]

    rule = AlwaysRule(50)
    second = App(db, sheet=google_sheet(FakeWorksheet(header=list(HEADER))), page=NO_CHAT, rule=rule)
    assert await second.run(max_ticks=4) == EXIT_OK
    assert rule.seen == [m["message_key"] for m in msgs[1:]]
    assert [(o.message_key, o.number) for o in all_orders(db)] == [
        (msgs[0]["message_key"], 7),
        (msgs[1]["message_key"], 1),
        (msgs[3]["message_key"], 3),
        (msgs[2]["message_key"], 50),  # the only message without an order gets one now
    ]
    assert [m["extracted"] for m in all_messages(db)] == [1, 1, 1, 1]


async def test_r7_3_since_is_inclusive_and_in_tz_display(cli_env: Path):
    """R7.3: `--since 'YYYY-MM-DD HH:MM'` is read in TZ_DISPLAY and includes a message sent exactly then."""
    db = cli_env
    msgs = [bubble(at("2026-11-19 09:59"), "1001", "a"), bubble(at("2026-11-19 10:00"), "1001", "b")]
    first = App(db, sheet=google_sheet(FakeWorksheet(header=list(HEADER))), page=FakeChat(msgs))
    assert await first.run(max_ticks=3) == EXIT_OK
    result = runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19 10:00"])
    assert result.exit_code == 0, result.output
    assert [m["extracted"] for m in all_messages(db)] == [1, 0]


def test_r7_4_start_holds_the_lock_and_reprocess_refuses_meanwhile(cli_env: Path, monkeypatch):
    """R7.4: while `start` runs it holds the lock in data/, and both `reprocess` forms refuse (non-zero exit,
    nothing changed)."""
    db = cli_env
    seen: dict[str, object] = {}

    class ProbingDriver(FakeDriver):
        def eval_json(self, js: str):
            if "probe" not in seen:
                try:
                    AppLock(lock_path_for(db)).acquire()
                    seen["probe"] = "free"
                except LockHeld:
                    seen["probe"] = "held"
            return super().eval_json(js)

    monkeypatch.setattr(cli, "build_driver", lambda settings, selectors: ProbingDriver(NO_CHAT))
    monkeypatch.setattr(cli, "build_sheet", lambda settings: google_sheet(FakeWorksheet(header=["bad"])))
    assert runner.invoke(cli.app, ["start"]).exit_code != 0
    assert seen["probe"] == "held"
    assert lock_path_for(db).parent == db.parent

    conn = open_database(db)
    MessagesRepo(conn).ensure_conversation("g1", start_ms=0, now_ms=T0)
    conn.close()
    with AppLock(lock_path_for(db)):  # exactly what a running `start` holds
        by_order = runner.invoke(cli.app, ["reprocess", "--order", "1"])
        by_conv = runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19"])
    for result in (by_order, by_conv):
        assert result.exit_code != 0
        assert "start is running" in result.stderr
    assert runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19"]).exit_code == 0
