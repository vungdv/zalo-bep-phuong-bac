"""FakeDriver and FakeChat: scripted stand-ins for BrowserDriver + the live page (ZaloWeb tests, T9/T10).

`FakeDriver.eval_json` answers from `queue` first (dicts or exceptions), then from `page` (a dict, an exception,
or a `FakeChat`). `FakeChat` models a virtualized message list: only `window` bubbles render, scrolling moves the
window, and `scroll_top` is 0 at the start of the chat.
"""

from __future__ import annotations

import contextlib
import copy
from pathlib import Path
from typing import Any

from order_extractor.zalo.browser import Relaunch, ScrollPosition

ROW_PX = 100
CLIENT_HEIGHT = 500


def bubble(
    ts: int,
    uid: str = "1001",
    text: str | None = "hi",
    *,
    sender: str | None = None,
    kind: str = "text",
    is_self: bool | None = None,
) -> dict[str, Any]:
    """One message as reader.js reports it."""
    return {
        "message_key": f"{ts}_{uid}",
        "sent_at_ms": ts,
        "sender_uid": uid,
        "sender": sender,
        "is_self": uid == "0" if is_self is None else is_self,
        "kind": kind,
        "text": text,
    }


def reading(
    messages: list[dict[str, Any]],
    *,
    key: str = "g1",
    title: str | None = "Nhóm",
    at_bottom: bool = True,
    scroll_top: float = 1000,
    senders: dict[str, str] | None = None,
    drift: str | None = None,
) -> dict[str, Any]:
    if senders is None:
        senders = {m["sender_uid"]: m["sender"] for m in messages if m["sender"] and not m["is_self"]}
    return {
        "state": "READING",
        "conversation": {"key": key, "title": title, "is_group": key.startswith("g")},
        "at_bottom": at_bottom,
        "scroll_top": scroll_top,
        "client_height": CLIENT_HEIGHT,
        "senders": senders,
        "messages": messages,
        "rendered_bubbles": len(messages),
        "drift": drift,
    }


LOGIN: dict[str, Any] = {"state": "LOGIN"}
NO_CHAT: dict[str, Any] = {"state": "NO_CHAT"}


class FakeChat:
    """A conversation of `messages` (oldest first) of which only the last `window` up to `end` are rendered."""

    def __init__(self, messages: list[dict[str, Any]], *, key: str = "g1", window: int = 5, title: str = "Nhóm"):
        self.messages = list(messages)
        self.key = key
        self.title = title
        self.window = window
        self.end = len(self.messages)

    @property
    def start(self) -> int:
        return max(0, self.end - self.window)

    @property
    def at_bottom(self) -> bool:
        return self.end == len(self.messages)

    def render(self) -> dict[str, Any]:
        visible = self.messages[self.start : self.end]
        return reading(
            visible, key=self.key, title=self.title, at_bottom=self.at_bottom, scroll_top=self.start * ROW_PX
        )

    def scroll(self, dy: int) -> None:
        rows = int(dy / ROW_PX)
        self.end = max(min(self.window, len(self.messages)), min(len(self.messages), self.end + rows))

    def to_bottom(self) -> None:
        self.end = len(self.messages)

    def append(self, *new: dict[str, Any]) -> None:
        follow = self.at_bottom
        self.messages.extend(new)
        if follow:
            self.end = len(self.messages)

    def position(self) -> ScrollPosition:
        total = len(self.messages) * ROW_PX
        return ScrollPosition(self.start * ROW_PX, max(total, CLIENT_HEIGHT), CLIENT_HEIGHT)


class FakeDriver:
    """Implements the `Driver` protocol of `order_extractor.zalo.browser` without a browser."""

    def __init__(self, page: Any = None, *, max_restarts: int = 3, snapshot_dir: Path | None = None) -> None:
        self.page = page
        self.queue: list[Any] = []
        self.max_restarts = max_restarts
        self.snapshot_dir = snapshot_dir or Path("snapshots")
        self.consecutive_failures = 0
        self.evals = 0
        self.opened = 0
        self.open_error: Exception | None = None
        self.scrolls: list[int | str] = []
        self.snapshots: list[Path] = []

    @property
    def chat(self) -> FakeChat | None:
        return self.page if isinstance(self.page, FakeChat) else None

    def ensure_open(self) -> None:
        self.opened += 1
        if self.open_error is not None:
            raise self.open_error

    def eval_json(self, js: str) -> Any:
        self.evals += 1
        item = self.queue.pop(0) if self.queue else self.page
        if isinstance(item, FakeChat):
            item = item.render()
        if isinstance(item, BaseException):
            raise item
        self.consecutive_failures = 0
        return copy.deepcopy(item)

    def snapshot(self, path: Path | None = None) -> Path:
        if path is None:
            path = self.snapshot_dir / f"snap{len(self.snapshots)}.yml"
        self.snapshots.append(path)
        return path

    def scroll_messages(self, dy: int) -> ScrollPosition:
        self.scrolls.append(dy)
        if self.chat is not None:
            self.chat.scroll(dy)
            return self.chat.position()
        return ScrollPosition(0, CLIENT_HEIGHT, CLIENT_HEIGHT)

    def scroll_to_bottom(self) -> ScrollPosition:
        self.scrolls.append("bottom")
        if self.chat is not None:
            self.chat.to_bottom()
            return self.chat.position()
        return ScrollPosition(0, CLIENT_HEIGHT, CLIENT_HEIGHT)

    def relaunch(self) -> Relaunch:
        self.consecutive_failures += 1
        attempt = self.consecutive_failures
        if attempt > self.max_restarts:
            return Relaunch(attempt, self.max_restarts, will_retry=False)
        with contextlib.suppress(Exception):
            self.ensure_open()
        return Relaunch(attempt, self.max_restarts, will_retry=True)

    @property
    def scrolled_up(self) -> int:
        return sum(1 for s in self.scrolls if isinstance(s, int) and s < 0)
