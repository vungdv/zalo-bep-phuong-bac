import pytest
from pydantic import ValidationError

from fakes import FakeMediator
from order_extractor.events import (
    BrowserDied,
    Colleague,
    Conversation,
    Event,
    LoginRequired,
    Mediator,
    Message,
    MessagesChecked,
    NewMessages,
    NewOrder,
    NoChatOpen,
    OrderRecorded,
    RecorderBlocked,
    RecordFailed,
    SelectorDrift,
    ZaloReady,
    parse_event,
)


def make_message(**overrides) -> Message:
    fields = {
        "conversation_key": "g1366548983169703056",
        "message_key": "1794544726909_3808620345438131900",
        "sent_at_ms": 1794544726909,
        "sender_uid": "3808620345438131900",
        "sender": "Lan",
        "is_self": False,
        "kind": "text",
        "text": "Shop ơi cho em 2 áo thun đen size M",
    }
    return Message(**(fields | overrides))


ALL_EVENTS: list[Event] = [
    LoginRequired(),
    NoChatOpen(),
    ZaloReady(conversation=Conversation(key="g1", title="Nhóm khách sỉ", is_group=True)),
    NewMessages(messages=[make_message()]),
    SelectorDrift(reason="0 messages for 60 s", snapshot_path="data/snapshots/1.yml"),
    BrowserDied(attempt=1, max_attempts=3, will_retry=True, error="no session"),
    NewOrder.from_message(make_message(), 2, "Nhóm khách sỉ"),
    MessagesChecked(conversation_key="g1", message_keys=["1_2", "3_4"]),
    OrderRecorded(order_id=42, sheet_row=7),
    RecordFailed(order_ids=[42], error="timeout", next_attempt_at_ms=1794544730000),
    RecorderBlocked(reason="header mismatch"),
]


@pytest.mark.parametrize("event", ALL_EVENTS, ids=lambda e: e.type)
def test_events_round_trip_through_json(event):
    assert parse_event(event.model_dump_json()) == event
    assert parse_event(event.model_dump()) == event


def test_every_catalogue_event_has_a_unique_type():
    assert len({e.type for e in ALL_EVENTS}) == 11


def test_new_order_copies_message_fields():
    order = NewOrder.from_message(make_message(conversation_title="From message"), 2)
    assert (order.conversation_key, order.message_key, order.number) == (
        "g1366548983169703056",
        "1794544726909_3808620345438131900",
        2,
    )
    assert order.sent_at_ms == 1794544726909
    assert order.sender == "Lan"
    assert order.text.startswith("Shop ơi")
    assert order.conversation_title == "From message"
    assert NewOrder.from_message(make_message(), 2, "Override").conversation_title == "Override"


def test_new_order_falls_back_to_sender_uid():
    assert NewOrder.from_message(make_message(sender=None), 1).sender == "3808620345438131900"


@pytest.mark.parametrize(
    "build",
    [
        lambda: make_message(kind="gif"),
        lambda: make_message(sent_at_ms=-1),
        lambda: make_message(message_key=""),
        lambda: make_message(unexpected="x"),
        lambda: NewMessages(messages=[]),
        lambda: BrowserDied(attempt=0, max_attempts=3, will_retry=True),
        lambda: RecordFailed(order_ids=[], error="x"),
    ],
)
def test_invalid_payloads_are_rejected(build):
    with pytest.raises(ValidationError):
        build()


def test_parse_event_rejects_unknown_type():
    with pytest.raises(ValidationError):
        parse_event({"type": "Nope"})


def test_message_sender_is_mutable():
    m = make_message(sender=None)
    m.sender = "Lan"
    assert m.sender == "Lan"


def test_fake_mediator_records_events_in_order():
    mediator = FakeMediator()
    assert isinstance(mediator, Mediator)
    for event in ALL_EVENTS:
        mediator.notify(event)
    assert mediator.events == ALL_EVENTS
    assert mediator.types[:2] == ["LoginRequired", "NoChatOpen"]
    assert mediator.of_type(OrderRecorded) == [OrderRecorded(order_id=42, sheet_row=7)]
    mediator.clear()
    assert mediator.events == []


def test_colleague_holds_mediator_and_requires_recover():
    with pytest.raises(TypeError):
        Colleague(FakeMediator())  # abstract

    class Echo(Colleague):
        def recover(self) -> None:
            self.mediator.notify(NoChatOpen())

    mediator = FakeMediator()
    colleague = Echo(mediator)
    colleague.recover()
    assert colleague.mediator is mediator
    assert mediator.types == ["NoChatOpen"]
