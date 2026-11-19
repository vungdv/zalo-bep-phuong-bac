"""Order rules (design §5, R4.5): turn one customer text message into an order number, or `None`.

The real rule is still to be specified (Q-rule). Until then the shipped rule is `StubRule`, which never finds an
order. Tests and the manual E2E inject their own rule through the `OrderRule` protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from order_extractor.events import Message


@runtime_checkable
class OrderRule(Protocol):
    """Pure function object: no I/O, no state that changes between calls."""

    def extract(self, message: Message) -> int | None:
        """Return the order number found in `message`, or `None` if it is not an order."""
        ...


class StubRule:
    """Placeholder rule shipped until Q-rule is answered: no message is an order [R4.5]."""

    def extract(self, message: Message) -> int | None:
        return None


def default_rule() -> OrderRule:
    """The rule the application wires in (composition root)."""
    return StubRule()
