"""R4 — Order extraction (acceptance)."""

from __future__ import annotations

from pathlib import Path

from acceptance.harness import (
    T0,
    AlwaysRule,
    App,
    FakeWorksheet,
    all_messages,
    all_orders,
    google_sheet,
)
from fake_driver import FakeChat, bubble
from fake_sheet import InMemorySheet
from order_extractor import cli
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import Message
from order_extractor.extractor.rules import OrderRule
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder.sheet import HEADER
from order_extractor.zalo.messages_repo import MessagesRepo
from rules_for_tests import FirstIntegerRule

EXAMPLE = "Shop ơi cho em 2 áo thun đen size M"


async def test_r4_requirements_example_becomes_the_expected_sheet_row(db: Path):
    """§1 example, R4.1, R4.3, R5.1, R5.2, R5.4: "Lan: Shop ơi cho em 2 áo thun đen size M" in "Nhóm khách sỉ" at
    2026-11-19 10:38 → one row: order_id, time in TZ_DISPLAY, conversation, Lan, the text, 2, served unticked."""
    ws = FakeWorksheet()  # a fresh sheet: the header is created first
    chat = FakeChat([bubble(T0, "3808620345438131900", EXAMPLE, sender="Lan")], title="Nhóm khách sỉ")
    app = App(db, sheet=google_sheet(ws), page=chat, rule=FirstIntegerRule())
    assert await app.run(until=lambda: len(ws.data) >= 1, max_ticks=100) == EXIT_OK

    [order] = all_orders(db)
    assert ws.header == list(HEADER)
    assert ws.data == [[str(order.id), "2026-11-19 10:38", "Nhóm khách sỉ", "Lan", EXAMPLE, 2, False]]
    assert order.status == "RECORDED" and order.sheet_row == 2
    assert [e.order_id for e in app.of_type("OrderRecorded")] == [order.id]


async def test_r4_1_rule_is_applied_to_each_customer_text_message_in_order(db: Path):
    """R4.1: every customer text message reaches the rule once, oldest first; numbers become orders, others not."""
    msgs = [
        bubble(T0, "1001", "chào shop", sender="Lan"),
        bubble(T0 + 1_000, "1001", "lấy 2 cái"),
        bubble(T0 + 2_000, "1002", "cho 10 hộp", sender="Minh"),
    ]
    rule = FirstIntegerRule()
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), page=FakeChat(msgs), rule=rule)
    assert await app.run(max_ticks=3) == EXIT_OK
    assert rule.seen == [m["message_key"] for m in msgs]
    assert [(o.message_key, o.number) for o in all_orders(db)] == [
        (msgs[1]["message_key"], 2),
        (msgs[2]["message_key"], 10),
    ]


async def test_r4_2_shop_and_non_text_messages_never_produce_orders(db: Path):
    """R4.2: even a rule that calls everything an order never sees self messages or media."""
    msgs = [
        bubble(T0, "0", "shop báo giá 5 mẫu"),
        bubble(T0 + 1, "1001", "[image]", sender="Lan", kind="image"),
        bubble(T0 + 2, "1001", "[sticker]", kind="sticker"),
        bubble(T0 + 3, "1001", "[file]", kind="file"),
        bubble(T0 + 4, "1001", "[voice]", kind="voice"),
        bubble(T0 + 5, "0", "[image]", kind="image"),
    ]
    rule = AlwaysRule()
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), page=FakeChat(msgs, window=10), rule=rule)
    assert await app.run(max_ticks=3) == EXIT_OK
    assert rule.seen == [] and all_orders(db) == []
    assert all(m["extracted"] == 1 for m in all_messages(db))


async def test_r4_3_new_order_carries_a_copy_of_the_message_and_is_idempotent(db: Path):
    """R4.3: the order holds its own copy of the sheet fields; the same message re-checked (with a rule that would
    now say something else) never yields a second order nor changes the first."""
    msg = bubble(T0, "1001", "lấy 3 cái", sender="Lan")
    chat = FakeChat([msg], title="Nhóm A")
    first = App(db, sheet=InMemorySheet(header=list(HEADER)), page=chat)
    assert await first.run(max_ticks=3) == EXIT_OK
    [order] = all_orders(db)
    assert (order.conversation_key, order.message_key, order.number) == ("g1", msg["message_key"], 3)
    assert (order.sent_at_ms, order.conversation_title, order.sender, order.text) == (T0, "Nhóm A", "Lan", "lấy 3 cái")

    conn = open_database(db)
    assert MessagesRepo(conn).unmark_checked("g1", 0) == 1  # as `reprocess --conversation` does
    conn.close()
    rule = AlwaysRule(99)
    second = App(db, sheet=InMemorySheet(header=list(HEADER)), page=chat, rule=rule)
    assert await second.run(max_ticks=3) == EXIT_OK
    assert rule.seen == [msg["message_key"]]
    assert [(o.id, o.number) for o in all_orders(db)] == [(order.id, 3)]


async def test_r4_4_every_message_of_a_batch_is_marked_checked(db: Path):
    """R4.4: after a batch every message in it is checked, with or without an order (none is re-checked later)."""
    msgs = [bubble(T0 + i, "1001", t, sender="Lan") for i, t in enumerate(["chào", "lấy 1", "cảm ơn", "lấy 2"])]
    rule = FirstIntegerRule()
    first = App(db, sheet=InMemorySheet(header=list(HEADER)), page=FakeChat(msgs), rule=rule)
    assert await first.run(max_ticks=3) == EXIT_OK
    assert [m["extracted"] for m in all_messages(db)] == [1, 1, 1, 1]
    checked = first.of_type("MessagesChecked")
    assert sorted(k for e in checked for k in e.message_keys) == sorted(m["message_key"] for m in msgs)

    again = FirstIntegerRule()
    second = App(db, sheet=InMemorySheet(header=list(HEADER)), page=FakeChat(msgs), rule=again)
    assert await second.run(max_ticks=3) == EXIT_OK
    assert again.seen == []


async def test_r4_5_shipped_rule_is_the_llm_rule(db: Path):
    """R4.5: the rule wired into the application is the replaceable LLM rule."""
    rule = cli.build_rule(Settings())
    assert isinstance(rule, OrderRule)
    assert type(rule).__name__ == "LlmRule"


async def test_r4_5_the_rule_is_replaceable(db: Path):
    """R4.5: any object with `extract(message) -> int | None` is accepted as the rule."""

    class LengthRule:
        def extract(self, message: Message) -> int | None:
            return len(message.text or "")

    assert isinstance(LengthRule(), OrderRule)
    app = App(
        db, sheet=InMemorySheet(header=list(HEADER)), page=FakeChat([bubble(T0, "1001", "abcd")]), rule=LengthRule()
    )
    assert await app.run(max_ticks=3) == EXIT_OK
    assert [o.number for o in all_orders(db)] == [4]
