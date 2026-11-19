"""Orchestrator with the real colleagues and repositories on a temp SQLite file (design §11; task T14).

Only the external boundaries are faked: the browser (`FakeDriver`) and Google Sheets (`InMemorySheet`).
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fake_driver import NO_CHAT, FakeChat, FakeDriver, bubble
from fake_sheet import InMemorySheet
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import Message
from order_extractor.extractor.order_extractor import OrderExtractor
from order_extractor.orchestrator import EXIT_OK, Orchestrator
from order_extractor.recorder.order_recorder import OrderRecorder
from order_extractor.recorder.orders_repo import OrdersRepo
from order_extractor.recorder.sheet import HEADER
from order_extractor.status import StatusPrinter
from order_extractor.zalo.messages_repo import MessagesRepo
from order_extractor.zalo.zalo_web import ZaloWeb
from rules_for_tests import FirstIntegerRule, PoisonedRule

T_MSG = 1_795_059_480_000  # 2026-11-19 10:38 in Asia/Ho_Chi_Minh
MAX_TICKS = 300


class Run:
    """One process lifetime: a fresh Orchestrator and colleagues over the same database file."""

    def __init__(self, db_path: Path, sheet: InMemorySheet, page: Any, *, rule: FirstIntegerRule | None = None):
        self.settings = Settings(db_path=db_path, read_interval_seconds=0.01, sheets_timeout_seconds=2.0)
        self.conn = open_database(db_path)
        self.sheet = sheet
        self.rule = rule or FirstIntegerRule()
        self.out = io.StringIO()
        self.ticks = 0
        self.until: Callable[[], bool] = lambda: False
        self.orch = Orchestrator(
            self.settings, printer=StatusPrinter(self.out, tty=False), sleep=self._sleep, handle_signals=False
        )
        self.driver = FakeDriver(page)
        self.zalo = ZaloWeb(
            self.orch,
            self.driver,
            MessagesRepo(self.conn),
            self.settings,
            reader_script="readState()",
            since_ms=0,
            sleep=lambda s: None,
        )
        self.recorder = OrderRecorder.from_settings(self.orch, OrdersRepo(self.conn), sheet, self.settings)
        self.orch.attach(self.zalo, OrderExtractor(self.orch, self.rule), self.recorder)

    async def _sleep(self, seconds: float) -> None:
        self.ticks += 1
        await asyncio.sleep(0.005)  # let the recorder's background job resume
        if self.until() or self.ticks >= MAX_TICKS:
            self.orch.stop()

    async def run_until(self, until: Callable[[], bool]) -> int:
        self.until = until
        try:
            return await asyncio.wait_for(self.orch.run(), 10)
        finally:
            self.conn.close()


def orders(db_path: Path) -> OrdersRepo:
    return OrdersRepo(open_database(db_path))


async def test_customer_message_becomes_a_sheet_row_end_to_end(tmp_path: Path):
    sheet = InMemorySheet()  # empty: the header is created on the first job
    chat = FakeChat([bubble(T_MSG, "1001", "cho em 2 áo", sender="Lan")], title="Nhóm khách sỉ")
    run = Run(tmp_path / "app.db", sheet, chat)

    assert await run.run_until(lambda: bool(sheet.rows)) == EXIT_OK

    assert sheet.header == list(HEADER) and sheet.validation_applied
    assert len(sheet.rows) == 1
    order_id, time, conversation, sender, text, number, served = sheet.rows[0]
    assert (conversation, sender, text, number, served) == ("Nhóm khách sỉ", "Lan", "cho em 2 áo", 2, False)
    assert time == "2026-11-19 10:38"
    repo = orders(tmp_path / "app.db")
    assert repo.counts() == {"NEW": 0, "RECORDED": 1, "FAILED": 0}
    assert repo.get(int(order_id)).sheet_row == 2
    assert "found 1 · recorded 1" in run.out.getvalue()


async def test_order_stored_before_a_crash_is_recorded_after_restart(tmp_path: Path):
    db = tmp_path / "app.db"
    down = InMemorySheet(header=list(HEADER))
    down.fail("read_header", times=10_000)  # Sheets unreachable for the whole first run
    chat = FakeChat([bubble(T_MSG, "1001", "cho em 3 áo", sender="Lan")])
    first = Run(db, down, chat)
    await first.run_until(lambda: orders(db).counts()["NEW"] == 1)
    assert down.rows == []
    assert MessagesRepo(open_database(db)).unchecked_messages() == []  # checked only after the order was stored

    up = InMemorySheet(header=list(HEADER))
    rule = FirstIntegerRule()
    second = Run(db, up, NO_CHAT, rule=rule)
    assert await second.run_until(lambda: bool(up.rows)) == EXIT_OK

    assert [r[5] for r in up.rows] == [3]
    assert orders(db).counts()["RECORDED"] == 1
    assert rule.seen == []  # the message was already checked; only the recorder had work left


async def test_message_stored_but_not_checked_before_a_crash_becomes_an_order_after_restart(tmp_path: Path):
    db = tmp_path / "app.db"
    messages = MessagesRepo(open_database(db))
    messages.ensure_conversation("g1", start_ms=0, now_ms=T_MSG, title="Nhóm")
    stored = Message(conversation_key="g1", message_key=f"{T_MSG}_1001", sent_at_ms=T_MSG, sender_uid="1001")
    stored.text, stored.sender = "lấy 4 cái", "Lan"
    assert messages.insert_message(stored, extracted=False, observed_at_ms=T_MSG)

    sheet = InMemorySheet(header=list(HEADER))
    rule = FirstIntegerRule()
    run = Run(db, sheet, NO_CHAT, rule=rule)
    assert await run.run_until(lambda: bool(sheet.rows)) == EXIT_OK

    assert rule.seen == [f"{T_MSG}_1001"]
    assert [(r[3], r[5]) for r in sheet.rows] == [("Lan", 4)]
    assert MessagesRepo(open_database(db)).unchecked_messages() == []


async def test_poisoned_unchecked_message_does_not_crash_the_restart(tmp_path: Path):
    db = tmp_path / "app.db"
    messages = MessagesRepo(open_database(db))
    messages.ensure_conversation("g1", start_ms=0, now_ms=T_MSG, title="Nhóm")
    for offset, text in ((0, "poison 7"), (1, "lấy 5 cái")):
        stored = Message(
            conversation_key="g1", message_key=f"{T_MSG + offset}_1001", sent_at_ms=T_MSG + offset, sender_uid="1001"
        )
        stored.text, stored.sender = text, "Lan"
        assert messages.insert_message(stored, extracted=False, observed_at_ms=T_MSG)

    sheet = InMemorySheet(header=list(HEADER))
    run = Run(db, sheet, NO_CHAT, rule=PoisonedRule())
    assert await run.run_until(lambda: bool(sheet.rows)) == EXIT_OK

    assert [r[5] for r in sheet.rows] == [5]
    assert [m.message_key for m in MessagesRepo(open_database(db)).unchecked_messages()] == [f"{T_MSG}_1001"]
