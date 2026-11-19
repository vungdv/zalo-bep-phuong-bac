from order_extractor.events import Message
from order_extractor.extractor.rules import OrderRule, StubRule, default_rule
from rules_for_tests import FirstIntegerRule


def msg(text: str | None, **kw) -> Message:
    return Message(conversation_key="g1", message_key="1_2", sent_at_ms=1, sender_uid="2", text=text, **kw)


def test_stub_rule_never_finds_an_order():
    rule = StubRule()
    for text in ["Shop ơi cho em 2 áo", "42", "", None, "=1+1"]:
        assert rule.extract(msg(text)) is None


def test_default_rule_is_the_stub_and_satisfies_protocol():
    rule = default_rule()
    assert isinstance(rule, StubRule)
    assert isinstance(rule, OrderRule)
    assert isinstance(FirstIntegerRule(), OrderRule)


def test_first_integer_rule():
    rule = FirstIntegerRule()
    assert rule.extract(msg("Shop ơi cho em 2 áo thun đen size M")) == 2
    assert rule.extract(msg("no digits")) is None
    assert rule.extract(msg(None)) is None
    assert rule.extract(msg("15 cái, 3 màu")) == 15
