"""Order rules used by tests and the manual E2E until Q-rule is specified (tasks T11, T16)."""

from __future__ import annotations

import re

from order_extractor.events import Message

_INTEGER = re.compile(r"\d+")


class FirstIntegerRule:
    """The first run of digits in the text is the order number; no digits → not an order."""

    def __init__(self) -> None:
        self.seen: list[str] = []  # message keys the rule was asked about

    def extract(self, message: Message) -> int | None:
        self.seen.append(message.message_key)
        match = _INTEGER.search(message.text or "")
        return int(match.group()) if match else None
