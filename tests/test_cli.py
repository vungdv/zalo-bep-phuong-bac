"""CLI composition root: start, status, reprocess (design §10; task T15).

Every test runs in a temp working directory (no real `.env`, `data/` or browser); the external boundaries
(`build_driver`, `build_sheet`) are swapped for `FakeDriver` / `InMemorySheet`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fake_driver import NO_CHAT, FakeDriver
from fake_sheet import InMemorySheet
from order_extractor import cli
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import Message, NewOrder
from order_extractor.extractor.rules import StubRule
from order_extractor.lock import AppLock, lock_path_for
from order_extractor.recorder.orders_repo import OrdersRepo
from order_extractor.recorder.sheet import HEADER
from order_extractor.zalo.browser import BrowserError
from order_extractor.zalo.messages_repo import MessagesRepo

REPO_ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    sa = tmp_path / "secrets" / "sa.json"
    sa.parent.mkdir()
    sa.write_text("{}")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "app.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "data" / "logs"))
    monkeypatch.setenv("SHEET_ID", "sheet-under-test")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(sa))
    monkeypatch.setenv("READ_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("SHEETS_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(cli, "build_rule", lambda settings: StubRule())  # no LLM call in CLI tests
    monkeypatch.setattr(cli, "SELECTORS_PATH", REPO_ROOT / "selectors.json")
    yield tmp_path
    root = logging.getLogger("order_extractor")  # configure_logging bound handlers to the runner's streams
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = True
    root.setLevel(logging.NOTSET)


@pytest.fixture
def db_path(workspace: Path) -> Path:
    return workspace / "data" / "app.db"


def fake_boundaries(monkeypatch: pytest.MonkeyPatch, page: object, sheet: InMemorySheet) -> FakeDriver:
    driver = FakeDriver(page, max_restarts=1)
    monkeypatch.setattr(cli, "build_driver", lambda settings, selectors: driver)
    monkeypatch.setattr(cli, "build_sheet", lambda settings: sheet)
    return driver


def ms(text: str) -> int:
    return round(datetime.fromisoformat(text).timestamp() * 1000)


def test_help_lists_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("start", "status", "reprocess"):
        assert command in result.output


# --- start --------------------------------------------------------------------------------------------------


def test_start_exits_non_zero_when_the_browser_cannot_be_relaunched(monkeypatch, db_path):
    driver = fake_boundaries(monkeypatch, BrowserError("session gone"), InMemorySheet(header=list(HEADER)))
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 3
    assert driver.opened >= 1
    assert result.stdout.startswith("Zalo: ")  # status line on stdout …
    assert "{" not in result.stdout  # … and no JSON log line there
    log_file = db_path.parent / "logs" / "app.jsonl"
    assert '"browser_died"' in log_file.read_text()
    assert db_path.exists()
    with AppLock(lock_path_for(db_path)):  # released on exit
        pass


def test_start_exits_non_zero_when_the_recorder_is_blocked(monkeypatch):
    fake_boundaries(monkeypatch, NO_CHAT, InMemorySheet(header=["order_id", "when"]))
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 4
    assert "Open a chat to monitor" in result.stdout
    assert "exit code 4" in result.stderr


def test_start_passes_since_as_epoch_ms_in_tz_display(monkeypatch):
    fake_boundaries(monkeypatch, BrowserError("gone"), InMemorySheet(header=list(HEADER)))
    seen: list[int | None] = []
    original = cli.build_orchestrator

    def spy(settings, conn, selectors, since_ms):
        seen.append(since_ms)
        return original(settings, conn, selectors, since_ms)

    monkeypatch.setattr(cli, "build_orchestrator", spy)
    result = runner.invoke(cli.app, ["start", "--since", "2026-11-19 08:00"])
    assert result.exit_code == 3
    assert seen == [ms("2026-11-19T08:00:00+07:00")]


def test_start_refuses_while_another_start_holds_the_lock(monkeypatch, db_path):
    fake_boundaries(monkeypatch, NO_CHAT, InMemorySheet(header=list(HEADER)))
    with AppLock(lock_path_for(db_path)):
        result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 5
    assert "is locked" in result.stderr


def test_start_fails_fast_without_sheet_id(monkeypatch):
    monkeypatch.setenv("SHEET_ID", "")
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 2
    assert "SHEET_ID is not set" in result.stderr


def test_start_fails_fast_without_service_account_file(monkeypatch, workspace):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(workspace / "missing.json"))
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 2
    assert "GOOGLE_SERVICE_ACCOUNT_FILE" in result.stderr


def test_start_rejects_invalid_configuration(monkeypatch):
    monkeypatch.setenv("READ_INTERVAL_SECONDS", "-1")
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 2
    assert "READ_INTERVAL_SECONDS" in result.stderr


def test_start_rejects_missing_selectors(monkeypatch, workspace):
    monkeypatch.setattr(cli, "SELECTORS_PATH", workspace / "nope.json")
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 2
    assert "selectors" in result.stderr


def test_start_rejects_an_unparseable_since():
    result = runner.invoke(cli.app, ["start", "--since", "yesterday"])
    assert result.exit_code == 2
    assert "invalid time" in result.stderr


def test_default_boundaries_are_the_real_adapters():
    settings = Settings(sheet_id="x")
    driver = cli.build_driver(settings, {"scroller": "#chatView"})
    assert type(driver).__name__ == "BrowserDriver" and driver.scroller_selector == "#chatView"
    assert type(cli.build_sheet(settings)).__name__ == "GoogleSheet"  # lazy: no network until first use


# --- status -------------------------------------------------------------------------------------------------


def test_status_without_a_database(db_path):
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "No database yet" in result.stdout
    assert not db_path.exists()


def test_status_prints_counts_and_the_oldest_pending_order(db_path):
    repo = OrdersRepo(open_database(db_path))
    created = ms("2026-11-19T10:38:00+07:00")
    for n in range(4):
        repo.store(NewOrder(conversation_key="g1", message_key=f"{n}_1", number=n, sent_at_ms=n), created + n)
    repo.mark_recorded(2, 5)
    repo.mark_failed(1, attempts=5, error="503 unavailable")
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[1].split() == ["NEW", "2"]
    assert lines[2].split() == ["RECORDED", "1"]
    assert lines[3].split() == ["FAILED", "1"]
    assert lines[4] == "oldest NEW/FAILED: #1 FAILED, created 2026-11-19 10:38, attempts 5, last error: 503 unavailable"


def test_status_with_nothing_pending(db_path):
    open_database(db_path).close()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "oldest NEW/FAILED: none" in result.stdout


def test_status_needs_no_lock(db_path):
    open_database(db_path).close()
    with AppLock(lock_path_for(db_path)):
        assert runner.invoke(cli.app, ["status"]).exit_code == 0


# --- reprocess --order --------------------------------------------------------------------------------------


@pytest.fixture
def orders(db_path: Path) -> OrdersRepo:
    return OrdersRepo(open_database(db_path))


def test_reprocess_order_resets_a_failed_order(orders):
    order_id = orders.store(NewOrder(conversation_key="g1", message_key="1_1", number=2, sent_at_ms=1), 1)
    orders.mark_failed(order_id, attempts=5, error="timeout")
    result = runner.invoke(cli.app, ["reprocess", "--order", str(order_id)])
    assert result.exit_code == 0, result.output
    assert "reset to NEW" in result.stdout
    order = orders.get(order_id)
    assert (order.status, order.attempts, order.error) == ("NEW", 0, None)
    assert order.next_attempt_at_ms > 1  # due now


def test_reprocess_order_refuses_an_order_that_is_not_failed(orders):
    order_id = orders.store(NewOrder(conversation_key="g1", message_key="1_1", number=2, sent_at_ms=1), 1)
    result = runner.invoke(cli.app, ["reprocess", "--order", str(order_id)])
    assert result.exit_code == 1
    assert "is NEW; only FAILED" in result.stderr
    assert orders.get(order_id).status == "NEW"


def test_reprocess_order_unknown_id(orders):
    result = runner.invoke(cli.app, ["reprocess", "--order", "99"])
    assert result.exit_code == 1
    assert "order 99 not found" in result.stderr


def test_reprocess_refuses_while_start_holds_the_lock(orders, db_path):
    order_id = orders.store(NewOrder(conversation_key="g1", message_key="1_1", number=2, sent_at_ms=1), 1)
    orders.mark_failed(order_id, attempts=5, error="timeout")
    with AppLock(lock_path_for(db_path)):
        result = runner.invoke(cli.app, ["reprocess", "--order", str(order_id)])
        conv = runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19"])
    assert result.exit_code == 5 and conv.exit_code == 5
    assert "stop it first" in result.stderr
    assert orders.get(order_id).status == "FAILED"


@pytest.mark.parametrize(
    "args",
    [
        ["reprocess"],
        ["reprocess", "--order", "1", "--conversation", "g1", "--since", "2026-11-19"],
        ["reprocess", "--conversation", "g1"],
        ["reprocess", "--order", "1", "--since", "2026-11-19"],
        ["reprocess", "--conversation", "g1", "--since", "not a time"],
    ],
)
def test_reprocess_usage_errors(args):
    assert runner.invoke(cli.app, args).exit_code == 2


# --- reprocess --conversation -------------------------------------------------------------------------------


def test_reprocess_conversation_unmarks_only_live_messages_in_range(db_path):
    repo = MessagesRepo(open_database(db_path))
    since = ms("2026-11-19T08:00:00+07:00")
    repo.ensure_conversation("g1", start_ms=0, now_ms=since)
    repo.ensure_conversation("g2", start_ms=0, now_ms=since)

    def add(conv: str, sent_at: int, uid: str = "1001") -> str:
        m = Message(conversation_key=conv, message_key=f"{sent_at}_{uid}", sent_at_ms=sent_at, sender_uid=uid)
        m.text = "x"
        repo.insert_message(m, extracted=True, observed_at_ms=sent_at)
        return m.message_key

    add("g1", since - 1)  # before --since
    in_range = [add("g1", since), add("g1", since + 60_000)]
    tombstone = add("g1", since + 1, uid="2002")
    add("g2", since + 5)  # other conversation
    repo.conn.execute("UPDATE messages SET purged_at = 1, text = NULL WHERE message_key = ?", (tombstone,))

    result = runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19 08:00"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("2 message(s) of g1 sent since 2026-11-19 08:00")
    assert [m.message_key for m in repo.unchecked_messages()] == in_range


def test_reprocess_conversation_accepts_an_iso_time_with_offset(db_path):
    repo = MessagesRepo(open_database(db_path))
    repo.ensure_conversation("g1", start_ms=0, now_ms=0)
    m = Message(conversation_key="g1", message_key="1_1", sent_at_ms=ms("2026-11-19T01:00:00+00:00"), sender_uid="1")
    repo.insert_message(m, extracted=True, observed_at_ms=0)
    result = runner.invoke(cli.app, ["reprocess", "--conversation", "g1", "--since", "2026-11-19T01:00:00Z"])
    assert result.exit_code == 0
    assert len(repo.unchecked_messages()) == 1


def test_reprocess_conversation_unknown_key(db_path):
    open_database(db_path).close()
    result = runner.invoke(cli.app, ["reprocess", "--conversation", "g404", "--since", "2026-11-19"])
    assert result.exit_code == 1
    assert "conversation g404 not found" in result.stderr


def test_reprocess_logs_carry_a_tick_id_from_the_migration_on(db_path):
    assert not db_path.exists()  # open_database migrates a fresh file and logs it
    result = runner.invoke(cli.app, ["reprocess", "--order", "99"])
    assert result.exit_code == 1
    lines = [json.loads(line) for line in (db_path.parent / "logs" / "app.jsonl").read_text().splitlines()]
    assert lines and "migration_applied" in {line["event"] for line in lines}
    assert all(line["tick_id"] for line in lines)
    assert len({line["tick_id"] for line in lines}) == 1
