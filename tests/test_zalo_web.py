"""ZaloWeb colleague with FakeDriver + FakeMediator + a migrated in-memory DB (tasks T9, T10)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fake_driver import LOGIN, NO_CHAT, FakeDriver, bubble, reading
from fakes import FakeMediator
from order_extractor.config import Settings
from order_extractor.db.connection import open_database
from order_extractor.events import (
    BrowserDied,
    LoginRequired,
    Message,
    NewMessages,
    NoChatOpen,
    SelectorDrift,
    ZaloReady,
)
from order_extractor.zalo.browser import BrowserError, BrowserTimeout
from order_extractor.zalo.messages_repo import MessagesRepo
from order_extractor.zalo.zalo_web import ZaloWeb

NOW = 10_000_000


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_database(":memory:")
    yield c
    c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> MessagesRepo:
    return MessagesRepo(conn)


class Harness:
    def __init__(self, repo: MessagesRepo, page: Any = None, *, since_ms: int | None = 0, **settings: Any) -> None:
        self.settings = Settings(**settings)
        self.mediator = FakeMediator()
        self.driver = FakeDriver(page, max_restarts=self.settings.browser_max_restarts)
        self.repo = repo
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.zalo = self.make(since_ms=since_ms)

    def make(self, *, since_ms: int | None = 0) -> ZaloWeb:
        return ZaloWeb(
            self.mediator,
            self.driver,
            self.repo,
            self.settings,
            reader_script="() => 1",
            since_ms=since_ms,
            clock=self.clock,
            sleep=self.sleeps.append,
        )

    def new_messages(self) -> list[list[str]]:
        return [[m.message_key for m in e.messages] for e in self.mediator.of_type(NewMessages)]

    def stored(self, conv: str = "g1") -> dict[str, sqlite3.Row]:
        rows = self.repo.conn.execute(
            "SELECT * FROM messages WHERE conversation_key = ? ORDER BY sent_at", (conv,)
        ).fetchall()
        return {r["message_key"]: r for r in rows}


# --- T9: poll + ingest ----------------------------------------------------------------------------------------


def test_same_page_polled_three_times_is_stored_once_and_emitted_once(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000), bubble(2000)]))
    for _ in range(3):
        h.zalo.poll()
    assert len(h.stored()) == 2
    assert h.mediator.types == ["ZaloReady", "NewMessages"]
    assert h.new_messages() == [["1000_1001", "2000_1001"]]


def test_new_bubble_gives_one_insert_and_one_event(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000)]))
    h.zalo.poll()
    h.mediator.clear()
    h.driver.page = reading([bubble(1000), bubble(2000, "1002", "2 áo")])
    h.zalo.poll()
    assert h.mediator.types == ["NewMessages"]
    assert h.new_messages() == [["2000_1002"]]
    assert len(h.stored()) == 2


def test_messages_are_ingested_chronologically(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(3000), bubble(1000), bubble(2000)]))
    h.zalo.poll()
    assert h.new_messages() == [["1000_1001", "2000_1001", "3000_1001"]]
    ids = [r["id"] for r in h.stored().values()]
    assert ids == sorted(ids)


@pytest.mark.parametrize(("page", "event"), [(LOGIN, LoginRequired), (NO_CHAT, NoChatOpen)])
def test_login_and_no_chat_emit_once_and_store_nothing(repo: MessagesRepo, page: dict[str, Any], event: type) -> None:
    h = Harness(repo, page)
    h.zalo.poll()
    h.zalo.poll()
    assert [type(e) for e in h.mediator.events] == [event]
    assert h.stored() == {}


def test_state_transitions_are_announced_each_time(repo: MessagesRepo) -> None:
    h = Harness(repo)
    h.driver.queue = [LOGIN, NO_CHAT, reading([bubble(1000)]), NO_CHAT, reading([bubble(1000)]), LOGIN]
    for _ in range(6):
        h.zalo.poll()
    assert h.mediator.types == [
        "LoginRequired",
        "NoChatOpen",
        "ZaloReady",
        "NewMessages",
        "NoChatOpen",
        "ZaloReady",
        "LoginRequired",
    ]


def test_zalo_ready_carries_the_conversation(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000)], key="g42", title="Sỉ"))
    h.zalo.poll()
    ready = h.mediator.of_type(ZaloReady)[0]
    assert (ready.conversation.key, ready.conversation.title, ready.conversation.is_group) == ("g42", "Sỉ", True)


def test_pre_watermark_bubbles_stored_as_checked_and_not_emitted(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(NOW - 2000), bubble(NOW - 1000)]), since_ms=None)
    h.zalo.poll()
    assert h.mediator.types == ["ZaloReady"]
    assert [r["extracted"] for r in h.stored().values()] == [1, 1]
    assert repo.get_conversation("g1").start_ms == NOW  # type: ignore[union-attr]
    h.driver.page = reading([bubble(NOW - 1000), bubble(NOW + 500)])
    h.zalo.poll()
    assert h.new_messages() == [[f"{NOW + 500}_1001"]]
    assert h.stored()[f"{NOW + 500}_1001"]["extracted"] == 0


def test_since_sets_the_watermark_on_first_sight(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000), bubble(5000)]), since_ms=3000)
    h.zalo.poll()
    assert repo.get_conversation("g1").start_ms == 3000  # type: ignore[union-attr]
    assert h.new_messages() == [["5000_1001"]]


def test_since_does_not_change_a_known_conversation(repo: MessagesRepo) -> None:
    repo.ensure_conversation("g1", start_ms=7000, now_ms=7000)
    h = Harness(repo, reading([bubble(5000), bubble(8000)]), since_ms=0)
    h.zalo.poll()
    assert repo.get_conversation("g1").start_ms == 7000  # type: ignore[union-attr]
    assert h.new_messages() == [["8000_1001"]]


def test_bubble_without_name_header_gets_the_stored_name(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000, sender="Lan")]))
    h.zalo.poll()
    h.driver.page = reading([bubble(2000), bubble(3000, "1009")])  # header scrolled away; 1009 never named
    h.zalo.poll()
    rows = h.stored()
    assert rows["1000_1001"]["sender"] == "Lan"
    assert rows["2000_1001"]["sender"] == "Lan"
    assert rows["3000_1009"]["sender"] == "1009"  # unknown → uid
    emitted = h.mediator.of_type(NewMessages)[-1].messages
    assert [m.sender for m in emitted] == ["Lan", "1009"]


def test_name_learned_later_applies_to_later_rows_only(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000, "1009")]))
    h.zalo.poll()
    h.driver.page = reading([bubble(1000, "1009"), bubble(2000, "1009", sender="Hoa"), bubble(3000, "1009")])
    h.zalo.poll()
    rows = h.stored()
    assert [rows[k]["sender"] for k in ("1000_1009", "2000_1009", "3000_1009")] == ["1009", "Hoa", "Hoa"]


def test_emitted_messages_carry_conversation_fields(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000, "0", "ok"), bubble(2000, kind="image", text="[image]")], title="Sỉ"))
    h.zalo.poll()
    first, second = h.mediator.of_type(NewMessages)[0].messages
    assert (first.conversation_key, first.conversation_title, first.is_self) == ("g1", "Sỉ", True)
    assert (second.kind, second.text) == ("image", "[image]")


def test_mark_checked_updates_the_messages(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000), bubble(2000)]))
    h.zalo.poll()
    assert h.zalo.mark_checked("g1", ["1000_1001"]) == 1
    assert [m.message_key for m in repo.unchecked_messages()] == ["2000_1001"]


def test_recover_re_emits_only_unchecked_per_conversation_in_chunks(repo: MessagesRepo) -> None:
    for key in ("g1", "g2"):
        repo.ensure_conversation(key, start_ms=0, now_ms=0)
    for i in range(230):
        m = Message(conversation_key="g1", message_key=f"{i}_1", sent_at_ms=i, sender_uid="1", text="x")
        repo.insert_message(m, extracted=(i % 10 == 0), observed_at_ms=0)  # 23 checked
    repo.insert_message(
        Message(conversation_key="g2", message_key="5_1", sent_at_ms=5, sender_uid="1"),
        extracted=False,
        observed_at_ms=0,
    )
    h = Harness(repo)
    h.zalo.recover()
    events = h.mediator.of_type(NewMessages)
    assert [len(e.messages) for e in events] == [100, 100, 7, 1]
    assert {m.conversation_key for m in events[0].messages} == {"g1"}
    assert events[-1].messages[0].conversation_key == "g2"
    keys = [m.message_key for e in events[:3] for m in e.messages]
    assert "0_1" not in keys and "10_1" not in keys
    assert keys == [f"{i}_1" for i in range(230) if i % 10]


def test_recover_with_nothing_unchecked_emits_nothing(repo: MessagesRepo) -> None:
    h = Harness(repo)
    h.zalo.recover()
    assert h.mediator.events == []


# --- browser failures [R1.5] ----------------------------------------------------------------------------------


def test_open_ensures_the_session(repo: MessagesRepo) -> None:
    h = Harness(repo)
    h.zalo.open()
    assert h.driver.opened == 1
    assert h.mediator.events == []


def test_open_failure_emits_browser_died(repo: MessagesRepo) -> None:
    h = Harness(repo)
    h.driver.open_error = BrowserError("no playwright-cli")
    h.zalo.open()
    died = h.mediator.of_type(BrowserDied)[0]
    assert (died.attempt, died.max_attempts, died.will_retry) == (1, 3, True)


def test_eval_failure_relaunches_and_emits_browser_died(repo: MessagesRepo) -> None:
    h = Harness(repo, BrowserTimeout("eval timed out"))
    h.zalo.poll()
    died = h.mediator.of_type(BrowserDied)[0]
    assert (died.attempt, died.will_retry, died.error) == (1, True, "eval timed out")
    assert h.driver.opened == 1


def test_consecutive_failures_exhaust_and_success_resets(repo: MessagesRepo) -> None:
    h = Harness(repo, browser_max_restarts=2)
    err = BrowserError("gone")
    h.driver.queue = [err, err, reading([bubble(1000)]), err, err, err]
    for _ in range(6):
        h.zalo.poll()
    died = h.mediator.of_type(BrowserDied)
    assert [(d.attempt, d.will_retry) for d in died] == [(1, True), (2, True), (1, True), (2, True), (3, False)]


def test_after_browser_death_the_conversation_is_announced_again(repo: MessagesRepo) -> None:
    h = Harness(repo)
    h.driver.queue = [reading([bubble(1000)]), BrowserError("x"), reading([bubble(1000)])]
    for _ in range(3):
        h.zalo.poll()
    assert h.mediator.types == ["ZaloReady", "NewMessages", "BrowserDied", "ZaloReady"]


# --- selector drift [R2.9] ------------------------------------------------------------------------------------


def test_reader_exception_reports_drift_with_snapshot_rate_limited(repo: MessagesRepo) -> None:
    h = Harness(repo, {"state": "ERROR", "error": "TypeError: x is null"})
    h.zalo.poll()
    h.clock.now += 30_000
    h.zalo.poll()  # within SELECTOR_ALERT_SECONDS → suppressed
    h.clock.now += 31_000
    h.zalo.poll()
    drifts = h.mediator.of_type(SelectorDrift)
    assert len(drifts) == 2
    assert "TypeError" in drifts[0].reason
    assert drifts[0].snapshot_path == str(h.driver.snapshots[0])


def test_snapshot_failure_still_reports_drift(repo: MessagesRepo) -> None:
    h = Harness(repo, {"state": "ERROR", "error": "boom"})

    def fail(path: Path | None = None) -> Path:
        raise BrowserError("snapshot failed")

    h.driver.snapshot = fail  # type: ignore[method-assign]
    h.zalo.poll()
    assert h.mediator.of_type(SelectorDrift)[0].snapshot_path is None


def test_malformed_reader_output_reports_drift(repo: MessagesRepo) -> None:
    h = Harness(repo, {"state": "SOMETHING"})
    h.zalo.poll()
    assert h.mediator.types == ["SelectorDrift"]


def test_inconsistent_conversation_keys_report_drift_and_store_nothing(repo: MessagesRepo) -> None:
    h = Harness(repo, reading([bubble(1000)], drift="bubbles disagree on the conversation key: g1, g2"))
    h.zalo.poll()
    assert h.mediator.types == ["SelectorDrift"]
    assert h.stored() == {}


def test_bubbles_that_never_parse_report_drift_after_alert_window(repo: MessagesRepo) -> None:
    h = Harness(repo, {"state": "NO_CHAT", "rendered_bubbles": 12})
    h.zalo.poll()
    h.clock.now += 59_000
    h.zalo.poll()
    assert h.mediator.types == ["NoChatOpen"]
    h.clock.now += 1_000
    h.zalo.poll()
    assert h.mediator.types == ["NoChatOpen", "SelectorDrift"]
    assert "12 bubble(s)" in h.mediator.of_type(SelectorDrift)[0].reason


def test_parsing_again_resets_the_empty_timer(repo: MessagesRepo) -> None:
    h = Harness(repo)
    h.driver.queue = [{"state": "NO_CHAT", "rendered_bubbles": 3}, reading([bubble(1000)])]
    h.zalo.poll()
    h.clock.now += 50_000
    h.zalo.poll()
    h.clock.now += 50_000
    h.driver.page = {"state": "NO_CHAT", "rendered_bubbles": 3}
    h.zalo.poll()
    assert "SelectorDrift" not in h.mediator.types
