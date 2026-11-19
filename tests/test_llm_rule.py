"""LlmRule against a fake HTTP session: no network, no Ollama."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from order_extractor.config import Settings
from order_extractor.events import Message
from order_extractor.extractor.llm_rule import SCHEMA, LlmRule
from order_extractor.extractor.rules import OrderRule


def msg(text: str | None) -> Message:
    return Message(conversation_key="g", message_key="1_1", sent_at_ms=1, sender_uid="1", text=text)


def rule_with(payload: dict | None = None, error: Exception | None = None):
    session = MagicMock()
    response = MagicMock()
    response.json.return_value = {"message": {"content": json.dumps(payload)}}
    if error:
        response.raise_for_status.side_effect = error
    session.post.return_value = response
    return LlmRule(Settings(llm_model="m", llm_url="http://h:1/", llm_timeout_seconds=7), session), session


def test_order_message_returns_the_number():
    rule, session = rule_with({"is_order": True, "number": 3})
    assert rule.extract(msg("Nhà em đặt 3 suất nhé")) == 3
    (url,), kwargs = session.post.call_args
    assert url == "http://h:1/api/chat"
    assert kwargs["timeout"] == 7.0
    body = kwargs["json"]
    assert body["model"] == "m" and body["format"] == SCHEMA and body["think"] is False and body["stream"] is False
    assert body["options"] == {"temperature": 0}
    assert body["messages"][1] == {"role": "user", "content": "<message>Nhà em đặt 3 suất nhé</message>"}


@pytest.mark.parametrize(
    "payload", [{"is_order": False, "number": 0}, {"is_order": True, "number": 0}, {"is_order": True, "number": True}]
)
def test_not_an_order_returns_none(payload):
    rule, _ = rule_with(payload)
    assert rule.extract(msg("cảm ơn shop")) is None


@pytest.mark.parametrize("text", [None, "", "   "])
def test_blank_text_never_calls_the_model(text):
    rule, session = rule_with({"is_order": True, "number": 1})
    assert rule.extract(msg(text)) is None
    session.post.assert_not_called()


def test_http_error_propagates_so_the_message_stays_unchecked():
    rule, _ = rule_with({}, error=requests.HTTPError("500"))
    with pytest.raises(requests.HTTPError):
        rule.extract(msg("đặt 3 suất"))


def test_malformed_answer_raises():
    session = MagicMock()
    session.post.return_value.json.return_value = {"message": {"content": "not json"}}
    with pytest.raises(json.JSONDecodeError):
        LlmRule(Settings(), session).extract(msg("đặt 3 suất"))


def test_default_session_retries_transient_errors():
    rule = LlmRule(Settings(llm_max_retries=4))
    assert isinstance(rule, OrderRule)
    adapter = rule._session.get_adapter("http://localhost:11434")
    assert adapter.max_retries.total == 4
