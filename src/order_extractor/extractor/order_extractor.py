"""OrderExtractor colleague (design §5): stateless; applies the `OrderRule` to new messages [R4.1-R4.4].

For each batch it emits, in this order:

1. one `NewOrder` per customer text message the rule turns into a number (oldest first) — the Orchestrator hands
   each to `OrderRecorder.store()` synchronously, so the order is committed before step 2;
2. one `MessagesChecked` per conversation in the batch, listing every message key of that conversation, whether
   or not it produced an order [R4.4]. Batches normally hold one conversation; recovery may mix several.

A rule that raises, or returns something other than an `int` (or `None`), must not stop the service [R2.1, R6.1]:
the failure is logged as `rule_failed` (exception type only, never the message text) and that message is left
OUT of `MessagesChecked`. It stays unchecked, so recovery retries it on the next start [R3.4, R3.5] — it cannot
be marked checked while we do not know whether it holds an order.
"""

from __future__ import annotations

from collections.abc import Sequence

from order_extractor.events import Colleague, Mediator, Message, MessagesChecked, NewOrder
from order_extractor.extractor.rules import OrderRule
from order_extractor.telemetry import get_logger

log = get_logger(__name__)


class OrderExtractor(Colleague):
    def __init__(self, mediator: Mediator, rule: OrderRule) -> None:
        super().__init__(mediator)
        self.rule = rule

    def handle(self, messages: Sequence[Message]) -> None:
        """Apply the rule to `messages` (oldest → newest), then report every key as checked (except rule failures)."""
        keys_by_conversation: dict[str, list[str]] = {}
        for m in messages:
            keys = keys_by_conversation.setdefault(m.conversation_key, [])
            if m.is_self or m.kind != "text":  # shop messages and media never create orders [R4.2]
                keys.append(m.message_key)
                continue
            try:
                order = self._apply_rule(m)
            except Exception as exc:
                log.error(
                    "rule_failed",
                    extra={
                        "conversation_key": m.conversation_key,
                        "message_key": m.message_key,
                        "error_type": type(exc).__name__,
                    },
                )
                continue  # left unchecked: retried by recovery on the next start [R3.4, R3.5]
            keys.append(m.message_key)
            if order is None:
                continue
            log.info(
                "order_found",
                extra={"conversation_key": m.conversation_key, "message_key": m.message_key, "number": order.number},
            )
            self.mediator.notify(order)
        for conversation_key, keys in keys_by_conversation.items():
            if keys:
                self.mediator.notify(MessagesChecked(conversation_key=conversation_key, message_keys=keys))

    def _apply_rule(self, message: Message) -> NewOrder | None:
        """Run the rule on one message; raises if the rule raises or returns a non-int."""
        number = self.rule.extract(message)
        if number is None:
            return None
        if isinstance(number, bool) or not isinstance(number, int):
            raise TypeError("order rule must return int or None")
        return NewOrder.from_message(message, number)

    def recover(self) -> None:
        """Nothing to recover: the extractor is stateless and ZaloWeb re-emits unchecked messages [R3.5]."""
