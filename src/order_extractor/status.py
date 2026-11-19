"""Console status line (design §8, R6.4): the only thing the application writes to stdout.

`Status` holds what the line shows; `StatusPrinter` draws it. On a terminal the line is redrawn in place
(`\\r` + clear to end of line); otherwise (a pipe or a file) a plain line is written only when its text changes,
so a redirected stdout does not fill up every tick. Logs never go to stdout (telemetry.py).

Counts: `read` and `found` are this run's (`NewMessages` / `NewOrder` events); `recorded`, `failed` and
`pending` are totals from the orders table, refreshed every tick.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO

LOGIN_PROMPT = "Waiting for login: scan the QR code in the browser"  # [R1.2]
NO_CHAT_PROMPT = "Open the chat group to monitor"  # [R1.3]
_CLEAR_LINE = "\r\x1b[K"


@dataclass
class Status:
    login: str = "starting"
    conversation: str | None = None
    messages_read: int = 0
    orders_found: int = 0
    orders_recorded: int = 0
    orders_failed: int = 0
    orders_pending: int = 0
    sheets: str = "idle"
    drift: bool = False

    def set_counts(self, counts: Mapping[str, int]) -> None:
        """Take the per-status totals of the orders table."""
        self.orders_recorded = counts.get("RECORDED", 0)
        self.orders_failed = counts.get("FAILED", 0)
        self.orders_pending = counts.get("NEW", 0)

    def render(self) -> str:
        zalo = self.login if self.conversation is None else f"{self.login} · {self.conversation}"
        parts = [
            f"Zalo: {zalo}",
            f"read {self.messages_read} · found {self.orders_found} · recorded {self.orders_recorded}"
            f" · failed {self.orders_failed} · pending {self.orders_pending}",
            f"sheets: {self.sheets}",
        ]
        if self.drift:
            parts.append("selectors may be outdated")
        return " | ".join(parts)


class StatusPrinter:
    """Writes `Status.render()` to `stream` (stdout by default)."""

    def __init__(self, stream: TextIO | None = None, *, tty: bool | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if tty is None:
            isatty = getattr(self.stream, "isatty", None)
            tty = bool(isatty and isatty())
        self.tty = tty
        self._last: str | None = None

    def show(self, status: Status) -> None:
        line = status.render()
        if self.tty:
            self.stream.write(_CLEAR_LINE + line)
        elif line != self._last:
            self.stream.write(line + "\n")
        else:
            return
        self._last = line
        self.stream.flush()

    def close(self) -> None:
        """End the redrawn line so the shell prompt starts on a new one."""
        if self.tty and self._last is not None:
            self.stream.write("\n")
            self.stream.flush()
        self._last = None
