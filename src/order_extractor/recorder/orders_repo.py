"""Orders repository (design §6, §7, §7.1): the only code that reads or writes the `orders` table.

Owned by OrderRecorder. All times are integer epoch milliseconds (UTC). The connection is autocommit
(`db.connection.connect`), so every method below commits on return; methods that touch several rows wrap them in
one transaction.

Order life cycle (design §7): `store` → NEW (due at once) → `mark_recorded` → RECORDED, or `schedule_retry` (NEW,
backed off) … `mark_failed` → FAILED → `mark_recorded` (column-A reconciliation) or `reset_failed` → NEW.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from order_extractor.db.connection import transaction
from order_extractor.events import NewOrder

OrderStatus = Literal["NEW", "RECORDED", "FAILED"]
STATUSES: tuple[OrderStatus, ...] = ("NEW", "RECORDED", "FAILED")
DAY_MS = 24 * 60 * 60 * 1000
_ERROR_MAX = 500  # stored error text is truncated; it is diagnostic only

_COLUMNS = (
    "id, conversation_key, message_key, number, sent_at, conversation_title, sender, text, status, attempts, "
    "created_at, next_attempt_at, sheet_row, error"
)


@dataclass(frozen=True)
class Order:
    """One row of `orders`. `conversation_title`, `sender` and `text` are `None` once purged."""

    id: int
    conversation_key: str
    message_key: str
    number: int
    sent_at_ms: int
    conversation_title: str | None
    sender: str | None
    text: str | None
    status: OrderStatus
    attempts: int
    created_at_ms: int
    next_attempt_at_ms: int
    sheet_row: int | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Order:
        return cls(
            id=row["id"],
            conversation_key=row["conversation_key"],
            message_key=row["message_key"],
            number=row["number"],
            sent_at_ms=row["sent_at"],
            conversation_title=row["conversation_title"],
            sender=row["sender"],
            text=row["text"],
            status=row["status"],
            attempts=row["attempts"],
            created_at_ms=row["created_at"],
            next_attempt_at_ms=row["next_attempt_at"],
            sheet_row=row["sheet_row"],
            error=row["error"],
        )


def _error_text(error: object) -> str:
    return str(error)[:_ERROR_MAX]


class OrdersRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- writes ---------------------------------------------------------------------------------------------

    def store(self, order: NewOrder, now_ms: int) -> int | None:
        """Insert `order` as NEW, due at once (`next_attempt_at = created_at`) [R5.1].

        Idempotent on `(conversation_key, message_key)` [R4.3]: returns the new id, or `None` for a duplicate.
        """
        cur = self.conn.execute(
            "INSERT INTO orders (conversation_key, message_key, number, sent_at, conversation_title, sender, text,"
            " status, attempts, created_at, next_attempt_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'NEW', 0, ?, ?)"
            " ON CONFLICT (conversation_key, message_key) DO NOTHING",
            (
                order.conversation_key,
                order.message_key,
                order.number,
                order.sent_at_ms,
                order.conversation_title,
                order.sender,
                order.text,
                now_ms,
                now_ms,
            ),
        )
        return cur.lastrowid if cur.rowcount == 1 else None

    def mark_recorded(self, order_id: int, sheet_row: int | None = None) -> None:
        """NEW/FAILED → RECORDED. `sheet_row` is `None` when reconciled from column A (row unknown)."""
        self.conn.execute(
            "UPDATE orders SET status = 'RECORDED', sheet_row = ?, error = NULL WHERE id = ? AND status != 'RECORDED'",
            (sheet_row, order_id),
        )

    def schedule_retry(self, order_id: int, attempts: int, next_attempt_at_ms: int, error: object) -> None:
        """Keep the order NEW, record the failed attempt and back it off until `next_attempt_at_ms` [R5.6]."""
        self.conn.execute(
            "UPDATE orders SET attempts = ?, next_attempt_at = ?, error = ? WHERE id = ? AND status = 'NEW'",
            (attempts, next_attempt_at_ms, _error_text(error), order_id),
        )

    def mark_failed(self, order_id: int, attempts: int, error: object) -> None:
        """NEW → FAILED after the last attempt; never retried automatically (see `reset_failed`)."""
        self.conn.execute(
            "UPDATE orders SET status = 'FAILED', attempts = ?, error = ? WHERE id = ? AND status = 'NEW'",
            (attempts, _error_text(error), order_id),
        )

    def reset_failed(self, order_id: int, now_ms: int) -> bool:
        """FAILED → NEW with `attempts = 0`, due at `now_ms` [R7.2]. Returns `False` if the order is not FAILED."""
        cur = self.conn.execute(
            "UPDATE orders SET status = 'NEW', attempts = 0, next_attempt_at = ?, error = NULL"
            " WHERE id = ? AND status = 'FAILED'",
            (now_ms, order_id),
        )
        return cur.rowcount == 1

    def purge(self, now_ms: int, retention_days: int) -> int:
        """Drop the copied content of RECORDED orders sent before the retention window [R6.3].

        NEW/FAILED orders keep their copy because the recorder still needs it. Returns the number of rows purged.
        """
        cutoff = now_ms - retention_days * DAY_MS
        cur = self.conn.execute(
            "UPDATE orders SET text = NULL, sender = NULL, conversation_title = NULL"
            " WHERE status = 'RECORDED' AND sent_at < ?"
            " AND (text IS NOT NULL OR sender IS NOT NULL OR conversation_title IS NOT NULL)",
            (cutoff,),
        )
        return cur.rowcount

    def mark_recorded_many(self, order_ids: Iterable[int]) -> None:
        """`mark_recorded(id, None)` for each id, in one transaction (column-A reconciliation)."""
        with transaction(self.conn):
            for order_id in order_ids:
                self.mark_recorded(order_id, None)

    # --- reads ----------------------------------------------------------------------------------------------

    def get(self, order_id: int) -> Order | None:
        row = self.conn.execute(f"SELECT {_COLUMNS} FROM orders WHERE id = ?", (order_id,)).fetchone()
        return Order.from_row(row) if row else None

    def orders_for_ids(self, order_ids: Sequence[int]) -> list[Order]:
        """The orders with these ids, by id (unknown ids are skipped)."""
        if not order_ids:
            return []
        marks = ",".join("?" * len(order_ids))
        rows = self.conn.execute(f"SELECT {_COLUMNS} FROM orders WHERE id IN ({marks}) ORDER BY id", tuple(order_ids))
        return [Order.from_row(r) for r in rows]

    def due(self, now_ms: int, limit: int) -> list[Order]:
        """NEW orders with `next_attempt_at <= now_ms`, oldest due first, at most `limit` [R5.1, R5.6]."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM orders WHERE status = 'NEW' AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at, id LIMIT ?",
            (now_ms, limit),
        )
        return [Order.from_row(r) for r in rows]

    def unrecorded(self) -> list[Order]:
        """Every NEW or FAILED order, by id: the candidates for column-A reconciliation [R5.5]."""
        rows = self.conn.execute(f"SELECT {_COLUMNS} FROM orders WHERE status IN ('NEW', 'FAILED') ORDER BY id")
        return [Order.from_row(r) for r in rows]

    def counts(self) -> dict[OrderStatus, int]:
        """Number of orders per status; every status is present (0 if none) [R7.1]."""
        result: dict[OrderStatus, int] = dict.fromkeys(STATUSES, 0)
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status"):
            result[row["status"]] = row["n"]
        return result

    def oldest_pending(self) -> Order | None:
        """The oldest NEW or FAILED order by `created_at` [R7.1]."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM orders WHERE status IN ('NEW', 'FAILED') ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        return Order.from_row(row) if row else None
