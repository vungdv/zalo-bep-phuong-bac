"""BrowserDriver: a thin wrapper around `playwright-cli` subprocess calls (design §4.1, R1.1, R1.4, R1.5).

* Every call is `subprocess.run([...])` with an argv list (never a shell), bounded by a timeout.
* JavaScript is passed as a **single argv element** (`playwright-cli -s=<session> --raw eval <js>`), so no escaping.
* `ensure_open()` checks `playwright-cli list --json` and opens the headed, persistent session only if it is missing.
* The only page action besides reading is scrolling the message list (`scroll_messages`, `scroll_to_bottom`) [R1.4].
* Consecutive failures are counted here: `relaunch()` counts one failure and reopens the session; a successful
  `eval_json` resets the counter. Once more than `BROWSER_MAX_RESTARTS` consecutive failures happen,
  `relaunch()` reports `will_retry=False` and does not relaunch again [R1.5].
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from order_extractor.config import Settings
from order_extractor.telemetry import get_logger

log = get_logger(__name__)

DEFAULT_CLI = "playwright-cli"
EVAL_TIMEOUT_SECONDS = 10.0
OPEN_TIMEOUT_SECONDS = 60.0

Runner = Callable[..., subprocess.CompletedProcess[str]]


class BrowserError(RuntimeError):
    """A `playwright-cli` call failed (non-zero exit, missing binary, unreadable output)."""


class BrowserTimeout(BrowserError):
    """A `playwright-cli` call did not finish within its timeout."""


class EvalError(BrowserError):
    """`eval` succeeded but its output is not the JSON the caller expected."""


@dataclass(frozen=True)
class Relaunch:
    """Outcome of `relaunch()`: the consecutive failure count and whether the caller should keep going."""

    attempt: int
    max_attempts: int
    will_retry: bool


@dataclass(frozen=True)
class ScrollPosition:
    scroll_top: float
    scroll_height: float
    client_height: float

    @property
    def at_bottom(self) -> bool:
        return self.scroll_top + self.client_height >= self.scroll_height - 5


class Driver(Protocol):
    """What ZaloWeb needs from a browser; `BrowserDriver` implements it, tests use `FakeDriver`."""

    def ensure_open(self) -> None: ...
    def eval_json(self, js: str) -> Any: ...
    def snapshot(self, path: Path | None = None) -> Path: ...
    def scroll_messages(self, dy: int) -> ScrollPosition: ...
    def scroll_to_bottom(self) -> ScrollPosition: ...
    def relaunch(self) -> Relaunch: ...


_SCROLL_JS = """() => {
  const el = document.querySelector(%(selector)s);
  if (!el) return JSON.stringify(null);
  if (%(to_bottom)s) { el.scrollTop = el.scrollHeight; } else { el.scrollTop = el.scrollTop + (%(dy)d); }
  return JSON.stringify({scroll_top: el.scrollTop, scroll_height: el.scrollHeight, client_height: el.clientHeight});
}"""


class BrowserDriver:
    """Drives the `playwright-cli` session named by `PW_SESSION`. Implements `Driver`."""

    def __init__(
        self,
        settings: Settings,
        *,
        scroller_selector: str,
        cli: str = DEFAULT_CLI,
        runner: Runner = subprocess.run,
        snapshot_dir: Path | None = None,
        eval_timeout: float = EVAL_TIMEOUT_SECONDS,
        open_timeout: float = OPEN_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = settings.pw_session
        self.url = settings.zalo_url
        self.profile_dir = settings.pw_profile_dir
        self.max_restarts = settings.browser_max_restarts
        self.scroller_selector = scroller_selector
        self.cli = cli
        self.snapshot_dir = snapshot_dir if snapshot_dir is not None else settings.db_path.parent / "snapshots"
        self.eval_timeout = eval_timeout
        self.open_timeout = open_timeout
        self._run = runner
        self._clock = clock
        self.consecutive_failures = 0

    # --- subprocess -------------------------------------------------------------------------------------------

    def _call(self, args: Sequence[str], timeout: float) -> str:
        argv = [self.cli, *args]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            command = next((a for a in args if not a.startswith("-")), "?")
            raise BrowserTimeout(f"{self.cli} {command} timed out after {timeout:g}s") from exc
        except OSError as exc:  # binary missing / not executable
            raise BrowserError(f"cannot run {self.cli}: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise BrowserError(f"{self.cli} exited {proc.returncode}: {detail}")
        return proc.stdout

    def _session_args(self, *args: str) -> list[str]:
        return [f"-s={self.session}", *args]

    # --- Driver API -------------------------------------------------------------------------------------------

    def is_open(self) -> bool:
        out = self._call(["list", "--json"], self.eval_timeout)
        try:
            browsers = json.loads(out).get("browsers", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise EvalError(f"unexpected `list --json` output: {out[:200]!r}") from exc
        return any(b.get("name") == self.session and b.get("status") == "open" for b in browsers)

    def ensure_open(self) -> None:
        """Open the headed, persistent session at `ZALO_URL` unless it is already open [R1.1]."""
        if self.is_open():
            return
        log.warning("browser_session_missing", extra={"session": self.session})
        self._call(
            self._session_args("open", self.url, "--headed", "--persistent", f"--profile={self.profile_dir}"),
            self.open_timeout,
        )
        log.info("browser_session_opened", extra={"session": self.session})

    def eval_json(self, js: str) -> Any:
        """Run `js` (a function expression) in the page and decode its JSON result; success resets the counter.

        The script may return a value or a JSON string (`JSON.stringify(...)`); both decode to Python objects.
        """
        out = self._call(self._session_args("--raw", "eval", js), self.eval_timeout).strip()
        try:
            value = json.loads(out)
            if isinstance(value, str):
                value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvalError(f"eval returned non-JSON output: {out[:200]!r}") from exc
        self.consecutive_failures = 0
        return value

    def snapshot(self, path: Path | None = None) -> Path:
        """Save an accessibility snapshot for selector-drift diagnostics [R2.9]; returns its path."""
        if path is None:
            path = self.snapshot_dir / f"{int(self._clock() * 1000)}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call(self._session_args("snapshot", f"--filename={path}"), self.eval_timeout)
        return path

    def _scroll(self, dy: int, to_bottom: bool) -> ScrollPosition:
        js = _SCROLL_JS % {
            "selector": json.dumps(self.scroller_selector),
            "to_bottom": "true" if to_bottom else "false",
            "dy": dy,
        }
        value = self.eval_json(js)
        if not isinstance(value, dict):
            raise EvalError(f"scroll container {self.scroller_selector!r} not found")
        try:
            return ScrollPosition(
                scroll_top=float(value["scroll_top"]),
                scroll_height=float(value["scroll_height"]),
                client_height=float(value["client_height"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvalError(f"scroll container {self.scroller_selector!r} returned an unexpected shape") from exc

    def scroll_messages(self, dy: int) -> ScrollPosition:
        """Scroll the message list by `dy` pixels (negative = up). The only UI action besides reading [R1.4]."""
        return self._scroll(int(dy), to_bottom=False)

    def scroll_to_bottom(self) -> ScrollPosition:
        return self._scroll(0, to_bottom=True)

    def relaunch(self) -> Relaunch:
        """Count one consecutive failure and, unless exhausted, reopen the session [R1.5].

        A failing `ensure_open()` is swallowed: the next read fails again and counts as the next attempt.
        """
        self.consecutive_failures += 1
        attempt = self.consecutive_failures
        if attempt > self.max_restarts:
            log.error("browser_restarts_exhausted", extra={"attempt": attempt, "max_attempts": self.max_restarts})
            return Relaunch(attempt, self.max_restarts, will_retry=False)
        try:
            self.ensure_open()
        except BrowserError as exc:
            log.error("browser_relaunch_failed", extra={"attempt": attempt, "error": str(exc)})
        return Relaunch(attempt, self.max_restarts, will_retry=True)
