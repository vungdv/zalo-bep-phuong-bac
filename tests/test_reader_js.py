"""reader.js against synthetic DOM fixtures in headless Chromium (task T7, design §11 ZaloWeb).

The fixtures in tests/fixtures/dom/ are hand-written from the DOM notes; real-DOM fixtures need a human login
(task T2b). Tests are skipped if headless Chromium cannot be launched (`uv run playwright install chromium`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from order_extractor.zalo.reader import (
    DEFAULT_SELECTORS_PATH,
    PageState,
    SelectorsError,
    build_reader_script,
    load_selectors,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dom"
SELECTORS = load_selectors(ROOT / DEFAULT_SELECTORS_PATH)
SCRIPT = build_reader_script(SELECTORS)
GROUP = "g9000000000000000001"


@pytest.fixture(scope="module")
def page() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"playwright unavailable: {exc}")
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # pragma: no cover - environment dependent
        pw.stop()
        pytest.skip(f"headless Chromium unavailable: {exc}")
    pg = browser.new_page()
    yield pg
    browser.close()
    pw.stop()


def read(page: Any, fixture: str, scroll: str | None = None) -> dict[str, Any]:
    page.set_content((FIXTURES / fixture).read_text(encoding="utf-8"))
    if scroll == "bottom":
        page.evaluate("() => { const s = document.querySelector('#chatView .transform-gpu'); s.scrollTop = 1e9; }")
    raw = page.evaluate(SCRIPT)
    assert isinstance(raw, str)  # the script returns JSON text, as playwright-cli --raw eval prints it
    value = json.loads(raw)
    PageState.parse(value)  # the Python contract accepts it
    return value


def test_login_title(page: Any) -> None:
    state = read(page, "login.html")
    assert state["state"] == "LOGIN"
    assert state["messages"] == [] and state["conversation"] is None


def test_login_title_with_unread_prefix_is_still_login(page: Any) -> None:
    assert read(page, "login_unread.html")["state"] == "LOGIN"


def test_logged_in_without_chat_is_no_chat(page: Any) -> None:
    state = read(page, "no_chat.html")
    assert (state["state"], state["rendered_bubbles"]) == ("NO_CHAT", 0)


def test_empty_conversation_is_no_chat(page: Any) -> None:
    state = read(page, "empty_conversation.html")
    assert (state["state"], state["conversation"], state["messages"]) == ("NO_CHAT", None, [])


def test_bubbles_without_qid_are_no_chat_with_rendered_count(page: Any) -> None:
    state = read(page, "no_qid.html")
    assert state["state"] == "NO_CHAT"
    assert state["rendered_bubbles"] == 8
    assert state["messages"] == []


def test_group_chat_with_unread_title_is_reading(page: Any) -> None:
    state = read(page, "group_chat.html", scroll="bottom")
    assert state["state"] == "READING"
    assert state["drift"] is None
    assert state["conversation"] == {"key": GROUP, "title": "Nhóm Khách Sỉ Test", "is_group": True}
    assert state["at_bottom"] is True
    assert state["rendered_bubbles"] == 8


def test_group_chat_messages(page: Any) -> None:
    messages = read(page, "group_chat.html")["messages"]
    simplified = [(m["message_key"], m["sender"], m["is_self"], m["kind"], m["text"]) for m in messages]
    assert simplified == [
        ("1794544700000_1004", None, False, "text", "còn hàng không shop"),
        ("1794544726909_1001", "Lan", False, "text", "Shop ơi cho em 2 áo thun đen size M"),
        ("1794544730000_1001", None, False, "text", "=SUM(1,2)\ndòng 2"),
        ("1794544740000_1002", "Minh", False, "image", "[image]"),
        ("1794544750000_1002", None, False, "sticker", "[sticker]"),
        ("1794631100000_1003", "Hoa", False, "file", "[file]"),
        ("1794631110000_1003", None, False, "other", "[other]"),
        ("1794631120000_0", None, True, "text", "Dạ shop ghi nhận ạ"),
    ]
    first = messages[1]
    assert (first["sent_at_ms"], first["sender_uid"]) == (1794544726909, "1001")


def test_senders_map_only_from_rendered_name_headers(page: Any) -> None:
    assert read(page, "group_chat.html")["senders"] == {"1001": "Lan", "1002": "Minh", "1003": "Hoa"}


def test_at_bottom_false_when_scrolled_up(page: Any) -> None:
    state = read(page, "group_chat.html")  # scrollTop = 0 on a 200 px scroller
    assert state["at_bottom"] is False
    assert state["scroll_top"] == 0
    assert state["client_height"] == 200


def test_inconsistent_conversation_keys_report_drift(page: Any) -> None:
    state = read(page, "mixed_conversations.html")
    assert state["state"] == "READING"
    assert "disagree" in state["drift"]


def test_selectors_drive_the_reader(page: Any) -> None:
    custom = {**SELECTORS, "login_title": "Zalo - Shop Test"}
    page.set_content((FIXTURES / "no_chat.html").read_text(encoding="utf-8"))
    assert json.loads(page.evaluate(build_reader_script(custom)))["state"] == "LOGIN"


def test_page_exception_is_reported_as_error_state(page: Any) -> None:
    broken = {**SELECTORS, "chat_container": "#["}  # invalid CSS → querySelector throws
    page.set_content((FIXTURES / "group_chat.html").read_text(encoding="utf-8"))
    state = json.loads(page.evaluate(build_reader_script(broken)))
    assert state["state"] == "ERROR"
    assert state["error"]


# --- script assembly (no browser) -------------------------------------------------------------------------


def test_script_is_one_function_expression_within_argv_limits() -> None:
    assert SCRIPT.startswith("() => {") and SCRIPT.endswith("}")
    assert json.dumps(SELECTORS, ensure_ascii=False) in SCRIPT
    size = len(SCRIPT.encode())
    assert size < 131072  # Linux MAX_ARG_STRLEN for a single argv element
    assert size < os.sysconf("SC_ARG_MAX")


def test_load_selectors_requires_keys(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"login_title": "x"}))
    with pytest.raises(SelectorsError, match="lacks required selectors"):
        load_selectors(path)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"a": 1}'])
def test_load_selectors_rejects_bad_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    with pytest.raises(SelectorsError):
        load_selectors(path)


def test_load_selectors_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SelectorsError, match="cannot read"):
        load_selectors(tmp_path / "missing.json")


def test_page_state_oldest() -> None:
    state = PageState.parse(
        {
            "state": "READING",
            "messages": [
                {"message_key": "2_1", "sent_at_ms": 2, "sender_uid": "1"},
                {"message_key": "1_1", "sent_at_ms": 1, "sender_uid": "1"},
            ],
        }
    )
    oldest = state.oldest()
    assert oldest is not None and oldest.message_key == "1_1"
    assert PageState.parse({"state": "LOGIN"}).oldest() is None
