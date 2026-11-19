"""R6 — Operations, privacy (acceptance)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acceptance.harness import (
    DAY_MS,
    REPO_ROOT,
    T0,
    App,
    FakePlaywrightCli,
    FakeWorksheet,
    OffsetClock,
    all_messages,
    all_orders,
    google_sheet,
)
from fake_driver import NO_CHAT, FakeChat, FakeDriver, bubble
from fakes import FakeMediator
from order_extractor import cli
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import Message
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder.sheet import HEADER
from order_extractor.status import StatusPrinter
from order_extractor.telemetry import configure_logging
from order_extractor.zalo.browser import BrowserDriver
from order_extractor.zalo.messages_repo import MessagesRepo
from order_extractor.zalo.reader import build_reader_script, load_selectors
from order_extractor.zalo.zalo_web import ZaloWeb

TEXT = "Shop ơi cho em 2 áo thun đen size M"
TITLE = "Nhóm khách sỉ"
PRIVATE = ["áo thun", "Lan", TITLE]  # message text, sender name, conversation title


def example_chat() -> FakeChat:
    return FakeChat([bubble(T0, "1001", TEXT, sender="Lan"), bubble(T0 + 1_000, "1001", "cảm ơn shop")], title=TITLE)


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --- R6.1 / R6.5: structured logs, stdout ---------------------------------------------------------------------


async def test_r6_1_r6_5_logs_are_json_with_tick_id_text_never_at_info_stdout_only_status(db: Path, capfd):
    """R6.1, R6.5: a few ticks (with one Sheets failure) → every log line, in LOG_DIR and on stderr, is a JSON object
    with a tick_id; message text, sender and title never appear at INFO; stdout carries only the status line."""
    log_dir = db.parent / "logs"
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "error")
    app = App(
        db,
        sheet=google_sheet(ws),
        page=example_chat(),
        printer=StatusPrinter(),
        record_backoff_base_seconds=0.001,
        record_backoff_max_seconds=0.001,
    )
    configure_logging(log_dir, 30)  # after the DB is open: this test is about the running loop
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=200) == EXIT_OK
    captured = capfd.readouterr()

    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert out_lines and all(line.startswith("Zalo: ") for line in out_lines)
    assert all("{" not in line for line in out_lines)

    err = json_lines(captured.err)
    assert err, "the Sheets failure is a WARNING and must reach stderr"
    assert all(e["level"] in ("WARNING", "ERROR", "CRITICAL") for e in err)

    file_text = (log_dir / "app.jsonl").read_text(encoding="utf-8")
    records = json_lines(file_text)
    assert records
    for record in [*records, *err]:
        assert {"ts", "level", "event", "tick_id"} <= record.keys()
        assert record["tick_id"], record
    for needle in PRIVATE:
        assert needle not in file_text and needle not in captured.err
    events = {r["event"]: r for r in records}
    assert "conversation_key" in events["new_messages"]
    assert {"conversation_key", "message_key"} <= events["order_found"].keys()
    assert "order_id" in events["order_recorded"]
    ticks = {r["tick_id"] for r in records}
    assert len(ticks) > 1  # a new id per tick


@pytest.mark.xfail(
    strict=True,
    reason="BUG: `start` (and `reprocess`) log before any tick is set: migration_applied from open_database() "
    "(cli.py:147 → db/connection.py:107) is written with tick_id null (R6.5)",
)
def test_r6_5_every_line_of_a_cli_start_carries_a_tick_id(cli_env: Path, monkeypatch):
    """R6.5 through `order-extractor start`: every line in LOG_DIR/app.jsonl carries a tick_id."""
    monkeypatch.setattr(cli, "build_sheet", lambda settings: google_sheet(FakeWorksheet(header=["x"])))
    monkeypatch.setattr(cli, "build_driver", lambda settings, selectors: FakeDriver(NO_CHAT))
    result = CliRunner().invoke(cli.app, ["start"])
    assert result.exit_code != 0  # header mismatch stops it
    records = json_lines((cli_env.parent / "logs" / "app.jsonl").read_text(encoding="utf-8"))
    missing = [r["event"] for r in records if not r.get("tick_id")]
    assert missing == []


def test_r6_1_no_secret_in_logs_or_console(cli_env: Path, monkeypatch):
    """R6.1: configuration comes from the environment and the SHEET_ID / service-account content never reach the
    logs, stderr or stdout."""
    monkeypatch.setattr(cli, "build_sheet", lambda settings: google_sheet(FakeWorksheet(header=["x"])))
    monkeypatch.setattr(cli, "build_driver", lambda settings, selectors: FakeDriver(example_chat()))
    result = CliRunner().invoke(cli.app, ["start"])
    log_text = (cli_env.parent / "logs" / "app.jsonl").read_text(encoding="utf-8")
    secret = os.environ["SHEET_ID"]
    for text in (log_text, result.stdout, result.stderr):
        assert secret not in text
    assert secret not in repr(Settings.load(env_file=None))


async def test_r6_2_message_text_leaves_only_as_the_messages_column(db: Path):
    """R6.2: the only outbound copy of a message text is column E (`messages`) of an appended order row; a message
    that is not an order is never sent anywhere."""
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=example_chat())
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK
    for name, kwargs in ws.calls:
        for key, value in kwargs.items():
            if name == "append_rows" and key == "values":
                assert all(TEXT not in str(cell) for row in value for cell in row[:4] + row[5:])
                assert all("cảm ơn" not in str(cell) for row in value for cell in row)
            else:
                assert TEXT not in str(value)
    assert [row[4] for row in ws.data] == [TEXT]


# --- R6.3: retention purge -------------------------------------------------------------------------------------


async def test_r6_3_purge_tombstones_and_a_backfill_past_them_neither_reinserts_nor_reorders(db: Path):
    """R6.3: after RETENTION_DAYS the startup purge turns checked messages into tombstones (keys kept, text and
    sender gone) and drops the copied content of RECORDED orders; a backfill that re-shows the purged messages
    inserts nothing, re-runs no rule and appends no row."""
    ws = FakeWorksheet(header=list(HEADER))
    chat = example_chat()
    first = App(db, sheet=google_sheet(ws), page=chat, clock=OffsetClock(T0 + 3_600_000))
    assert await first.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK

    rule_seen: list[str] = []
    second = App(db, sheet=google_sheet(ws), page=chat, clock=OffsetClock(T0 + 31 * DAY_MS))
    second.rule.seen = rule_seen  # type: ignore[attr-defined]
    assert await second.run(max_ticks=5) == EXIT_OK

    msgs = all_messages(db)
    assert len(msgs) == 2
    for m in msgs:
        assert (m["text"], m["sender"]) == (None, None)
        assert m["purged_at"] is not None and m["extracted"] == 1
        assert m["message_key"] and m["sender_uid"] == "1001" and m["sent_at"] in (T0, T0 + 1_000)
    [order] = all_orders(db)
    assert (order.text, order.sender, order.conversation_title) == (None, None, None)
    assert (order.status, order.number, order.message_key) == ("RECORDED", 2, f"{T0}_1001")
    assert rule_seen == []
    assert len(ws.data) == 1 and ws.count("append_rows") == 1


async def test_r6_3_new_orders_keep_their_content_until_recorded(db: Path):
    """R6.3: an order that is still NEW when its message passes the retention window keeps its copy, so the row
    written later is complete."""
    down = FakeWorksheet(header=list(HEADER))
    down.fault("row_values", "error", times=10_000)
    first = App(db, sheet=google_sheet(down), page=example_chat(), clock=OffsetClock(T0 + 3_600_000))
    assert await first.run(max_ticks=5) == EXIT_OK
    assert [o.status for o in all_orders(db)] == ["NEW"]

    up = FakeWorksheet(header=list(HEADER))
    second = App(db, sheet=google_sheet(up), page=NO_CHAT, clock=OffsetClock(T0 + 40 * DAY_MS))
    assert await second.run(until=lambda: len(up.data) == 1, max_ticks=100) == EXIT_OK
    assert up.data[0][2:5] == [TITLE, "Lan", TEXT]
    assert {m["text"] for m in all_messages(db)} == {None}  # the messages themselves were purged


def test_r6_3_unchecked_messages_are_not_purged_and_old_snapshots_are(db: Path):
    """R6.3: a message the rule has not checked keeps its text past the window; a sender name with no live message
    left is dropped; DOM snapshots older than the window are deleted, newer ones kept."""
    conn = open_database(db)
    repo = MessagesRepo(conn)
    repo.ensure_conversation("g1", start_ms=0, now_ms=T0)
    repo.upsert_senders({"1001": "Lan", "1002": "Minh"}, T0)
    for i, (uid, checked) in enumerate([("1001", True), ("1001", False), ("1002", True)]):
        m = Message(conversation_key="g1", message_key=f"{T0 + i}_{uid}", sent_at_ms=T0 + i, sender_uid=uid)
        m.text, m.sender = f"text {i}", "x"
        repo.insert_message(m, extracted=checked, observed_at_ms=T0)
    snapshots = db.parent / "snapshots"
    snapshots.mkdir(parents=True)
    old, new = snapshots / "old.yml", snapshots / "new.yml"
    old.write_text("x")
    new.write_text("y")
    now = T0 + 31 * DAY_MS
    os.utime(old, ((T0 - DAY_MS) / 1000, (T0 - DAY_MS) / 1000))
    os.utime(new, ((now - DAY_MS) / 1000, (now - DAY_MS) / 1000))

    zalo = ZaloWeb(FakeMediator(), FakeDriver(NO_CHAT), repo, Settings(db_path=db), reader_script="x")
    zalo.purge(now)
    texts = [(r["extracted"], r["text"]) for r in conn.execute("SELECT extracted, text FROM messages ORDER BY id")]
    assert texts == [(1, None), (0, "text 1"), (1, None)]
    assert [r["uid"] for r in conn.execute("SELECT uid FROM senders")] == ["1001"]  # 1002 has no live message
    assert not old.exists() and new.exists()
    conn.close()


async def test_r6_3_purge_runs_at_startup_and_then_once_a_day(db: Path):
    """R6.3: the purge also runs while the tool keeps running: a message crossing the window a day later is purged
    without a restart."""
    clock = OffsetClock(T0 + 29 * DAY_MS + DAY_MS // 2)
    app = App(db, sheet=google_sheet(FakeWorksheet(header=list(HEADER))), page=example_chat(), clock=clock)
    texts: dict[int, set] = {}

    def tick(n: int) -> None:
        texts[n] = {m["text"] for m in all_messages(db)}
        if n == 3:
            clock.jump(DAY_MS)

    app.on_tick.append(tick)
    assert await app.run(max_ticks=6) == EXIT_OK
    assert texts[3] == {TEXT, "cảm ơn shop"}  # not old enough at startup
    assert texts[6] == {None}


# --- R6.4: status line -----------------------------------------------------------------------------------------


async def test_r6_4_status_line_shows_state_conversation_and_counts(db: Path):
    """R6.4: the stdout status line shows login state, conversation, messages read, orders found / recorded /
    failed."""
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=example_chat())
    assert await app.run(until=lambda: len(ws.data) == 1, max_ticks=100) == EXIT_OK
    last = app.status_lines[-1]
    assert TITLE in last and "reading" in last
    for part in ("read 2", "found 1", "recorded 1", "failed 0"):
        assert part in last, (part, last)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: if `playwright-cli eval` prints anything besides the JSON (e.g. a notice line), EvalError embeds "
    "the first 200 chars of the reader output, which hold the conversation title, and ZaloWeb logs it at ERROR "
    "to app.jsonl and stderr (zalo/browser.py:164, zalo/zalo_web.py:339; R6.1, design §8)",
)
async def test_r6_1_chat_content_never_reaches_the_logs_when_the_reader_output_is_unparsable(db: Path, chromium, capfd):
    """R6.1 / design §8: even when the browser output cannot be parsed, chat titles, sender names and message text
    are not written to the logs above DEBUG."""
    selectors = load_selectors(REPO_ROOT / "selectors.json")
    script = build_reader_script(selectors)
    chromium.load("group_chat.html")

    def noisy(js: str):
        value = chromium.evaluate(js)
        return f"Update available: playwright-cli 0.2.0\n{value}" if js == script else value

    driver = BrowserDriver(
        Settings(db_path=db), scroller_selector=selectors["scroller"], runner=FakePlaywrightCli(noisy)
    )
    app = App(db, sheet=google_sheet(FakeWorksheet(header=list(HEADER))), driver=driver, reader_script=script)
    log_dir = db.parent / "logs"
    configure_logging(log_dir, 30)
    assert await app.run(max_ticks=3) == EXIT_OK
    logged = (log_dir / "app.jsonl").read_text(encoding="utf-8") + capfd.readouterr().err
    assert "browser_died" in logged
    for needle in ("Nhóm Khách Sỉ Test", "Lan", "Minh", "Hoa"):
        assert needle not in logged, needle
