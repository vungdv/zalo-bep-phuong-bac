"""R1 — Browser session & login (acceptance)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from acceptance.harness import (
    READER,
    REPO_ROOT,
    App,
    FakePlaywrightCli,
    PageWorker,
    all_messages,
    chat_evaluator,
)
from fake_driver import LOGIN, NO_CHAT, FakeChat, bubble
from fake_sheet import InMemorySheet
from order_extractor.config import Settings
from order_extractor.orchestrator import EXIT_OK
from order_extractor.recorder.sheet import HEADER
from order_extractor.status import LOGIN_PROMPT, NO_CHAT_PROMPT
from order_extractor.zalo.browser import BrowserDriver
from order_extractor.zalo.reader import build_reader_script, load_selectors
from rules_for_tests import FirstIntegerRule

SELECTORS = load_selectors(REPO_ROOT / "selectors.json")


def real_driver(db: Path, cli: FakePlaywrightCli, **settings: object) -> BrowserDriver:
    return BrowserDriver(Settings(db_path=db, **settings), scroller_selector=SELECTORS["scroller"], runner=cli)


async def test_r1_1_start_opens_the_headed_persistent_zalo_session_at_chat_zalo_me(db: Path):
    """R1.1: no `zalo` session yet → `playwright-cli -s=zalo open https://chat.zalo.me --headed` with the profile."""
    cli = FakePlaywrightCli()
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli))
    assert await app.run(max_ticks=3) == EXIT_OK

    opens = [a for a in cli.argv if "open" in a]
    assert len(opens) == 1
    argv = opens[0]
    assert argv[0] == "playwright-cli" and "-s=zalo" in argv
    assert "https://chat.zalo.me" in argv
    assert "--headed" in argv
    assert any(a.startswith("--profile=") and a.endswith("data/browser-profile") for a in argv)
    assert cli.commands()[0] == "list"  # attach is tried first


async def test_r1_1_attaches_to_an_already_open_session(db: Path):
    """R1.1: `open (or attach to)`: an open `zalo` session is reused, no second browser is launched."""
    cli = FakePlaywrightCli()
    cli.open_sessions.add("zalo")
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli))
    assert await app.run(max_ticks=3) == EXIT_OK
    assert "open" not in cli.commands()
    assert cli.commands().count("eval") == 3


async def test_r1_2_login_screen_prints_the_prompt_and_reads_nothing(db: Path):
    """R1.2: while the QR screen is shown, print the login prompt and poll without reading messages."""
    rule = FirstIntegerRule()
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), page=LOGIN, rule=rule)
    assert await app.run(max_ticks=5) == EXIT_OK
    assert app.driver.evals == 5  # it keeps polling
    assert any("Waiting for login: scan the QR code in the browser" in line for line in app.status_lines)
    assert all_messages(db) == [] and rule.seen == []
    assert len(app.of_type("LoginRequired")) == 1  # once per transition


async def test_r1_3_no_chat_open_prints_the_prompt_and_waits(db: Path):
    """R1.3: logged in but no conversation open → "Open a chat to monitor", nothing read."""
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), page=NO_CHAT)
    assert await app.run(max_ticks=4) == EXIT_OK
    assert any(NO_CHAT_PROMPT in line for line in app.status_lines)
    assert all_messages(db) == []


async def test_r1_2_r1_3_real_reader_login_with_unread_prefix_then_empty_conversation(db: Path, chromium: PageWorker):
    """R1.2 + R1.3 with the real reader.js: `(3) Zalo - Đăng nhập Zalo` is the login screen; an open conversation
    without rendered messages is "Open a chat to monitor"; neither stores anything."""
    chromium.load("login_unread.html")
    cli = FakePlaywrightCli(chromium.evaluate)
    script = build_reader_script(SELECTORS)
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli), reader_script=script)
    app.on_tick.append(lambda n: chromium.load("empty_conversation.html") if n == 3 else None)
    assert await app.run(max_ticks=6) == EXIT_OK

    text = "\n".join(app.status_lines)
    assert LOGIN_PROMPT in text and NO_CHAT_PROMPT in text
    assert text.index(LOGIN_PROMPT) < text.index(NO_CHAT_PROMPT)
    assert all_messages(db) == []


async def test_r1_4_only_reading_and_scrolling_reach_the_browser(db: Path):
    """R1.4: across backfill, live reading and catch-up the tool only lists/opens the session, evaluates the reader,
    and scrolls the message list; it never clicks, types, fills or presses anything."""
    chat = FakeChat([bubble(1_000 + i, "1001", f"tin {i}", sender="Lan") for i in range(20)], window=5)
    cli = FakePlaywrightCli(chat_evaluator(chat))
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli))

    def human(n: int) -> None:
        if n == 3:
            chat.scroll(-800)  # the human scrolls up to read history
        if n == 5:
            chat.append(*[bubble(5_000 + i, "1002", f"mới {i}", sender="Minh") for i in range(8)])
        if n == 7:
            chat.to_bottom()

    app.on_tick.append(human)
    assert await app.run(max_ticks=10) == EXIT_OK

    assert set(cli.commands()) <= {"list", "open", "eval"}
    scripts = [a[-1] for a in cli.argv if "eval" in a]
    for js in scripts:
        assert js == READER or ("scrollTop" in js and "click" not in js and "focus" not in js)
    assert any(js != READER for js in scripts)  # it did scroll (backfill / catch-up)
    assert len(all_messages(db)) == 28


async def test_r1_4_real_page_dom_is_left_untouched(db: Path, chromium: PageWorker):
    """R1.4: after reading a real fixture (with backfill scrolling) the page's DOM is byte-for-byte unchanged."""
    chromium.load("group_chat.html")
    before = chromium.body()
    cli = FakePlaywrightCli(chromium.evaluate)
    script = build_reader_script(SELECTORS)
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli), reader_script=script)
    assert await app.run(max_ticks=4) == EXIT_OK
    assert len(all_messages(db)) == 8
    assert chromium.body() == before
    assert set(cli.commands()) <= {"list", "open", "eval"}


