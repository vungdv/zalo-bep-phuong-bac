"""OrderExtractor colleague (design §5): stateless; applies the `OrderRule` to new messages [R4.1-R4.4].

For each batch it emits, in this order:

1. one `NewOrder` per customer text message the rule turns into a number (oldest first) — the Orchestrator hands
   each to `OrderRecorder.store()` synchronously, so the order is committed before step 2;
2. one `MessagesChecked` per conversation in the batch, listing every message key of that conversation, whether
   or not it produced an order [R4.4]. Batches normally hold one conversation; recovery may mix several.
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
        """Apply the rule to `messages` (oldest → newest), then report every key as checked."""
        keys_by_conversation: dict[str, list[str]] = {}
        for m in messages:
            keys_by_conversation.setdefault(m.conversation_key, []).append(m.message_key)
            if m.is_self or m.kind != "text":  # shop messages and media never create orders [R4.2]
                continue
            number = self.rule.extract(m)
            if number is None:
                continue
            log.info(
                "order_found",
                extra={"conversation_key": m.conversation_key, "message_key": m.message_key, "number": number},
            )
            self.mediator.notify(NewOrder.from_message(m, number))
        for conversation_key, keys in keys_by_conversation.items():
            self.mediator.notify(MessagesChecked(conversation_key=conversation_key, message_keys=keys))

    def recover(self) -> None:
        """Nothing to recover: the extractor is stateless and ZaloWeb re-emits unchecked messages [R3.5]."""
