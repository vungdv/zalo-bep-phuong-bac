"""BrowserDriver with a mocked subprocess runner (task T6)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from order_extractor.config import Settings
from order_extractor.zalo.browser import BrowserDriver, BrowserError, BrowserTimeout, EvalError, ScrollPosition

LIST_OPEN = json.dumps({"browsers": [{"name": "zalo", "status": "open"}]})
LIST_EMPTY = json.dumps({"browsers": []})


class FakeRunner:
    """Stands in for `subprocess.run`; answers each call from a queue of results (or exceptions)."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, tuple):
            code, out, err = result
            return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, result, "")


def make(runner: FakeRunner, tmp_path: Path, **overrides: Any) -> BrowserDriver:
    settings = Settings(db_path=tmp_path / "app.db", **overrides)
    return BrowserDriver(settings, scroller_selector="#chatView .transform-gpu", runner=runner, clock=lambda: 1234.5)


def test_ensure_open_does_nothing_when_session_listed(tmp_path: Path) -> None:
    runner = FakeRunner(LIST_OPEN)
    make(runner, tmp_path).ensure_open()
    assert [argv for argv, _ in runner.calls] == [["playwright-cli", "list", "--json"]]


def test_ensure_open_opens_headed_persistent_session_when_missing(tmp_path: Path) -> None:
    runner = FakeRunner(LIST_EMPTY, "")
    make(runner, tmp_path, pw_session="shop", pw_profile_dir=Path("data/p")).ensure_open()
    argv, kwargs = runner.calls[1]
    assert argv == [
        "playwright-cli",
        "-s=shop",
        "open",
        "https://chat.zalo.me",
        "--headed",
        "--persistent",
        "--profile=data/p",
    ]
    assert kwargs["timeout"] == 60.0


def test_ensure_open_treats_closed_or_other_session_as_missing(tmp_path: Path) -> None:
    listing = json.dumps({"browsers": [{"name": "zalo", "status": "closed"}, {"name": "x", "status": "open"}]})
    runner = FakeRunner(listing, "")
    make(runner, tmp_path).ensure_open()
    assert runner.calls[1][0][2] == "open"


def test_list_output_not_json_is_eval_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError):
        make(FakeRunner("### Browsers"), tmp_path).ensure_open()


def test_eval_json_passes_script_as_one_argv_element_without_shell(tmp_path: Path) -> None:
    script = '() => JSON.stringify({a: "it\'s; $(rm -rf /) `x`"})'
    runner = FakeRunner('{"a": 1}')
    assert make(runner, tmp_path).eval_json(script) == {"a": 1}
    argv, kwargs = runner.calls[0]
    assert argv == ["playwright-cli", "-s=zalo", "--raw", "eval", script]
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert kwargs["timeout"] == 10.0
    assert kwargs["capture_output"] is True


def test_eval_json_decodes_a_json_string_result(tmp_path: Path) -> None:
    runner = FakeRunner(json.dumps(json.dumps({"state": "LOGIN"})) + "\n")
    assert make(runner, tmp_path).eval_json("() => 1") == {"state": "LOGIN"}


def test_eval_json_error_on_non_json_output(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="non-JSON"):
        make(FakeRunner("Error: page crashed"), tmp_path).eval_json("() => 1")


def test_eval_nonzero_exit_is_browser_error(tmp_path: Path) -> None:
    with pytest.raises(BrowserError, match="exited 1: browser 'zalo' is not open"):
        make(FakeRunner((1, "", "browser 'zalo' is not open")), tmp_path).eval_json("() => 1")


def test_eval_timeout_is_browser_timeout(tmp_path: Path) -> None:
    runner = FakeRunner(subprocess.TimeoutExpired(["playwright-cli"], 10))
    with pytest.raises(BrowserTimeout, match="eval timed out after 10s"):
        make(runner, tmp_path).eval_json("() => 1")


def test_missing_binary_is_browser_error(tmp_path: Path) -> None:
    with pytest.raises(BrowserError, match="cannot run"):
        make(FakeRunner(FileNotFoundError("playwright-cli")), tmp_path).ensure_open()


def test_relaunch_counts_and_reopens_missing_session(tmp_path: Path) -> None:
    runner = FakeRunner(LIST_EMPTY, "")
    driver = make(runner, tmp_path)
    outcome = driver.relaunch()
    assert (outcome.attempt, outcome.max_attempts, outcome.will_retry) == (1, 3, True)
    assert runner.calls[1][0][2] == "open"


def test_counter_resets_after_successful_eval(tmp_path: Path) -> None:
    runner = FakeRunner(LIST_OPEN, LIST_OPEN, "1", LIST_OPEN)
    driver = make(runner, tmp_path)
    driver.relaunch()
    assert driver.relaunch().attempt == 2
    driver.eval_json("() => 1")
    assert driver.consecutive_failures == 0
    assert driver.relaunch().attempt == 1


def test_restarts_exhausted_after_max_consecutive_failures(tmp_path: Path) -> None:
    runner = FakeRunner(LIST_EMPTY, (1, "", "launch failed"), LIST_EMPTY, (1, "", "x"))
    driver = make(runner, tmp_path, browser_max_restarts=2)
    assert driver.relaunch().will_retry  # relaunch fails, swallowed
    assert driver.relaunch().will_retry
    final = driver.relaunch()
    assert (final.attempt, final.max_attempts, final.will_retry) == (3, 2, False)
    assert len(runner.calls) == 4  # no relaunch once exhausted


def test_snapshot_goes_to_data_snapshots(tmp_path: Path) -> None:
    runner = FakeRunner("")
    path = make(runner, tmp_path).snapshot()
    assert path == tmp_path / "snapshots" / "1234500.yml"
    assert path.parent.is_dir()
    assert runner.calls[0][0] == ["playwright-cli", "-s=zalo", "snapshot", f"--filename={path}"]


def test_snapshot_to_explicit_path(tmp_path: Path) -> None:
    target = tmp_path / "x" / "s.yml"
    assert make(FakeRunner(""), tmp_path).snapshot(target) == target


def test_scroll_messages_evals_scroll_on_the_configured_container(tmp_path: Path) -> None:
    runner = FakeRunner(json.dumps(json.dumps({"scroll_top": 100, "scroll_height": 2000, "client_height": 600})))
    pos = make(runner, tmp_path).scroll_messages(-600)
    assert pos == ScrollPosition(100.0, 2000.0, 600.0)
    assert not pos.at_bottom
    js = runner.calls[0][0][-1]
    assert '"#chatView .transform-gpu"' in js
    assert "el.scrollTop + (-600)" in js


def test_scroll_to_bottom(tmp_path: Path) -> None:
    runner = FakeRunner(json.dumps({"scroll_top": 1400, "scroll_height": 2000, "client_height": 600}))
    pos = make(runner, tmp_path).scroll_to_bottom()
    assert pos.at_bottom
    assert "el.scrollTop = el.scrollHeight" in runner.calls[0][0][-1]


def test_scroll_without_container_is_eval_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="not found"):
        make(FakeRunner("null"), tmp_path).scroll_messages(-100)
