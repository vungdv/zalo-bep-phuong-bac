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


class PoisonedRule(FirstIntegerRule):
    """`FirstIntegerRule`, except messages whose text contains `poison` raise and `bad` returns a float."""

    def extract(self, message: Message) -> int | None:
        text = message.text or ""
        if "poison" in text:
            self.seen.append(message.message_key)
            raise ValueError(f"cannot parse {text!r}")  # message text in the exception: must never be logged
        if "bad" in text:
            self.seen.append(message.message_key)
            return 2.5  # type: ignore[return-value]
        return super().extract(message)
