"""Test doubles shared by all test modules (design §3, §11).

Importable as `from fakes import FakeMediator` (pytest adds tests/ to sys.path via `pythonpath`).
"""

from __future__ import annotations

from typing import TypeVar

from order_extractor.events import Event

E = TypeVar("E", bound=Event)


class FakeMediator:
    """Records every notified event, in order. Satisfies `order_extractor.events.Mediator`."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: type[E]) -> list[E]:
        return [e for e in self.events if isinstance(e, event_type)]

    @property
    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def clear(self) -> None:
        self.events.clear()


# FakeDriver (ZaloWeb, task T9/T10): scripted BrowserDriver returning reader.js JSON per call.

# InMemorySheet (OrderRecorder, task T13): header, column A, append_rows with failure injection.
