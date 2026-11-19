"""ZaloWeb colleague: page-state machine, single ingest path, recovery (design §4.3, §4.4).

Public API (called by the Orchestrator):

* `open()` — make sure the `playwright-cli` session is open [R1.1].
* `poll()` — one read of the page; emits `LoginRequired` / `NoChatOpen` / `ZaloReady` once per transition,
  `NewMessages` for newly stored unchecked messages, `SelectorDrift`, or `BrowserDied`.
* `mark_checked(conversation_key, keys)` — reaction to `MessagesChecked` [R3.4].
* `recover()` — re-emit unchecked messages after a restart, per conversation, in chunks of 100 [R3.5].

Messages are committed before `NewMessages` is emitted (persist before notify).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

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
from order_extractor.zalo.browser import BrowserError, Driver
from order_extractor.zalo.messages_repo import ConversationRow, MessagesRepo
from order_extractor.zalo.reader import PageState, ReaderMessage

log = get_logger(__name__)

RECOVER_CHUNK = 100


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
    ) -> None:
        super().__init__(mediator)
        self.driver = driver
        self.repo = repo
        self.settings = settings
        self.reader_script = reader_script
        self.since_ms = since_ms
        self.clock = clock
        self.sleep = sleep
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
            self._enter_conversation(state.conversation)
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

    # --- ingest [R2.2, R2.3, R2.7, R2.10, R2.11] ---------------------------------------------------------------

    def ingest(self, messages: Iterable[ReaderMessage]) -> int:
        """The single insert path: chronological, de-duplicated, stored senders, watermark. Returns rows inserted."""
        conv = self.conversation
        if conv is None:
            return 0
        now = self.clock()
        batch: list[Message] = []
        inserted = 0
        for raw in sorted(messages, key=lambda m: m.sent_at_ms):
            if self.repo.exists(conv.key, raw.message_key):
                continue
            message = Message(
                conversation_key=conv.key,
                message_key=raw.message_key,
                sent_at_ms=raw.sent_at_ms,
                sender_uid=raw.sender_uid,
                sender=raw.sender or self.repo.sender_name(raw.sender_uid) or raw.sender_uid,
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
