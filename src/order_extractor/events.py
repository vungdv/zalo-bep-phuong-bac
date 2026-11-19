"""Mediator contract (design §1): domain value types, the event catalogue, `Mediator`, and `Colleague`.

Colleagues never call each other; they report what happened with `self.mediator.notify(event)` and the
Orchestrator routes it. Every event is a pydantic model with a `type` discriminator, so it validates on
construction and round-trips through JSON (`event.model_dump_json()` / `parse_event(data)`).

All times are integer epoch milliseconds (UTC), matching the SQLite schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

MessageKind = Literal["text", "image", "sticker", "file", "voice", "video", "link", "recalled", "other"]
"""Bubble kinds produced by reader.js. Only `text` can become an order [R4.2]; others carry a placeholder text."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Conversation(_Model):
    """The open chat. `key` is the `data-qid` suffix (e.g. `g1366…`) [R2.13]; `title` is display-only."""

    key: str = Field(min_length=1)
    title: str | None = None
    is_group: bool = False


class Message(_Model):
    """One chat bubble as stored by ZaloWeb. Mutable so ZaloWeb can resolve `sender` from stored names [R2.7]."""

    conversation_key: str = Field(min_length=1)
    message_key: str = Field(min_length=1)  # "<sentAtMs>_<senderUid>" [R2.3]
    sent_at_ms: int = Field(ge=0)
    sender_uid: str
    sender: str | None = None
    is_self: bool = False
    kind: MessageKind = "text"
    text: str | None = None
    conversation_title: str | None = None


# --- Events -------------------------------------------------------------------------------------------------


class Event(_Model):
    """Base of every event. Subclasses set a unique `type` literal."""

    type: str


class LoginRequired(Event):
    type: Literal["LoginRequired"] = "LoginRequired"


class NoChatOpen(Event):
    type: Literal["NoChatOpen"] = "NoChatOpen"


class ZaloReady(Event):
    type: Literal["ZaloReady"] = "ZaloReady"
    conversation: Conversation


class NewMessages(Event):
    """Stored, unchecked messages at/after the watermark, oldest first [R2.10]."""

    type: Literal["NewMessages"] = "NewMessages"
    messages: list[Message] = Field(min_length=1)


class SelectorDrift(Event):
    type: Literal["SelectorDrift"] = "SelectorDrift"
    reason: str
    snapshot_path: str | None = None


class BrowserDied(Event):
    type: Literal["BrowserDied"] = "BrowserDied"
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    will_retry: bool
    error: str | None = None


class NewOrder(Event):
    """An order found in one message, with a copy of the fields the sheet needs [R4.3]."""

    type: Literal["NewOrder"] = "NewOrder"
    conversation_key: str = Field(min_length=1)
    message_key: str = Field(min_length=1)
    number: int
    sent_at_ms: int = Field(ge=0)
    conversation_title: str | None = None
    sender: str | None = None
    text: str | None = None

    @classmethod
    def from_message(cls, message: Message, number: int, conversation_title: str | None = None) -> NewOrder:
        """Copy `message` into an order. `conversation_title` overrides `message.conversation_title`."""
        return cls(
            conversation_key=message.conversation_key,
            message_key=message.message_key,
            number=number,
            sent_at_ms=message.sent_at_ms,
            conversation_title=conversation_title if conversation_title is not None else message.conversation_title,
            sender=message.sender if message.sender is not None else message.sender_uid,
            text=message.text,
        )


class MessagesChecked(Event):
    """Every message key of a handled batch, emitted after the batch's `NewOrder`s [R4.4]."""

    type: Literal["MessagesChecked"] = "MessagesChecked"
    conversation_key: str = Field(min_length=1)
    message_keys: list[str]


class OrderRecorded(Event):
    type: Literal["OrderRecorded"] = "OrderRecorded"
    order_id: int
    sheet_row: int | None = None  # None when reconciled from column A (row unknown)


class RecordFailed(Event):
    type: Literal["RecordFailed"] = "RecordFailed"
    order_ids: list[int] = Field(min_length=1)
    error: str
    next_attempt_at_ms: int | None = None  # None when `final`
    final: bool = False


class RecorderBlocked(Event):
    type: Literal["RecorderBlocked"] = "RecorderBlocked"
    reason: str


AnyEvent = Annotated[
    LoginRequired
    | NoChatOpen
    | ZaloReady
    | NewMessages
    | SelectorDrift
    | BrowserDied
    | NewOrder
    | MessagesChecked
    | OrderRecorded
    | RecordFailed
    | RecorderBlocked,
    Field(discriminator="type"),
]

_EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def parse_event(data: str | bytes | dict[str, Any]) -> Event:
    """Rebuild an event from its JSON text or `model_dump()` dict (dispatching on `type`)."""
    if isinstance(data, str | bytes):
        return _EVENT_ADAPTER.validate_json(data)
    return _EVENT_ADAPTER.validate_python(data)


# --- Mediator pattern ---------------------------------------------------------------------------------------


@runtime_checkable
class Mediator(Protocol):
    """Receives every event and decides which colleague handles it next. Dispatch is synchronous."""

    def notify(self, event: Event) -> None: ...


class Colleague(ABC):
    """Base of ZaloWeb, OrderExtractor and OrderRecorder: knows only the mediator."""

    def __init__(self, mediator: Mediator) -> None:
        self.mediator = mediator

    @abstractmethod
    def recover(self) -> None:
        """Resume this colleague's own unfinished work after a restart [R3.5]."""
