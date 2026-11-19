"""ZaloWeb colleague: page-state machine, single ingest path, recovery (design §4.3, §4.4).

Public API (called by the Orchestrator):

* `open()` — make sure the `playwright-cli` session is open [R1.1].
* `poll()` — one read of the page; emits `LoginRequired` / `NoChatOpen` / `ZaloReady` once per transition,
  `NewMessages` for newly stored unchecked messages, `SelectorDrift`, or `BrowserDied`.
* `mark_checked(conversation_key, keys)` — reaction to `MessagesChecked` [R3.4].
* `recover()` — re-emit unchecked messages after a restart, per conversation, in chunks of 100 [R3.5].
* `purge(now_ms)` — retention purge of messages, senders and DOM snapshots [R6.3].

Backfill runs on the first READING poll of a conversation in this run (also after a restart or a switch) and as
catch-up when the list returns to the bottom with a gap [R2.4, R2.5, R2.12]. It gathers the messages and sender
names of every screen in memory, then stores senders first and calls `ingest` once with everything, so a poll emits
at most one `NewMessages`, oldest first, even while backfilling [R2.2, R2.10]; a bubble without a name header can
take its sender name from an older screen [R2.7].

Scroll-back guarantee [R2.4, NFR1]: once backfill scrolls the list up, `_tool_scrolled` stays set until a
`scroll_to_bottom()` succeeds. Backfill scrolls back on every exit path (try/finally), and if that fails the next
`poll()` scrolls to the bottom before reading. Tool-initiated scrolling never counts as the human scrolling up.

A scroll call that fails with `EvalError` (the scroll container selector no longer matches) is selector drift,
not a dead browser [R2.9]: it is reported as `SelectorDrift`, the backfill stops, and nothing is relaunched.
`BrowserError`/`BrowserTimeout` from a scroll stay on the relaunch path [R1.5].

Messages are committed before `NewMessages` is emitted (persist before notify).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import ValidationError

from order_extractor.config import Settings
from order_extractor.events import (
    BrowserDied,
    Colleague,
    Conversation,
    LoginRequired,
    Mediator,
    Message,
    NewMessages,
    NoChatOpen,
    SelectorDrift,
    ZaloReady,
)
from order_extractor.telemetry import get_logger
from order_extractor.zalo.browser import BrowserError, Driver, EvalError
from order_extractor.zalo.messages_repo import ConversationRow, MessagesRepo
from order_extractor.zalo.reader import PageState, ReaderMessage

log = get_logger(__name__)

RECOVER_CHUNK = 100
BACKFILL_WAIT_SECONDS = 0.8
DEFAULT_SCROLL_PX = 600
MIN_SCROLL_PX = 100
SCROLLER_MISSING = "scroll container not found"
DAY_MS = 86_400_000


def epoch_ms() -> int:
    return int(time.time() * 1000)


class ZaloWeb(Colleague):
    def __init__(
        self,
        mediator: Mediator,
        driver: Driver,
        repo: MessagesRepo,
        settings: Settings,
        *,
        reader_script: str,
        since_ms: int | None = None,
        clock: Callable[[], int] = epoch_ms,
        sleep: Callable[[float], None] = time.sleep,
        snapshot_dir: Path | None = None,
    ) -> None:
        super().__init__(mediator)
        self.driver = driver
        self.repo = repo
        self.settings = settings
        self.reader_script = reader_script
        self.since_ms = since_ms
        self.clock = clock
        self.sleep = sleep
        self.snapshot_dir = snapshot_dir if snapshot_dir is not None else settings.db_path.parent / "snapshots"
        self.was_scrolled_up = False  # the *human* scrolled up (catch-up pending) [R2.12]
        self._tool_scrolled = False  # backfill scrolled up and has not yet returned to the bottom [R2.4]
        self.page_state: str | None = None  # last announced state: LOGIN / NO_CHAT / READING
        self.conversation: ConversationRow | None = None
        self._empty_since: int | None = None
        self._last_drift_ms: int | None = None

    # --- Orchestrator API -------------------------------------------------------------------------------------

    def open(self) -> None:
        try:
            self.driver.ensure_open()
        except BrowserError as exc:
            self._browser_failed(exc)

    def poll(self) -> None:
        if self._tool_scrolled and not self._scroll_back():
            return
        state = self._read()
        if state is None:
            return
        if state.state == "LOGIN":
            self._leave("LOGIN", LoginRequired())
            return
        self._watch_empty(state)
        if state.state == "NO_CHAT":
            self._leave("NO_CHAT", NoChatOpen())
            return
        if state.drift or state.conversation is None:
            self._drift(state.drift or "READING without a conversation key")
            return
        self.repo.upsert_senders(state.senders, self.clock())
        if self.conversation is None or state.conversation.key != self.conversation.key:
            # First READING poll of this run, after a restart, or a switch: backfill [R2.4, R2.5].
            self._enter_conversation(state.conversation)
            self.backfill(state)
            return
        if not state.at_bottom:
            # The human is reading history: keep ingesting what renders, remember to check for a gap [R2.12].
            if not self._tool_scrolled:  # our own scrolling is not the human reading history
                if not self.was_scrolled_up:
                    log.warning("live_reading_paused", extra={"conversation_key": self.conversation.key})
                self.was_scrolled_up = True
            self.ingest(state.messages)
            return
        if self.was_scrolled_up:
            self.was_scrolled_up = False
            oldest = state.oldest()
            if oldest is not None and not self.repo.exists(self.conversation.key, oldest.message_key):
                log.info("catch_up", extra={"conversation_key": self.conversation.key})
                self.backfill(state)
                return
        self.ingest(state.messages)

    def mark_checked(self, conversation_key: str, message_keys: Iterable[str]) -> int:
        return self.repo.mark_checked(conversation_key, message_keys)

    def recover(self) -> None:
        """Re-emit `NewMessages` for every unchecked message: one conversation per event, at most 100 each."""
        unchecked = self.repo.unchecked_messages()
        by_conversation: dict[str, list[Message]] = {}
        for message in unchecked:
            by_conversation.setdefault(message.conversation_key, []).append(message)
        for key, messages in by_conversation.items():
            for start in range(0, len(messages), RECOVER_CHUNK):
                self.mediator.notify(NewMessages(messages=messages[start : start + RECOVER_CHUNK]))
            log.info("messages_recovered", extra={"conversation_key": key, "count": len(messages)})

    def purge(self, now_ms: int | None = None) -> dict[str, int]:
        """Retention purge of ZaloWeb's own data: message tombstones, unused sender names, old snapshots [R6.3]."""
        now = self.clock() if now_ms is None else now_ms
        cutoff = now - self.settings.retention_days * DAY_MS
        messages, senders = self.repo.purge(cutoff, now)
        snapshots = 0
        if self.snapshot_dir.is_dir():
            for path in self.snapshot_dir.iterdir():
                if path.is_file() and path.stat().st_mtime * 1000 < cutoff:
                    path.unlink(missing_ok=True)
                    snapshots += 1
        result = {"messages": messages, "senders": senders, "snapshots": snapshots}
        log.info("zalo_purge", extra=result)
        return result

    # --- backfill [R2.4, R2.11, R2.12] ------------------------------------------------------------------------

    def backfill(self, state: PageState) -> int:
        """Gather the current screen, then scroll up screen by screen until a stop condition holds (design §4.4):

        1. the oldest rendered bubble is already stored, or was gathered from a newer screen of this backfill;
        2. the oldest rendered bubble is older than the start watermark;
        3. `BACKFILL_MAX_MESSAGES` new (not yet stored) messages were gathered;
        4. `scrollTop` stayed at 0 for two reads (start of the chat).

        Then store the gathered senders, `ingest` every gathered message once (one `NewMessages`, oldest first),
        and scroll back to the bottom (on every exit path). Returns the number of messages stored.
        """
        conv = self.conversation
        if conv is None:
            return 0
        gathered: dict[str, ReaderMessage] = {}
        senders: dict[str, str] = {}
        stored = 0
        reported = False  # a failure was already reported: a failing scroll-back must not report it twice
        try:
            failure: BrowserError | None = None
            try:
                reason = self._gather(conv, state, gathered, senders)
            except BrowserError as exc:
                failure, reason = exc, "browser_error"
            reported = reason == "read_failed"  # `_read` already reported it
            self.repo.upsert_senders(senders, self.clock())
            stored = self._ingest(conv, gathered.values())
            if failure is not None:
                reported = True
                self._browser_failed(failure)
        finally:
            if self._tool_scrolled:
                self._scroll_back(report=not reported)
        log.info("backfill_done", extra={"conversation_key": conv.key, "count": stored, "reason": reason})
        return stored

    def _gather(
        self,
        conv: ConversationRow,
        state: PageState,
        gathered: dict[str, ReaderMessage],
        senders: dict[str, str],
    ) -> str:
        """Collect messages (by key) and sender names (newest screen wins) screen by screen; returns why it stopped.

        Nothing is stored here, so the stop conditions see the database as it was before the backfill."""
        new = 0
        top_reads = 0
        for _ in range(self.settings.backfill_max_messages + 2):
            oldest = state.oldest()
            oldest_known = oldest is not None and (
                oldest.message_key in gathered or self.repo.exists(conv.key, oldest.message_key)
            )
            for uid, name in state.senders.items():
                senders.setdefault(uid, name)  # screens go newest → oldest: keep the newest name
            for message in state.messages:
                if message.message_key not in gathered:
                    gathered[message.message_key] = message
                    if not self.repo.exists(conv.key, message.message_key):
                        new += 1
            top_reads = top_reads + 1 if state.scroll_top <= 0 else 0
            if oldest is None:
                return "empty"
            if oldest_known:
                return "oldest_stored"
            if oldest.sent_at_ms < conv.start_ms:
                return "watermark"
            if new >= self.settings.backfill_max_messages:
                return "max_messages"
            if top_reads >= 2:
                return "start_of_chat"
            self._tool_scrolled = True  # before the call: a timed-out scroll may still have moved the list
            try:
                self.driver.scroll_messages(-self._viewport(state))
            except EvalError:
                self._drift(SCROLLER_MISSING)  # selector drift, not a dead browser: no relaunch [R2.9]
                return "scroll_container_missing"
            self.sleep(BACKFILL_WAIT_SECONDS)
            next_state = self._read()
            if next_state is None:
                return "read_failed"  # browser died or drift (already reported); the next READING poll starts over
            if next_state.state != "READING" or next_state.conversation is None or next_state.drift:
                return "page_changed"
            if next_state.conversation.key != conv.key:
                return "conversation_switched"
            state = next_state
        return "screen_limit"

    def _scroll_back(self, *, report: bool = True) -> bool:
        """Return the list to the bottom after tool scrolling; clears `_tool_scrolled` only on success [R2.4].

        Returns False only when the browser failed (the caller stops); a missing scroll container is reported as
        drift and reading goes on."""
        try:
            self.driver.scroll_to_bottom()
        except EvalError:
            self._drift(SCROLLER_MISSING)
            return True
        except BrowserError as exc:
            if report:
                self._browser_failed(exc)
            else:
                log.warning("scroll_back_failed", extra={"error_type": type(exc).__name__})
            return False
        self._tool_scrolled = False
        return True

    @staticmethod
    def _viewport(state: PageState) -> int:
        """Scroll by ~90 % of the visible height so consecutive screens overlap."""
        return max(int(state.client_height * 0.9), MIN_SCROLL_PX) if state.client_height else DEFAULT_SCROLL_PX

    # --- ingest [R2.2, R2.3, R2.7, R2.10, R2.11] ---------------------------------------------------------------

    def ingest(self, messages: Iterable[ReaderMessage]) -> int:
        """The single insert path: chronological, de-duplicated, stored senders, watermark. Returns rows inserted."""
        conv = self.conversation
        if conv is None:
            return 0
        return self._ingest(conv, messages)

    def _ingest(self, conv: ConversationRow, messages: Iterable[ReaderMessage]) -> int:
        now = self.clock()
        batch: list[Message] = []
        inserted = 0
        peer = conv.title if not conv.is_group else None  # a private chat shows no sender names: the peer is the title
        for raw in sorted(messages, key=lambda m: m.sent_at_ms):
            if self.repo.exists(conv.key, raw.message_key):
                continue
            message = Message(
                conversation_key=conv.key,
                message_key=raw.message_key,
                sent_at_ms=raw.sent_at_ms,
                sender_uid=raw.sender_uid,
                sender=raw.sender
                or self.repo.sender_name(raw.sender_uid)
                or (None if raw.is_self else peer)
                or raw.sender_uid,
                is_self=raw.is_self,
                kind=raw.kind,
                text=raw.text,
                conversation_title=conv.title,
            )
            checked = message.sent_at_ms < conv.start_ms
            if not self.repo.insert_message(message, extracted=checked, observed_at_ms=now):
                continue
            inserted += 1
            log.debug(
                "message_stored",
                extra={"conversation_key": conv.key, "message_key": message.message_key, "text": message.text},
            )
            if not checked:
                batch.append(message)
        if batch:
            log.info("new_messages", extra={"conversation_key": conv.key, "count": len(batch)})
            self.mediator.notify(NewMessages(messages=batch))
        return inserted

    # --- internals --------------------------------------------------------------------------------------------

    def _read(self) -> PageState | None:
        try:
            value = self.driver.eval_json(self.reader_script)
        except BrowserError as exc:
            self._browser_failed(exc)
            return None
        try:
            state = PageState.parse(value)
        except ValidationError as exc:
            self._drift(f"reader returned an unexpected shape: {exc.error_count()} error(s)")
            return None
        if state.state == "ERROR":
            self._drift(f"reader exception: {state.error}")
            return None
        return state

    def _leave(self, page_state: str, event: LoginRequired | NoChatOpen) -> None:
        self.conversation = None
        if self.page_state != page_state:
            self.page_state = page_state
            log.info("page_state", extra={"state": page_state})
            self.mediator.notify(event)

    def _enter_conversation(self, conversation: Conversation) -> None:
        now = self.clock()
        start_ms = self.since_ms if self.since_ms is not None else now
        self.conversation = self.repo.ensure_conversation(
            conversation.key, start_ms=start_ms, now_ms=now, title=conversation.title, is_group=conversation.is_group
        )
        self.page_state = "READING"
        log.info(
            "conversation_open",
            extra={
                "conversation_key": conversation.key,
                "start_ms": self.conversation.start_ms,
                "title": conversation.title,
            },
        )
        self.mediator.notify(ZaloReady(conversation=conversation))

    def _watch_empty(self, state: PageState) -> None:
        """Bubbles render but none parse (or READING with 0 messages) for SELECTOR_ALERT_SECONDS → drift [R2.9]."""
        empty = (state.state == "NO_CHAT" and state.rendered_bubbles > 0) or (
            state.state == "READING" and not state.messages
        )
        if not empty:
            self._empty_since = None
            return
        now = self.clock()
        if self._empty_since is None:
            self._empty_since = now
        elif now - self._empty_since >= self.settings.selector_alert_seconds * 1000:
            self._drift(f"{state.rendered_bubbles} bubble(s) rendered but none parsed")

    def _drift(self, reason: str) -> None:
        now = self.clock()
        window = self.settings.selector_alert_seconds * 1000
        if self._last_drift_ms is not None and now - self._last_drift_ms < window:
            return
        self._last_drift_ms = now
        try:
            path: str | None = str(self.driver.snapshot())
        except BrowserError as exc:
            log.error("snapshot_failed", extra={"error": str(exc)})
            path = None
        log.warning("selectors may be outdated", extra={"reason": reason, "snapshot_path": path})
        self.mediator.notify(SelectorDrift(reason=reason, snapshot_path=path))

    def _browser_failed(self, exc: BrowserError) -> None:
        self.page_state = None
        self.conversation = None
        outcome = self.driver.relaunch()
        log.error(
            "browser_died",
            extra={"attempt": outcome.attempt, "max_attempts": outcome.max_attempts, "error": str(exc)},
        )
        self.mediator.notify(
            BrowserDied(
                attempt=outcome.attempt,
                max_attempts=outcome.max_attempts,
                will_retry=outcome.will_retry,
                error=str(exc),
            )
        )
