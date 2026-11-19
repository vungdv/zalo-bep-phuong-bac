"""Reader script assembly and its output contract (design §4.2).

* `load_selectors(path)` reads `selectors.json` and checks the required keys are present.
* `build_reader_script(selectors)` inlines the selectors into `reader.js` and wraps it as one function expression
  for `playwright-cli --raw eval` (built once at startup). Exceptions inside the page come back as
  `{"state": "ERROR", "error": ...}` instead of failing the eval, so ZaloWeb can report selector drift [R2.9].
* `PageState.model_validate(value)` validates what the script returns.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from order_extractor.events import Conversation, MessageKind

DEFAULT_SELECTORS_PATH = Path("selectors.json")
REQUIRED_SELECTORS = frozenset(
    {
        "login_title",
        "chat_container",
        "scroller",
        "conversation_title",
        "group_subtitle",
        "chat_item",
        "self_item_class",
        "message_item",
        "qid_node",
        "qid_regex",
        "message_text",
        "sender_name",
    }
)


class SelectorsError(ValueError):
    """`selectors.json` is missing, not a JSON object of strings, or lacks a required key."""


def load_selectors(path: str | Path = DEFAULT_SELECTORS_PATH) -> dict[str, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectorsError(f"cannot read selectors from {path}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise SelectorsError(f"{path} must be a JSON object of strings")
    missing = sorted(REQUIRED_SELECTORS - data.keys())
    if missing:
        raise SelectorsError(f"{path} lacks required selectors: {', '.join(missing)}")
    return data


def reader_source() -> str:
    return resources.files("order_extractor.zalo").joinpath("reader.js").read_text(encoding="utf-8")


def build_reader_script(selectors: dict[str, str]) -> str:
    """`() => { const SEL = {...}; <reader.js>; return JSON.stringify(readState(SEL)); }` (one argv element)."""
    sel = json.dumps(selectors, ensure_ascii=False)
    return (
        "() => {\n"
        f"const SEL = {sel};\n"
        f"{reader_source()}\n"
        "try { return JSON.stringify(readState(SEL)); }\n"
        'catch (e) { return JSON.stringify({state: "ERROR", error: String((e && e.message) || e)}); }\n'
        "}"
    )


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ReaderMessage(_Lenient):
    """One bubble as `reader.js` reports it (no conversation fields yet)."""

    message_key: str = Field(min_length=1)
    sent_at_ms: int = Field(ge=0)
    sender_uid: str
    sender: str | None = None
    is_self: bool = False
    kind: MessageKind = "other"
    text: str | None = None


class PageState(_Lenient):
    state: Literal["LOGIN", "NO_CHAT", "READING", "ERROR"]
    conversation: Conversation | None = None
    at_bottom: bool = True
    scroll_top: float = 0
    client_height: float = 0
    senders: dict[str, str] = Field(default_factory=dict)
    messages: list[ReaderMessage] = Field(default_factory=list)
    rendered_bubbles: int = 0
    drift: str | None = None
    error: str | None = None

    @classmethod
    def parse(cls, value: Any) -> PageState:
        return cls.model_validate(value)

    def oldest(self) -> ReaderMessage | None:
        return min(self.messages, key=lambda m: m.sent_at_ms, default=None)
