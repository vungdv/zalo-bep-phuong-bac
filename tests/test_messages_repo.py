"""messages_repo against a migrated in-memory SQLite (task T8)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from order_extractor.db.connection import open_database
from order_extractor.events import Message
from order_extractor.zalo.messages_repo import ConversationRow, MessagesRepo

CONV = "g1"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_database(":memory:")
    yield c
    c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> MessagesRepo:
    r = MessagesRepo(conn)
    r.ensure_conversation(CONV, start_ms=1000, now_ms=1000, title="Nhóm", is_group=True)
    return r


def msg(ts: int, uid: str = "7", text: str | None = "hi", conv: str = CONV, **kw: object) -> Message:
    return Message(
        conversation_key=conv, message_key=f"{ts}_{uid}", sent_at_ms=ts, sender_uid=uid, sender="Lan", text=text, **kw
    )


def row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM messages WHERE message_key = ?", (key,)).fetchone()


def test_ensure_conversation_sets_watermark_once(repo: MessagesRepo) -> None:
    again = repo.ensure_conversation(CONV, start_ms=99999, now_ms=99999, title="Nhóm mới", is_group=True)
    assert again == ConversationRow(CONV, "Nhóm mới", True, 1000, 1000)


def test_ensure_conversation_keeps_title_when_none_given(repo: MessagesRepo) -> None:
    assert repo.ensure_conversation(CONV, start_ms=5, now_ms=5).title == "Nhóm"


def test_get_conversation_unknown(repo: MessagesRepo) -> None:
    assert repo.get_conversation("g404") is None


def test_insert_is_idempotent(repo: MessagesRepo, conn: sqlite3.Connection) -> None:
    assert repo.insert_message(msg(2000), extracted=False, observed_at_ms=3000)
    assert not repo.insert_message(msg(2000, text="changed"), extracted=True, observed_at_ms=4000)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    r = row(conn, "2000_7")
    assert (r["text"], r["extracted"], r["observed_at"], r["sent_at"]) == ("hi", 0, 3000, 2000)
    assert repo.exists(CONV, "2000_7")
    assert not repo.exists(CONV, "2001_7")
    assert not repo.exists("g2", "2000_7")


def test_same_key_in_another_conversation_is_distinct(repo: MessagesRepo) -> None:
    repo.ensure_conversation("g2", start_ms=0, now_ms=0)
    assert repo.insert_message(msg(2000), extracted=False, observed_at_ms=0)
    assert repo.insert_message(msg(2000, conv="g2"), extracted=False, observed_at_ms=0)


def test_unchecked_messages_oldest_first_with_title(repo: MessagesRepo) -> None:
    for ts, checked in [(3000, False), (2000, False), (2500, True)]:
        repo.insert_message(msg(ts), extracted=checked, observed_at_ms=0)
    got = repo.unchecked_messages()
    assert [m.message_key for m in got] == ["2000_7", "3000_7"]
    assert got[0].conversation_title == "Nhóm"
    assert got[0].sender == "Lan" and got[0].kind == "text" and got[0].is_self is False
    assert [m.message_key for m in repo.unchecked_messages(limit=1)] == ["2000_7"]


def test_mark_and_unmark_checked(repo: MessagesRepo, conn: sqlite3.Connection) -> None:
    for ts in (2000, 3000, 4000):
        repo.insert_message(msg(ts), extracted=False, observed_at_ms=0)
    assert repo.mark_checked(CONV, ["2000_7", "3000_7", "missing"]) == 2
    assert repo.mark_checked(CONV, []) == 0
    assert [m.message_key for m in repo.unchecked_messages()] == ["4000_7"]
    assert repo.unmark_checked(CONV, since_ms=3000) == 1
    assert [m.message_key for m in repo.unchecked_messages()] == ["3000_7", "4000_7"]


def test_purge_leaves_tombstones_and_skips_unchecked(repo: MessagesRepo, conn: sqlite3.Connection) -> None:
    repo.insert_message(msg(2000), extracted=True, observed_at_ms=0)  # old, checked → purged
    repo.insert_message(msg(2100), extracted=False, observed_at_ms=0)  # old, unchecked → kept
    repo.insert_message(msg(9000), extracted=True, observed_at_ms=0)  # recent → kept
    assert repo.purge_messages(cutoff_ms=5000, now_ms=10000) == 1
    tomb = row(conn, "2000_7")
    assert (tomb["text"], tomb["sender"], tomb["purged_at"]) == (None, None, 10000)
    assert (tomb["sender_uid"], tomb["sent_at"], tomb["extracted"]) == ("7", 2000, 1)
    assert row(conn, "2100_7")["text"] == "hi"
    assert row(conn, "9000_7")["text"] == "hi"
    assert repo.purge_messages(cutoff_ms=5000, now_ms=11000) == 0  # already tombstoned


def test_reinserting_a_purged_key_is_a_noop(repo: MessagesRepo, conn: sqlite3.Connection) -> None:
    repo.insert_message(msg(2000), extracted=True, observed_at_ms=0)
    repo.purge_messages(cutoff_ms=5000, now_ms=10000)
    assert repo.exists(CONV, "2000_7")
    assert not repo.insert_message(msg(2000), extracted=False, observed_at_ms=0)
    assert row(conn, "2000_7")["text"] is None


def test_unmark_skips_tombstones(repo: MessagesRepo) -> None:
    repo.insert_message(msg(2000), extracted=True, observed_at_ms=0)
    repo.purge_messages(cutoff_ms=5000, now_ms=10000)
    assert repo.unmark_checked(CONV, since_ms=0) == 0
    assert repo.unchecked_messages() == []


def test_senders_upsert_and_lookup(repo: MessagesRepo, conn: sqlite3.Connection) -> None:
    repo.upsert_senders({"7": "Lan", "8": "Minh"}, now_ms=1)
    repo.upsert_senders({"7": "Lan Anh"}, now_ms=2)
    repo.upsert_senders({}, now_ms=3)
    assert repo.sender_name("7") == "Lan Anh"
    assert repo.sender_name("8") == "Minh"
    assert repo.sender_name("9") is None
    assert conn.execute("SELECT updated_at FROM senders WHERE uid = '8'").fetchone()[0] == 1


def test_sender_lookup_after_purge(repo: MessagesRepo) -> None:
    repo.upsert_senders({"7": "Lan", "8": "Minh"}, now_ms=1)
    repo.insert_message(msg(2000, uid="7"), extracted=True, observed_at_ms=0)  # will be purged
    repo.insert_message(msg(2001, uid="8"), extracted=True, observed_at_ms=0)  # will be purged
    repo.insert_message(msg(9000, uid="8"), extracted=True, observed_at_ms=0)  # live
    assert repo.purge(cutoff_ms=5000, now_ms=10000) == (2, 1)
    assert repo.sender_name("7") is None  # no live message left
    assert repo.sender_name("8") == "Minh"
