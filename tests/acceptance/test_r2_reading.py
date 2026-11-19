"""R2 — Reading messages one by one (acceptance)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from acceptance.harness import (
    DAY_MS,
    REPO_ROOT,
    T0,
    App,
    FakePlaywrightCli,
    FakeWorksheet,
    OffsetClock,
    PageWorker,
    all_messages,
    all_orders,
    at,
    chat_evaluator,
    db_rows,
    google_sheet,
)
from fake_driver import FakeChat, bubble, reading
from fake_sheet import InMemorySheet
from order_extractor.config import Settings
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder.sheet import HEADER
from order_extractor.zalo.browser import BrowserDriver
from order_extractor.zalo.reader import build_reader_script, load_selectors
from rules_for_tests import FirstIntegerRule

SELECTORS = load_selectors(REPO_ROOT / "selectors.json")
GROUP = "g9000000000000000001"


def sheet() -> InMemorySheet:
    return InMemorySheet(header=list(HEADER))


def history(n: int, start: int, uid: str = "1001", name: str = "Lan", step: int = 60_000) -> list[dict]:
    """`n` customer messages, one a minute from `start`, each with a number in it."""
    return [bubble(start + i * step, uid, f"lấy {i + 1} cái", sender=name) for i in range(n)]


# --- R2.1 / R2.3: poll, de-duplicate ------------------------------------------------------------------------


async def test_r2_1_r2_3_same_page_polled_many_times_is_stored_once(db: Path):
    """R2.1, R2.3: the same rendered page polled 10 times → each message stored and checked by the rule once."""
    rule = FirstIntegerRule()
    chat = FakeChat(history(3, T0), window=5)
    app = App(db, sheet=sheet(), page=chat, rule=rule)
    assert await app.run(max_ticks=10) == EXIT_OK

    assert len(all_messages(db)) == 3
    assert sorted(rule.seen) == sorted({m["message_key"] for m in chat.messages})
    assert len(rule.seen) == 3
    assert len(app.of_type("NewMessages")) == 1
    assert app.driver.evals >= 10


async def test_r2_1_r2_10_a_new_bubble_is_stored_and_announced_once(db: Path):
    """R2.1, R2.10: a bubble that appears later is stored once and announced in exactly one NewMessages."""
    chat = FakeChat(history(2, T0))
    app = App(db, sheet=sheet(), page=chat)
    late = bubble(T0 + 600_000, "1002", "cho 5 hộp", sender="Minh")
    app.on_tick.append(lambda n: chat.append(late) if n == 4 else None)
    assert await app.run(max_ticks=10) == EXIT_OK

    batches = [[m.message_key for m in e.messages] for e in app.of_type("NewMessages")]
    assert batches[-1] == [late["message_key"]]
    assert sum(late["message_key"] in b for b in batches) == 1
    assert len(all_messages(db)) == 3


async def test_r2_3_key_is_unique_per_conversation_not_globally(db: Path):
    """R2.3: de-duplication is on (conversation_key, message_key); the same key in another chat is a new message."""
    same = bubble(T0, "1001", "cho 1 cái", sender="Lan")
    chat1, chat2 = FakeChat([same], key="g1"), FakeChat([same], key="g2", title="Nhóm 2")
    app = App(db, sheet=sheet(), page=chat1)
    app.on_tick.append(lambda n: setattr(app.driver, "page", chat2) if n == 3 else None)
    assert await app.run(max_ticks=6) == EXIT_OK
    assert sorted((m["conversation_key"], m["message_key"]) for m in all_messages(db)) == [
        ("g1", same["message_key"]),
        ("g2", same["message_key"]),
    ]
    assert len(all_orders(db)) == 2


# --- R2.2: chronological, one at a time ---------------------------------------------------------------------


async def test_r2_2_live_messages_are_processed_oldest_first(db: Path):
    """R2.2: bubbles that render out of order in one poll are stored and checked in send-time order."""
    rule = FirstIntegerRule()
    chat = FakeChat(history(1, T0))
    app = App(db, sheet=sheet(), page=chat, rule=rule)
    newer, older = bubble(T0 + 120_000, "1002", "lấy 9", sender="B"), bubble(T0 + 60_000, "1003", "lấy 8", sender="C")
    app.on_tick.append(lambda n: chat.append(newer, older) if n == 3 else None)
    assert await app.run(max_ticks=6) == EXIT_OK

    assert rule.seen[-2:] == [older["message_key"], newer["message_key"]]
    orders = all_orders(db)
    assert [o.number for o in orders] == [1, 8, 9]  # order ids follow send time
    stored = db_rows(db, "SELECT message_key FROM messages ORDER BY id")
    assert [r["message_key"] for r in stored][-2:] == [older["message_key"], newer["message_key"]]


@pytest.mark.xfail(
    strict=True,
    reason="BUG (design-accepted deviation, design §4.4): backfill emits each screen as it scrolls up, so the "
    "rule sees newer screens before older ones and order ids are not chronological (R2.2)",
)
async def test_r2_2_backfilled_history_is_processed_oldest_first(db: Path):
    """R2.2: messages gathered by a multi-screen backfill reach the rule in chronological order."""
    rule = FirstIntegerRule()
    chat = FakeChat(history(12, T0), window=5)
    app = App(db, sheet=sheet(), page=chat, rule=rule)
    assert await app.run(max_ticks=4) == EXIT_OK
    assert len(rule.seen) == 12
    assert rule.seen == [m["message_key"] for m in chat.messages]


# --- R2.4: backfill stop conditions -------------------------------------------------------------------------


async def test_r2_4_backfill_reads_to_the_start_of_the_chat_then_returns_to_the_bottom(db: Path):
    """R2.4: with no other stop condition the backfill scrolls to the start of the chat and back to the bottom."""
    chat = FakeChat(history(23, T0), window=5)
    app = App(db, sheet=sheet(), page=chat)
    assert await app.run(max_ticks=2) == EXIT_OK
    assert len(all_messages(db)) == 23
    assert app.driver.scrolled_up >= 4
    assert app.driver.scrolls[-1] == "bottom" and chat.at_bottom


async def test_r2_4_backfill_stops_after_backfill_max_messages(db: Path):
    """R2.4: BACKFILL_MAX_MESSAGES bounds one backfill; the list is then returned to the bottom."""
    chat = FakeChat(history(60, T0), window=5)
    app = App(db, sheet=sheet(), page=chat, backfill_max_messages=12)
    assert await app.run(max_ticks=2) == EXIT_OK
    stored = len(all_messages(db))
    assert 12 <= stored < 12 + 5
    assert app.driver.scrolls[-1] == "bottom"
    newest = {m["message_key"] for m in chat.messages[-stored:]}
    assert {m["message_key"] for m in all_messages(db)} == newest  # the newest ones, contiguous


async def test_r2_4_r2_11_backfill_stops_below_the_start_watermark(db: Path):
    """R2.4, R2.11: backfill stops once the oldest rendered bubble predates the watermark; the older bubbles it
    saw are stored as checked and never reach the rule."""
    rule = FirstIntegerRule()
    msgs = history(40, T0)
    since = msgs[25]["sent_at_ms"]
    chat = FakeChat(msgs, window=5)
    app = App(db, sheet=sheet(), page=chat, rule=rule, since_ms=since)
    assert await app.run(max_ticks=2) == EXIT_OK

    stored = all_messages(db)
    assert len(stored) < 40  # it did not scroll to the start of the chat
    assert {m["message_key"] for m in msgs[25:]} <= {m["message_key"] for m in stored}
    assert set(rule.seen) == {m["message_key"] for m in msgs[25:]}
    assert all(m["extracted"] == 1 for m in stored if m["sent_at"] < since)
    assert sorted(o.number for o in all_orders(db)) == list(range(26, 41))


async def test_r2_4_restart_backfill_stops_at_the_oldest_stored_bubble(db: Path):
    """R2.4: after a restart the first READING poll backfills; when the oldest rendered bubble is already stored it
    does not scroll up, and only the messages that arrived while down are new."""
    chat = FakeChat(history(10, T0), window=5)
    first = App(db, sheet=sheet(), page=chat)
    assert await first.run(max_ticks=2) == EXIT_OK
    chat.append(*history(2, T0 + 3_600_000, uid="1002", name="Minh"))

    rule = FirstIntegerRule()
    second = App(db, sheet=sheet(), page=chat, rule=rule)
    assert await second.run(max_ticks=3) == EXIT_OK
    assert second.driver.scrolled_up == 0
    assert len(rule.seen) == 2
    assert len(all_messages(db)) == 12


async def test_r2_4_restart_backfill_recovers_messages_that_arrived_while_down(db: Path):
    """R2.4: more messages than one screen arrived while the tool was down → backfill scrolls up to the gap."""
    chat = FakeChat(history(6, T0), window=5)
    first = App(db, sheet=sheet(), page=chat)
    assert await first.run(max_ticks=2) == EXIT_OK
    chat.append(*history(9, T0 + 3_600_000, uid="1002", name="Minh"))

    rule = FirstIntegerRule()
    second = App(db, sheet=sheet(), page=chat, rule=rule)
    assert await second.run(max_ticks=3) == EXIT_OK
    assert len(all_messages(db)) == 15
    assert len(rule.seen) == 9
    assert second.driver.scrolled_up >= 1


# --- R2.5 / R2.11: conversations and the start watermark -----------------------------------------------------


async def test_r2_11_first_open_history_creates_no_orders(db: Path):
    """R2.11: without --since the watermark is the moment the chat is first opened: history is stored as checked,
    never reaches the rule, creates no rows; a message sent afterwards does."""
    rule = FirstIntegerRule()
    clock = OffsetClock(T0 + 3_600_000)
    chat = FakeChat(history(5, T0), window=10)
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=chat, rule=rule, since_ms=None, clock=clock)
    live: dict = {}

    def customer(n: int) -> None:
        if n == 3:
            live.update(bubble(clock(), "1001", "thêm 3 cái", sender="Lan"))
            chat.append(live)

    app.on_tick.append(customer)
    assert await app.run(until=lambda: len(ws.data) >= 1, max_ticks=100) == EXIT_OK

    stored = all_messages(db)
    assert len(stored) == 6 and all(m["extracted"] == 1 for m in stored)
    assert rule.seen == [live["message_key"]]
    assert [row[4] for row in ws.data] == ["thêm 3 cái"]
    conv = db_rows(db, "SELECT start_ms, first_seen FROM conversations")[0]
    assert T0 + 3_600_000 <= conv["start_ms"] <= T0 + 3_660_000


async def test_r2_11_watermark_is_never_changed_by_a_later_since(db: Path):
    """R2.11: a later `start --since` does not move the watermark of a conversation that is already known."""
    clock = OffsetClock(T0 + 3_600_000)
    chat = FakeChat(history(4, T0), window=10)
    first = App(db, sheet=sheet(), page=chat, since_ms=None, clock=clock)
    assert await first.run(max_ticks=2) == EXIT_OK
    start_ms = db_rows(db, "SELECT start_ms FROM conversations")[0]["start_ms"]

    rule = FirstIntegerRule()
    second = App(db, sheet=sheet(), page=chat, rule=rule, since_ms=T0 - DAY_MS)
    assert await second.run(max_ticks=3) == EXIT_OK
    assert db_rows(db, "SELECT start_ms FROM conversations")[0]["start_ms"] == start_ms
    assert rule.seen == [] and all_orders(db) == []


async def test_r2_5_r2_11_switching_conversation_reads_the_new_one_with_its_own_watermark(db: Path):
    """R2.5, R2.11: the human switches chats; the new chat is backfilled with a watermark set when it is first
    opened, so its history creates no orders but a new message in it does."""
    clock = OffsetClock(T0 + 3_600_000)
    g1 = FakeChat(history(3, T0), key="g1", title="Nhóm 1")
    g2 = FakeChat(history(4, T0 - DAY_MS, uid="2001", name="Hà"), key="g2", title="Nhóm 2")
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=g1, since_ms=None, clock=clock)

    def human(n: int) -> None:
        if n == 2:
            clock.jump(600_000)
            app.driver.page = g2
        if n == 5:
            g2.append(bubble(clock(), "2001", "đặt 6 thùng", sender="Hà"))

    app.on_tick.append(human)
    assert await app.run(until=lambda: len(ws.data) >= 1, max_ticks=100) == EXIT_OK

    convs = {c["key"]: c["start_ms"] for c in db_rows(db, "SELECT key, start_ms FROM conversations")}
    assert set(convs) == {"g1", "g2"}
    assert convs["g2"] >= convs["g1"] + 600_000
    assert len(all_messages(db)) == 8
    assert [(row[2], row[3], row[5]) for row in ws.data] == [("Nhóm 2", "Hà", 6)]
    ready = [e.conversation.key for e in app.of_type("ZaloReady")]
    assert ready == ["g1", "g2"]


async def test_r2_11_since_applies_to_conversations_opened_later_in_the_run(db: Path):
    """R2.11: `--since` is the watermark of every conversation first seen in the run, including one the human
    opens later: its messages at/after --since become orders, older ones do not."""
    since = at("2026-11-18 00:00")
    clock = OffsetClock(T0 + 3_600_000)
    g1 = FakeChat([bubble(at("2026-11-19 09:00"), "1001", "lấy 1", sender="Lan")], key="g1")
    g2_msgs = [
        bubble(at("2026-11-17 09:00"), "2001", "lấy 70", sender="Hà"),
        bubble(at("2026-11-18 09:00"), "2001", "lấy 80", sender="Hà"),
        bubble(at("2026-11-19 09:00"), "2002", "lấy 90", sender="Tú"),
    ]
    g2 = FakeChat(g2_msgs, key="g2")
    app = App(db, sheet=sheet(), page=g1, since_ms=since, clock=clock)
    app.on_tick.append(lambda n: setattr(app.driver, "page", g2) if n == 4 else None)
    assert await app.run(max_ticks=8) == EXIT_OK

    convs = {c["key"]: c["start_ms"] for c in db_rows(db, "SELECT key, start_ms FROM conversations")}
    assert convs == {"g1": since, "g2": since}
    assert sorted(o.number for o in all_orders(db)) == [1, 80, 90]


# --- R2.6 / R2.7 / R2.8: kinds, senders, self ----------------------------------------------------------------


async def test_r2_6_non_text_messages_are_stored_as_placeholders_without_processing(db: Path):
    """R2.6: image / sticker / file / voice bubbles are stored with their placeholder and never reach the rule."""
    rule = FirstIntegerRule()
    msgs = [
        bubble(T0, "1001", "[image]", sender="Lan", kind="image"),
        bubble(T0 + 1, "1001", "[sticker]", kind="sticker"),
        bubble(T0 + 2, "1001", "[file]", kind="file"),
        bubble(T0 + 3, "1001", "[voice]", kind="voice"),
        bubble(T0 + 4, "1001", "lấy 2", kind="text"),
    ]
    app = App(db, sheet=sheet(), page=FakeChat(msgs), rule=rule)
    assert await app.run(max_ticks=3) == EXIT_OK
    stored = {m["kind"]: m["text"] for m in all_messages(db)}
    assert stored == {"image": "[image]", "sticker": "[sticker]", "file": "[file]", "voice": "[voice]", "text": "lấy 2"}
    assert rule.seen == [msgs[-1]["message_key"]]


async def test_r2_7_bubble_without_a_name_header_gets_the_stored_sender_name(db: Path):
    """R2.7: once a uid's name has been seen, a later bubble rendered without a name header (header scrolled
    away) is stored, and recorded, with that name."""
    chat = FakeChat([bubble(T0, "1001", "chào shop", sender="Lan")], window=1)
    ws = FakeWorksheet(header=list(HEADER))
    app = App(db, sheet=google_sheet(ws), page=chat)
    nameless = bubble(T0 + 60_000, "1001", "cho em 4 cái", sender=None)
    app.on_tick.append(lambda n: chat.append(nameless) if n == 3 else None)  # window=1: Lan's header is gone
    assert await app.run(until=lambda: len(ws.data) >= 1, max_ticks=100) == EXIT_OK

    assert [m["sender"] for m in all_messages(db)] == ["Lan", "Lan"]
    assert [row[3] for row in ws.data] == ["Lan"]


async def test_r2_7_unknown_sender_is_stored_as_uid_and_only_later_rows_get_the_name(db: Path):
    """R2.7: with no known name the sender is stored as the uid; when the name appears later only later rows use
    it, the earlier row is not rewritten."""
    chat = FakeChat([bubble(T0, "1009", "lấy 1", sender=None)], window=1)
    app = App(db, sheet=sheet(), page=chat)
    app.on_tick.append(lambda n: chat.append(bubble(T0 + 60_000, "1009", "lấy 2", sender="Tú")) if n == 3 else None)
    assert await app.run(max_ticks=6) == EXIT_OK
    assert [m["sender"] for m in all_messages(db)] == ["1009", "Tú"]
    assert [(o.number, o.sender) for o in all_orders(db)] == [(1, "1009"), (2, "Tú")]


async def test_r2_8_self_messages_are_from_the_shop(db: Path):
    """R2.8: uid 0 (is_self) is the shop; it is stored as self and never becomes an order."""
    rule = FirstIntegerRule()
    chat = FakeChat([bubble(T0, "0", "shop gửi 3 mẫu"), bubble(T0 + 1, "1001", "lấy 3", sender="Lan")])
    app = App(db, sheet=sheet(), page=chat, rule=rule)
    assert await app.run(max_ticks=3) == EXIT_OK
    stored = {m["sender_uid"]: m["is_self"] for m in all_messages(db)}
    assert stored == {"0": 1, "1001": 0}
    assert len(rule.seen) == 1 and [o.sender for o in all_orders(db)] == ["Lan"]


# --- R2.9: selector drift ------------------------------------------------------------------------------------


async def test_r2_9_reader_exception_logs_selectors_may_be_outdated_with_a_snapshot(db: Path, caplog):
    """R2.9: the reader throws → WARNING "selectors may be outdated" with the DOM snapshot path."""
    caplog.set_level(logging.WARNING, logger="order_extractor")
    app = App(db, sheet=sheet(), page={"state": "ERROR", "error": "Cannot read properties of null"})
    assert await app.run(max_ticks=3) == EXIT_OK
    drift = [r for r in caplog.records if "selectors may be outdated" in r.getMessage()]
    assert drift and getattr(drift[0], "snapshot_path", None)
    assert any("selectors may be outdated" in line for line in app.status_lines)


async def test_r2_9_zero_messages_for_selector_alert_seconds_logs_drift(db: Path, caplog):
    """R2.9: an open conversation where the reader finds 0 messages for SELECTOR_ALERT_SECONDS → drift alert,
    but not before."""
    caplog.set_level(logging.WARNING, logger="order_extractor")
    clock = OffsetClock(T0)
    page = reading([], key="g1")
    page["rendered_bubbles"] = 6
    app = App(db, sheet=sheet(), page=page, clock=clock, selector_alert_seconds=60)
    seen_at: list[int] = []

    def tick(n: int) -> None:
        clock.jump(10_000)
        if any("selectors may be outdated" in r.getMessage() for r in caplog.records) and not seen_at:
            seen_at.append(n)

    app.on_tick.append(tick)
    assert await app.run(max_ticks=10) == EXIT_OK
    assert seen_at, "no drift alert"
    assert 6 <= seen_at[0] <= 8  # ~60 s of empty reads at 10 s per tick
    drift = [r for r in caplog.records if "selectors may be outdated" in r.getMessage()]
    assert getattr(drift[0], "snapshot_path", None)


async def test_r2_9_real_reader_bubbles_without_parsable_qid_alert_after_selector_alert_seconds(
    db: Path, chromium: PageWorker, caplog
):
    """R2.9 with the real reader.js: bubbles render but none carries a parsable data-qid (selectors outdated) →
    nothing is stored and, after SELECTOR_ALERT_SECONDS, "selectors may be outdated" is logged with a snapshot."""
    caplog.set_level(logging.WARNING, logger="order_extractor")
    chromium.load("no_qid.html")
    cli = FakePlaywrightCli(chromium.evaluate)
    driver = BrowserDriver(Settings(db_path=db), scroller_selector=SELECTORS["scroller"], runner=cli)
    clock = OffsetClock(T0)
    app = App(db, sheet=sheet(), driver=driver, clock=clock, reader_script=build_reader_script(SELECTORS))
    app.on_tick.append(lambda n: clock.jump(15_000))
    assert await app.run(max_ticks=8) == EXIT_OK
    assert all_messages(db) == []
    drift = [r for r in caplog.records if "selectors may be outdated" in r.getMessage()]
    assert len(drift) == 1
    snapshot = Path(str(getattr(drift[0], "snapshot_path", "")))
    assert snapshot.is_file() and snapshot.parent == db.parent / "snapshots"


@pytest.mark.xfail(
    strict=True,
    reason="BUG (gap in R2.3 itself): the no-qid fallback key hashes the bubble's ordinal among *rendered* "
    "bubbles, so when the virtualized list drops a bubble above it the same message gets a new key and is "
    "stored, checked and turned into an order a second time (reader.js:101; R2.3, R4.3 'at most one order')",
)
async def test_r2_3_bubble_without_qid_is_stored_once_when_the_rendered_window_shifts(db: Path, chromium: PageWorker):
    """R2.3: a bubble without data-qid (fallback key) is still read exactly once while the list virtualizes."""
    chromium.load("group_chat.html")
    chromium.evaluate(
        "() => document.querySelector('[data-qid^=\"5000000000003@\"]').removeAttribute('data-qid')"
    )  # Lan's "=SUM(1,2)" bubble renders without a qid
    cli = FakePlaywrightCli(chromium.evaluate)
    driver = BrowserDriver(Settings(db_path=db), scroller_selector=SELECTORS["scroller"], runner=cli)
    rule = FirstIntegerRule()
    app = App(db, sheet=sheet(), driver=driver, rule=rule, reader_script=build_reader_script(SELECTORS))
    drop_top = "() => document.querySelector('#messageViewContainer .chat-item').remove()"
    app.on_tick.append(lambda n: chromium.evaluate(drop_top) if n == 3 else None)  # virtualization unmounts it
    assert await app.run(max_ticks=6) == EXIT_OK
    texts = [m["text"] for m in all_messages(db)]
    assert texts.count("=SUM(1,2)\ndòng 2") == 1
    assert [o.number for o in all_orders(db)].count(1) == 1


# --- R2.10: one event, watermark applied ---------------------------------------------------------------------


async def test_r2_10_new_messages_event_excludes_pre_watermark_messages(db: Path):
    """R2.10: the NewMessages of a screen holds only stored messages at/after the watermark, oldest first."""
    since = T0
    msgs = [bubble(T0 - 120_000, "1001", "lấy 1", sender="Lan"), bubble(T0 + 60_000, "1001", "lấy 2")]
    msgs.append(bubble(T0, "1002", "lấy 3", sender="Minh"))
    app = App(db, sheet=sheet(), page=FakeChat(msgs), since_ms=since)
    assert await app.run(max_ticks=3) == EXIT_OK
    events = app.of_type("NewMessages")
    assert len(events) == 1
    assert [m.message_key for m in events[0].messages] == [msgs[2]["message_key"], msgs[1]["message_key"]]
    assert len(all_messages(db)) == 3


@pytest.mark.xfail(
    strict=True,
    reason="BUG (design-accepted deviation, design §4.4): a multi-screen backfill inside one poll emits one "
    "NewMessages per screen instead of one per poll (R2.10)",
)
async def test_r2_10_one_new_messages_event_per_poll_even_during_backfill(db: Path):
    """R2.10: the poll that first opens a chat (and backfills several screens) emits exactly one NewMessages."""
    app = App(db, sheet=sheet(), page=FakeChat(history(12, T0), window=5))
    assert await app.run(max_ticks=1) == EXIT_OK
    assert len(all_messages(db)) == 12
    assert len(app.of_type("NewMessages")) == 1


# --- R2.12: catch-up -----------------------------------------------------------------------------------------


async def test_r2_12_scrolled_up_then_back_at_bottom_with_a_gap_runs_catch_up(db: Path):
    """R2.12: messages that arrived while the human was scrolled up (more than one screen) are recovered by a
    catch-up backfill once the list is back at the bottom; each becomes one order."""
    chat = FakeChat(history(10, T0), window=5)
    app = App(db, sheet=sheet(), page=chat)
    gap = history(9, T0 + 3_600_000, uid="1002", name="Minh")

    def human(n: int) -> None:
        if n == 3:
            chat.scroll(-700)  # reading history
        if n == 5:
            chat.append(*gap)  # not followed: the human is scrolled up
        if n == 7:
            chat.to_bottom()

    app.on_tick.append(human)
    assert await app.run(max_ticks=12) == EXIT_OK
    keys = {m["message_key"] for m in all_messages(db)}
    assert {g["message_key"] for g in gap} <= keys
    assert len(keys) == 19
    assert len(all_orders(db)) == 19
    assert chat.at_bottom


async def test_r2_12_back_at_bottom_without_a_gap_does_not_scroll(db: Path):
    """R2.12: no gap (the oldest rendered bubble is stored) → no catch-up scrolling."""
    chat = FakeChat(history(10, T0), window=5)
    app = App(db, sheet=sheet(), page=chat)
    counted: dict[str, int] = {}

    def human(n: int) -> None:
        if n == 3:
            counted["before"] = app.driver.scrolled_up
            chat.scroll(-300)
        if n == 5:
            chat.to_bottom()
            chat.append(bubble(T0 + 3_600_000, "1002", "lấy 4", sender="Minh"))

    app.on_tick.append(human)
    assert await app.run(max_ticks=9) == EXIT_OK
    assert app.driver.scrolled_up == counted["before"]
    assert len(all_messages(db)) == 11


# --- R2.13: conversation key ---------------------------------------------------------------------------------


async def test_r2_13_conversation_key_comes_from_qid_not_title(db: Path):
    """R2.13: a renamed chat (same qid suffix) is the same conversation; another key with the same title is not."""
    chat = FakeChat(history(2, T0), key="g1", title="Nhóm A")
    app = App(db, sheet=sheet(), page=chat)

    def human(n: int) -> None:
        if n == 2:
            chat.title = "Nhóm A (đổi tên)"
        if n == 4:
            app.driver.page = FakeChat(history(1, T0, uid="3001", name="Long"), key="g2", title="Nhóm A (đổi tên)")

    app.on_tick.append(human)
    assert await app.run(max_ticks=6) == EXIT_OK
    assert sorted(c["key"] for c in db_rows(db, "SELECT key FROM conversations")) == ["g1", "g2"]
    assert [e.conversation.key for e in app.of_type("ZaloReady")] == ["g1", "g2"]


# --- real reader.js end to end -------------------------------------------------------------------------------


async def test_r2_real_reader_group_chat_end_to_end(db: Path, chromium: PageWorker):
    """R2.1, R2.6, R2.7, R2.8, R2.13, R4.2, R5.2 with the real reader.js in Chromium, the real BrowserDriver and the
    real Sheets adapter: the fixture group becomes exactly the expected rows."""
    chromium.load("group_chat.html")
    cli = FakePlaywrightCli(chromium.evaluate)
    driver = BrowserDriver(Settings(db_path=db), scroller_selector=SELECTORS["scroller"], runner=cli)
    rule = FirstIntegerRule()
    ws = FakeWorksheet()
    app = App(db, sheet=google_sheet(ws), driver=driver, rule=rule, reader_script=build_reader_script(SELECTORS))
    assert await app.run(until=lambda: len(ws.data) >= 2, max_ticks=100) == EXIT_OK

    stored = {m["message_key"]: m for m in all_messages(db)}
    assert len(stored) == 8
    assert {m["conversation_key"] for m in stored.values()} == {GROUP}
    assert stored["1794544700000_1004"]["sender"] == "1004"  # never named: stored as uid (R2.7)
    assert stored["1794544730000_1001"]["sender"] == "Lan"  # no header on this bubble (R2.7)
    assert stored["1794631120000_0"]["is_self"] == 1  # (R2.8)
    assert {stored[k]["text"] for k in ("1794544740000_1002", "1794544750000_1002", "1794631100000_1003")} == {
        "[image]",
        "[sticker]",
        "[file]",
    }
    text_customer = {k for k, m in stored.items() if m["kind"] == "text" and not m["is_self"]}
    assert set(rule.seen) == text_customer  # media and self never reach the rule (R4.2)
    rows = [row[1:] for row in ws.data]
    assert rows == [
        ["2026-11-13 11:38", "Nhóm Khách Sỉ Test", "Lan", "Shop ơi cho em 2 áo thun đen size M", 2, False],
        ["2026-11-13 11:38", "Nhóm Khách Sỉ Test", "Lan", "=SUM(1,2)\ndòng 2", 1, False],
    ]
    appends = [k for name, k in ws.calls if name == "append_rows"]
    assert all(str(k["value_input_option"]) in ("RAW", "ValueInputOption.raw") for k in appends)


async def test_r2_4_backfill_returns_to_the_bottom_even_after_a_transient_scroll_failure(db: Path):
    """R2.4, NFR1: one timed-out scroll-to-bottom at the end of a backfill must not stop live reading: a message
    sent afterwards is still stored within a few polls without any human action."""
    chat = FakeChat(history(12, T0), window=5)
    evaluate = chat_evaluator(chat)
    failures = {"left": 1}

    def flaky(js: str):
        if "if (true)" in js and failures["left"]:
            failures["left"] -= 1
            raise subprocess.TimeoutExpired(cmd="playwright-cli eval", timeout=10)
        return evaluate(js)

    cli = FakePlaywrightCli(flaky)
    driver = BrowserDriver(Settings(db_path=db), scroller_selector=SELECTORS["scroller"], runner=cli)
    app = App(db, sheet=sheet(), driver=driver)
    late = bubble(T0 + 3_600_000, "1002", "lấy 9 thùng", sender="Minh")
    app.on_tick.append(lambda n: chat.append(late) if n == 4 else None)
    assert await app.run(max_ticks=12) == EXIT_OK
    assert failures["left"] == 0
    assert late["message_key"] in {m["message_key"] for m in all_messages(db)}
