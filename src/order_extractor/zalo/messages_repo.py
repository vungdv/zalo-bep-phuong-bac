"""Repository for the tables owned by ZaloWeb: `conversations`, `messages`, `senders` (design §7, §7.1).

Every write is a single autocommitted statement (the connection is in autocommit mode), so a message is durable
before ZaloWeb announces it [R3.4]. All times are integer epoch milliseconds.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from order_extractor.db.connection import transaction
from order_extractor.events import Message


@dataclass(frozen=True)
class ConversationRow:
    key: str
    title: str | None
    is_group: bool
    first_seen: int
    start_ms: int  # start watermark [R2.11]


_MESSAGE_COLUMNS = """
    m.conversation_key, m.message_key, m.sent_at, m.sender_uid, m.sender, m.is_self, m.kind, m.text,
    c.title AS conversation_title
"""


def _to_message(row: sqlite3.Row) -> Message:
    return Message(
        conversation_key=row["conversation_key"],
        message_key=row["message_key"],
        sent_at_ms=row["sent_at"],
        sender_uid=row["sender_uid"],
        sender=row["sender"],
        is_self=bool(row["is_self"]),
        kind=row["kind"],
        text=row["text"],
        conversation_title=row["conversation_title"],
    )


class MessagesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- conversations ----------------------------------------------------------------------------------------

    def ensure_conversation(
        self, key: str, *, start_ms: int, now_ms: int, title: str | None = None, is_group: bool = False
    ) -> ConversationRow:
        """Insert the conversation on first sight with `start_ms` as its watermark; never change the watermark.

        The display title and group flag are refreshed on later calls.
        """
        self.conn.execute(
            """
            INSERT INTO conversations (key, title, is_group, first_seen, start_ms) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET title = COALESCE(excluded.title, title), is_group = excluded.is_group
            """,
            (key, title, int(is_group), now_ms, start_ms),
        )
        row = self.get_conversation(key)
        assert row is not None
        return row

    def get_conversation(self, key: str) -> ConversationRow | None:
        row = self.conn.execute(
            "SELECT key, title, is_group, first_seen, start_ms FROM conversations WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return ConversationRow(row["key"], row["title"], bool(row["is_group"]), row["first_seen"], row["start_ms"])

    # --- messages ---------------------------------------------------------------------------------------------

    def exists(self, conversation_key: str, message_key: str) -> bool:
        """True for live rows and tombstones alike: the row is the de-duplication record [R2.3]."""
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE conversation_key = ? AND message_key = ?", (conversation_key, message_key)
        ).fetchone()
        return row is not None

    def insert_message(self, message: Message, *, extracted: bool, observed_at_ms: int) -> bool:
        """Insert one message (committed on return); returns False if the key already exists (incl. tombstones)."""
        cur = self.conn.execute(
            """
            INSERT INTO messages (conversation_key, message_key, sender_uid, sender, is_self, kind, text,
                                  sent_at, observed_at, extracted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (conversation_key, message_key) DO NOTHING
            """,
            (
                message.conversation_key,
                message.message_key,
                message.sender_uid,
                message.sender,
                int(message.is_self),
                message.kind,
                message.text,
                message.sent_at_ms,
                observed_at_ms,
                int(extracted),
            ),
        )
        return cur.rowcount == 1

    def unchecked_messages(self, limit: int | None = None) -> list[Message]:
        """Live messages not yet checked by the rule, oldest first (by `sent_at`, then insertion order)."""
        sql = f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m JOIN conversations c ON c.key = m.conversation_key
            WHERE m.extracted = 0 AND m.purged_at IS NULL
            ORDER BY m.sent_at, m.id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [_to_message(r) for r in self.conn.execute(sql, params)]

    def mark_checked(self, conversation_key: str, message_keys: Iterable[str]) -> int:
        keys = list(message_keys)
        if not keys:
            return 0
        with transaction(self.conn):
            total = 0
            for key in keys:
                cur = self.conn.execute(
                    "UPDATE messages SET extracted = 1"
                    " WHERE conversation_key = ? AND message_key = ? AND extracted = 0",
                    (conversation_key, key),
                )
                total += cur.rowcount
        return total

    def unmark_checked(self, conversation_key: str, since_ms: int) -> int:
        """`reprocess --conversation`: live (not purged) messages sent at/after `since_ms` become unchecked [R7.3]."""
        cur = self.conn.execute(
            """
            UPDATE messages SET extracted = 0
            WHERE conversation_key = ? AND sent_at >= ? AND purged_at IS NULL AND extracted = 1
            """,
            (conversation_key, since_ms),
        )
        return cur.rowcount

    def purge_messages(self, cutoff_ms: int, now_ms: int) -> int:
        """Tombstone checked messages sent before `cutoff_ms`: drop text and sender, keep keys [R6.3]."""
        cur = self.conn.execute(
            """
            UPDATE messages SET text = NULL, sender = NULL, purged_at = ?
            WHERE purged_at IS NULL AND sent_at < ? AND extracted = 1
            """,
            (now_ms, cutoff_ms),
        )
        return cur.rowcount

    # --- senders ----------------------------------------------------------------------------------------------

    def upsert_senders(self, names: Mapping[str, str], now_ms: int) -> None:
        """Remember the last seen display name per uid [R2.7]."""
        if not names:
            return
        self.conn.executemany(
            """
            INSERT INTO senders (uid, name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (uid) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            WHERE senders.name IS NOT excluded.name
            """,
            [(uid, name, now_ms) for uid, name in names.items() if name],
        )

    def sender_name(self, uid: str) -> str | None:
        row = self.conn.execute("SELECT name FROM senders WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else row["name"]

    def purge_senders(self) -> int:
        """Drop names whose uid has no live message left [§7.1]."""
        cur = self.conn.execute(
            "DELETE FROM senders WHERE uid NOT IN (SELECT sender_uid FROM messages WHERE purged_at IS NULL)"
        )
        return cur.rowcount

    def purge(self, cutoff_ms: int, now_ms: int) -> tuple[int, int]:
        """Retention purge of this repository's tables: (messages tombstoned, senders deleted)."""
        return self.purge_messages(cutoff_ms, now_ms), self.purge_senders()