async def test_r1_5_exits_non_zero_after_browser_max_restarts_failed_relaunches(db: Path, caplog):
    """R1.5: the browser dies and every relaunch fails → stop reading, log an error, relaunch; after
    BROWSER_MAX_RESTARTS (3) consecutive failed relaunches the run exits non-zero."""
    chat = FakeChat([bubble(1_000, "1001", "a", sender="Lan")])
    cli = FakePlaywrightCli(chat_evaluator(chat))
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli, browser_max_restarts=3))

    def browser_closed(n: int) -> None:
        if n == 2:
            cli.kill()
            cli.can_open = False

    app.on_tick.append(browser_closed)
    caplog.set_level(logging.ERROR, logger="order_extractor")
    code = await app.run(max_ticks=50)

    assert code != 0
    assert cli.failed_opens == 3  # three relaunch attempts, all failed
    assert app.ticks < 50
    assert any(r.getMessage() == "browser_died" and r.levelno == logging.ERROR for r in caplog.records)
    died = app.of_type("BrowserDied")
    assert [e.will_retry for e in died] == [True, True, True, False]
    assert any("browser down" in line for line in app.status_lines)


async def test_r1_5_counter_resets_after_a_successful_read(db: Path):
    """R1.5: a death followed by a successful relaunch + read resets the counter, so repeated isolated deaths
    (more than BROWSER_MAX_RESTARTS in total) never stop the run."""
    chat = FakeChat([bubble(1_000, "1001", "a", sender="Lan")])
    cli = FakePlaywrightCli(chat_evaluator(chat))
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), driver=real_driver(db, cli, browser_max_restarts=2))
    app.on_tick.append(lambda n: cli.kill() if n % 3 == 0 else None)  # dies every third tick, relaunch works
    assert await app.run(max_ticks=24) == EXIT_OK

    died = app.of_type("BrowserDied")
    assert len(died) >= 6  # far more than BROWSER_MAX_RESTARTS deaths in total
    assert {e.attempt for e in died} == {1}
    assert all(e.will_retry for e in died)
    assert cli.commands().count("open") >= 7  # initial open + one relaunch per death


def test_r1_6_browser_profile_directory_is_git_ignored():
    """R1.6: the default profile directory (./data/browser-profile) is git-ignored."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    assert Settings().pw_profile_dir == Path("data/browser-profile")
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "data/browser-profile/Default/Cookies"], cwd=REPO_ROOT, check=False
    )
    assert probe.returncode == 0


async def test_r1_2_session_expiring_mid_run_pauses_reading_until_login_then_resumes(db: Path):
    """R1.2: the session expires while reading → the login prompt is shown and nothing is read; after the human
    logs in again reading resumes and the message sent meanwhile is picked up."""
    chat = FakeChat([bubble(1_000, "1001", "lấy 1", sender="Lan")])
    app = App(db, sheet=InMemorySheet(header=list(HEADER)), page=chat)
    late = bubble(2_000, "1001", "lấy 2", sender="Lan")

    def human(n: int) -> None:
        if n == 3:
            app.driver.page = LOGIN
        if n == 5:
            chat.append(late)
        if n == 8:
            app.driver.page = chat

    app.on_tick.append(human)
    stored_during_login: list[int] = []
    app.on_tick.append(lambda n: stored_during_login.append(len(all_messages(db))) if n == 7 else None)
    assert await app.run(max_ticks=11) == EXIT_OK

    assert stored_during_login == [1]
    assert len(all_messages(db)) == 2
    assert [e.type for e in app.events if e.type in ("ZaloReady", "LoginRequired")] == [
        "ZaloReady",
        "LoginRequired",
        "ZaloReady",
    ]
    assert any(LOGIN_PROMPT in line for line in app.status_lines)
