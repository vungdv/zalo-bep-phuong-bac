"""LlmRule against a fake HTTP session: no network, no Ollama."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from order_extractor.config import Settings
from order_extractor.events import Message
from order_extractor.extractor.llm_rule import TOOL_NAME, TOOLS, LlmRule
from order_extractor.extractor.rules import OrderRule


def msg(text: str | None) -> Message:
    return Message(conversation_key="g", message_key="1_1", sent_at_ms=1, sender_uid="1", text=text)


def rule_with(payload: dict | None = None, error: Exception | None = None):
    session = MagicMock()
    response = MagicMock()
    calls = [] if payload is None else [{"function": {"name": TOOL_NAME, "arguments": payload}}]
    response.json.return_value = {"message": {"role": "assistant", "content": "", "tool_calls": calls}}
    if error:
        response.raise_for_status.side_effect = error
    session.post.return_value = response
    return LlmRule(Settings(llm_model="m", llm_url="http://h:1/", llm_timeout_seconds=7), session), session


def test_order_message_returns_the_number():
    rule, session = rule_with({"number": 3})
    assert rule.extract(msg("Nhà em đặt 3 suất nhé")) == 3
    (url,), kwargs = session.post.call_args
    assert url == "http://h:1/api/chat"
    assert kwargs["timeout"] == 7.0
    body = kwargs["json"]
    assert body["model"] == "m" and body["tools"] == TOOLS and body["think"] is False and body["stream"] is False
    assert body["options"] == {"temperature": 0}
    assert body["messages"][1] == {"role": "user", "content": "<message>Nhà em đặt 3 suất nhé</message>"}


@pytest.mark.parametrize(
    "payload", [None, {"number": 0}, {"number": True}, {"number": -2}]
)
def test_not_an_order_returns_none(payload):
    rule, _ = rule_with(payload)
    assert rule.extract(msg("cảm ơn shop")) is None


@pytest.mark.parametrize("text", [None, "", "   "])
def test_blank_text_never_calls_the_model(text):
    rule, session = rule_with({"number": 1})
    assert rule.extract(msg(text)) is None
    session.post.assert_not_called()


def test_http_error_propagates_so_the_message_stays_unchecked():
    rule, _ = rule_with(None, error=requests.HTTPError("500"))
    with pytest.raises(requests.HTTPError):
        rule.extract(msg("đặt 3 suất"))


def test_string_arguments_are_parsed():
    session = MagicMock()
    call = {"function": {"name": TOOL_NAME, "arguments": '{"number": 4}'}}
    session.post.return_value.json.return_value = {"message": {"tool_calls": [call]}}
    assert LlmRule(Settings(), session).extract(msg("đặt 4 suất")) == 4


def test_malformed_tool_call_raises():
    session = MagicMock()
    call = {"function": {"name": TOOL_NAME, "arguments": {}}}
    session.post.return_value.json.return_value = {"message": {"tool_calls": [call]}}
    with pytest.raises(KeyError):
        LlmRule(Settings(), session).extract(msg("đặt 3 suất"))


def test_default_session_retries_transient_errors():
    rule = LlmRule(Settings(llm_max_retries=4))
    assert isinstance(rule, OrderRule)
    adapter = rule._session.get_adapter("http://localhost:11434")
    assert adapter.max_retries.total == 4
