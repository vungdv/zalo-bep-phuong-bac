import pytest

from fakes import FakeMediator
from order_extractor.events import Colleague, Message, MessagesChecked, NewOrder
from order_extractor.extractor.order_extractor import OrderExtractor
from order_extractor.extractor.rules import StubRule
from rules_for_tests import FirstIntegerRule, PoisonedRule

CONV = "g1366548983169703056"


def msg(n: int, text: str | None = "cho em 2 áo", conv: str = CONV, **kw) -> Message:
    fields = {
        "conversation_key": conv,
        "message_key": f"{1000 + n}_77",
        "sent_at_ms": 1000 + n,
        "sender_uid": "77",
        "sender": "Lan",
        "text": text,
        "conversation_title": "Nhóm khách sỉ",
    }
    fields.update(kw)
    return Message(**fields)


@pytest.fixture
def mediator() -> FakeMediator:
    return FakeMediator()


def test_is_a_colleague_and_recover_is_noop(mediator):
    extractor = OrderExtractor(mediator, StubRule())
    assert isinstance(extractor, Colleague)
    extractor.recover()
    assert mediator.events == []


def test_rule_hit_emits_one_new_order_with_message_copy(mediator):
    extractor = OrderExtractor(mediator, FirstIntegerRule())
    m = msg(1, "Shop ơi cho em 2 áo thun đen size M")
    extractor.handle([m])
    assert mediator.types == ["NewOrder", "MessagesChecked"]
    order = mediator.of_type(NewOrder)[0]
    assert order == NewOrder(
        conversation_key=CONV,
        message_key=m.message_key,
        number=2,
        sent_at_ms=m.sent_at_ms,
        conversation_title="Nhóm khách sỉ",
        sender="Lan",
        text="Shop ơi cho em 2 áo thun đen size M",
    )


def test_self_and_media_messages_never_reach_the_rule(mediator):
    rule = FirstIntegerRule()
    batch = [
        msg(1, "shop: 5 cái", is_self=True, sender_uid="0"),
        msg(2, "[image]", kind="image"),
        msg(3, "[sticker] 3", kind="sticker"),
        msg(4, "[file] 4.pdf", kind="file"),
        msg(5, "[voice]", kind="voice"),
        msg(6, "khách: 7 cái"),
    ]
    OrderExtractor(mediator, rule).handle(batch)
    assert rule.seen == [batch[5].message_key]
    assert [o.number for o in mediator.of_type(NewOrder)] == [7]


def test_messages_checked_lists_every_key_after_all_new_orders(mediator):
    batch = [msg(1, "1 cái"), msg(2, "hello"), msg(3, "3 cái", is_self=True), msg(4, "4 cái"), msg(5, None)]
    OrderExtractor(mediator, FirstIntegerRule()).handle(batch)
    assert mediator.types == ["NewOrder", "NewOrder", "MessagesChecked"]
    assert [o.number for o in mediator.of_type(NewOrder)] == [1, 4]  # oldest first
    checked = mediator.of_type(MessagesChecked)[0]
    assert checked.conversation_key == CONV
    assert checked.message_keys == [m.message_key for m in batch]


def test_stub_rule_still_reports_messages_checked(mediator):
    batch = [msg(1), msg(2)]
    OrderExtractor(mediator, StubRule()).handle(batch)
    assert mediator.types == ["MessagesChecked"]
    assert mediator.events[0].message_keys == [m.message_key for m in batch]


def test_mixed_conversations_get_one_messages_checked_each(mediator):
    batch = [msg(1, "1", conv="gA"), msg(2, "2", conv="gB"), msg(3, "x", conv="gA")]
    OrderExtractor(mediator, FirstIntegerRule()).handle(batch)
    assert mediator.types == ["NewOrder", "NewOrder", "MessagesChecked", "MessagesChecked"]
    checked = mediator.of_type(MessagesChecked)
    assert [(c.conversation_key, c.message_keys) for c in checked] == [
        ("gA", [batch[0].message_key, batch[2].message_key]),
        ("gB", [batch[1].message_key]),
    ]


def test_empty_batch_emits_nothing(mediator):
    OrderExtractor(mediator, FirstIntegerRule()).handle([])
    assert mediator.events == []


def test_sender_falls_back_to_uid_and_title_copied(mediator):
    OrderExtractor(mediator, FirstIntegerRule()).handle([msg(1, "9", sender=None, conversation_title=None)])
    order = mediator.of_type(NewOrder)[0]
    assert order.sender == "77"
    assert order.conversation_title is None


def test_rule_exception_skips_only_that_message_and_leaves_it_unchecked(mediator, caplog):
    batch = [msg(1, "1 cái"), msg(2, "poison 9 secret-text"), msg(3, "3 cái")]
    with caplog.at_level("DEBUG", logger="order_extractor"):
        OrderExtractor(mediator, PoisonedRule()).handle(batch)
    assert [o.number for o in mediator.of_type(NewOrder)] == [1, 3]
    checked = mediator.of_type(MessagesChecked)[0]
    assert checked.message_keys == [batch[0].message_key, batch[2].message_key]  # failed one stays unchecked
    failures = [r for r in caplog.records if r.getMessage() == "rule_failed"]
    assert len(failures) == 1
    record = failures[0]
    assert record.levelname == "ERROR"
    assert (record.conversation_key, record.message_key, record.error_type) == (
        CONV,
        batch[1].message_key,
        "ValueError",
    )
    assert record.exc_info is None
    assert all("secret-text" not in r.getMessage() and "secret-text" not in str(vars(r)) for r in caplog.records)


def test_non_int_rule_result_is_treated_as_a_rule_failure(mediator, caplog):
    batch = [msg(1, "bad"), msg(2, "4 cái")]
    with caplog.at_level("ERROR", logger="order_extractor"):
        OrderExtractor(mediator, PoisonedRule()).handle(batch)
    assert [o.number for o in mediator.of_type(NewOrder)] == [4]
    assert mediator.of_type(MessagesChecked)[0].message_keys == [batch[1].message_key]
    assert [r.error_type for r in caplog.records if r.getMessage() == "rule_failed"] == ["TypeError"]


def test_batch_where_every_message_fails_emits_no_messages_checked(mediator):
    OrderExtractor(mediator, PoisonedRule()).handle([msg(1, "poison")])
    assert mediator.events == []
