"""R3 — Mediator architecture (acceptance)."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

from acceptance.harness import (
    REPO_ROOT,
    T0,
    App,
    FakeWorksheet,
    all_messages,
    all_orders,
    google_sheet,
)
from fake_driver import NO_CHAT, FakeChat, FakeDriver, bubble
from fake_sheet import InMemorySheet
from fakes import FakeMediator
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import Message, NewOrder
from order_extractor.extractor.order_extractor import OrderExtractor
from order_extractor.orchestrator import EXIT_ERROR, EXIT_OK
from order_extractor.recorder.order_recorder import OrderRecorder
from order_extractor.recorder.orders_repo import OrdersRepo
from order_extractor.recorder.sheet import HEADER
from order_extractor.zalo.messages_repo import MessagesRepo
from order_extractor.zalo.zalo_web import ZaloWeb
from rules_for_tests import FirstIntegerRule

PKG = REPO_ROOT / "src" / "order_extractor"
COLLEAGUES = {"zalo": PKG / "zalo", "extractor": PKG / "extractor", "recorder": PKG / "recorder"}


def imported_modules(folder: Path) -> set[str]:
    found: set[str] = set()
    for path in folder.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
    return found


def test_r3_1_r3_2_colleagues_never_import_each_other():
    """R3.1, R3.2: ZaloWeb, OrderExtractor and OrderRecorder are separate packages that do not reference each
    other (nor the Orchestrator); they share only the event contract."""
    for name, folder in COLLEAGUES.items():
        imports = imported_modules(folder)
        others = [o for o in COLLEAGUES if o != name]
        offending = {m for m in imports for o in others if m.startswith(f"order_extractor.{o}")}
        assert not offending, f"{name} imports {offending}"
        assert "order_extractor.orchestrator" not in imports


def test_r3_2_r3_4_each_colleague_touches_only_its_own_tables():
    """R3.2, R3.4: messages_repo owns conversations/messages/senders; orders_repo owns orders; no SQL crosses."""

    def tables(path: Path) -> set[str]:
        sql = path.read_text(encoding="utf-8")
        return set(re.findall(r"(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", sql, flags=re.IGNORECASE)) & {
            "conversations",
            "messages",
            "senders",
            "orders",
        }

    assert tables(PKG / "zalo" / "messages_repo.py") <= {"conversations", "messages", "senders"}
    assert tables(PKG / "recorder" / "orders_repo.py") == {"orders"}
    for path in [*PKG.joinpath("extractor").glob("*.py"), PKG / "orchestrator.py"]:
        assert not tables(path), f"{path.name} runs SQL"


async def test_r3_3_orchestrator_opens_zalo_web_then_drives_the_polling_loop(db: Path):
    """R3.3: on start the Orchestrator opens Zalo Web before the first poll and then polls every tick."""
    driver = FakeDriver(NO_CHAT)
    calls: list[str] = []
    real_open, real_eval = driver.ensure_open, driver.eval_json
    driver.ensure_open = lambda: (calls.append("open"), real_open())[1]  # type: ignore[method-assign]
    driver.eval_json = lambda js: (calls.append("eval"), real_eval(js))[1]  # type: ignore[method-assign]
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=driver)
    assert await app.run(max_ticks=5) == EXIT_OK
    assert calls[0] == "open"
    assert calls.count("eval") == 5


def crash_on_new_order(app: App, message_key: str) -> None:
    """Simulates the process dying while the order of `message_key` is being stored (after the rule ran).

    A rule that merely raises no longer kills the service (it is logged and the message left unchecked), so the
    crash is injected at the mediator instead."""
    route = app.orch.notify

    def crashing(event: Any) -> None:
        if isinstance(event, NewOrder) and event.message_key == message_key:
            raise RuntimeError("simulated crash")
        route(event)

    app.orch.notify = crashing  # type: ignore[method-assign]


async def test_r3_4_messages_are_marked_checked_only_after_their_orders_exist(db: Path):
    """R3.4: a crash in the middle of a batch leaves the whole batch unchecked (even the message whose order was
    already stored); on restart the rule re-runs and no order is duplicated."""
    msgs = [bubble(T0, "1001", "lấy 1", sender="Lan"), bubble(T0 + 1, "1002", "lấy 2", sender="Minh")]
    chat = FakeChat(msgs)
    first = App(db, sheet=InMemorySheet(header=list(HEADER)), page=chat)
    crash_on_new_order(first, msgs[1]["message_key"])
    assert await first.run(max_ticks=5) == EXIT_ERROR

    assert [o.number for o in all_orders(db)] == [1]  # the first order was committed …
    assert [m["extracted"] for m in all_messages(db)] == [0, 0]  # … but nothing is marked checked

    ws = FakeWorksheet(header=list(HEADER))
    rule = FirstIntegerRule()
    second = App(db, sheet=google_sheet(ws), page=chat, rule=rule)
    assert await second.run(until=lambda: len(ws.data) == 2, max_ticks=100) == EXIT_OK
    assert sorted(rule.seen) == sorted(m["message_key"] for m in msgs)
    assert [o.number for o in all_orders(db)] == [1, 2]
    assert sorted(r[1] for r in ws.data) == [1, 2]
    assert [m["extracted"] for m in all_messages(db)] == [1, 1]


async def test_r3_5_restart_recovers_unchecked_messages_and_unrecorded_orders(db: Path):
    """R3.5: same DB file, new process: ZaloWeb re-emits unchecked messages and OrderRecorder records the orders
    that were NEW (Sheets was down in the first run)."""
    down = FakeWorksheet(header=list(HEADER))
    down.fault("row_values", "error", times=10_000)
    msgs = [bubble(T0, "1001", "lấy 3", sender="Lan")]
    first = App(db, sheet=google_sheet(down), page=FakeChat(msgs))
    assert await first.run(max_ticks=8) == EXIT_OK
    assert [o.status for o in all_orders(db)] == ["NEW"] and down.data == []

    # a second message was stored but not checked when the process died
    conn = open_database(db)
    late = Message(conversation_key="g1", message_key=f"{T0 + 5}_1002", sent_at_ms=T0 + 5, sender_uid="1002")
    late.sender, late.text = "Minh", "lấy 4"
    assert MessagesRepo(conn).insert_message(late, extracted=False, observed_at_ms=T0 + 5)
    conn.close()

    up = FakeWorksheet(header=list(HEADER))
    rule = FirstIntegerRule()
    second = App(db, sheet=google_sheet(up), page=NO_CHAT, rule=rule)
    assert await second.run(until=lambda: len(up.data) == 2, max_ticks=100) == EXIT_OK
    assert rule.seen == [late.message_key]
    assert sorted((r[2], r[1]) for r in up.data) == [("Lan", 3), ("Minh", 4)]
    assert {o.status for o in all_orders(db)} == {"RECORDED"}


async def test_r3_5_crash_after_append_before_status_commit_is_not_appended_twice(db: Path):
    """R3.5, R5.5: the process stops while an append is in flight; the append lands after the process is gone,
    so SQLite still says NEW. The next start finds the id in column A and does not append it again."""
    ws = FakeWorksheet(header=list(HEADER))
    ws.fault("append_rows", "hang")
    chat = FakeChat([bubble(T0, "1001", "lấy 5", sender="Lan")])
    first = App(db, sheet=google_sheet(ws), page=chat, sheets_timeout_seconds=0.05)
    assert await first.run(until=lambda: ws.hanging.is_set(), max_ticks=200) == EXIT_OK
    ws.release()
    for _ in range(200):
        if ws.data:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # the abandoned job resumes against a closed database and is lost
    assert len(ws.data) == 1
    assert [o.status for o in all_orders(db)] == ["NEW"]

    second = App(db, sheet=google_sheet(ws), page=NO_CHAT)
    assert await second.run(until=lambda: all_orders(db)[0].status == "RECORDED", max_ticks=100) == EXIT_OK
    assert len(ws.data) == 1
    assert ws.count("append_rows") == 1


async def test_r3_6_each_colleague_runs_alone_with_a_fake_mediator(db: Path):
    """R3.6: each colleague is driven with a fake mediator that records what it emits."""
    conn = open_database(db)
    settings = Settings(db_path=db)

    zalo_m = FakeMediator()
    zalo = ZaloWeb(
        zalo_m,
        FakeDriver(FakeChat([bubble(T0, "1001", "lấy 2", sender="Lan")])),
        MessagesRepo(conn),
        settings,
        reader_script="x",
        since_ms=0,
        sleep=lambda s: None,
    )
    zalo.poll()
    assert zalo_m.types == ["ZaloReady", "NewMessages"]
    message = zalo_m.events[1].messages[0]

    ext_m = FakeMediator()
    OrderExtractor(ext_m, FirstIntegerRule()).handle([message])
    assert ext_m.types == ["NewOrder", "MessagesChecked"]

    rec_m = FakeMediator()
    recorder = OrderRecorder.from_settings(rec_m, OrdersRepo(conn), InMemorySheet(header=list(HEADER)), settings)
    assert isinstance(ext_m.events[0], NewOrder)
    recorder.store(ext_m.events[0])
    for _ in range(3):
        recorder.kick()
        await recorder.wait_idle(2)
    assert rec_m.types == ["OrderRecorded"]
    conn.close()
