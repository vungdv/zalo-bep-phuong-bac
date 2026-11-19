# Tasks — Zalo Order Extractor

> Phase 3 of 3. Implements [design.md](design.md) (Rev 5) and [requirements.md](requirements.md) (Rev 5).
> Work top to bottom. A task is done only when its tests pass. Each task is a vertical slice within one colleague's
> boundary and can be tested on its own.

## Milestone 1 — Skeleton & discovery

- [ ] **T1. Scaffold**: install `uv`; add `pyproject.toml` (Python 3.12), the package layout from design §3, a typer CLI stub, ruff/pytest with `pytest-cov` (`--cov-fail-under=80`), and `.env.example` (all keys from design §9, with placeholder values only). `.gitignore` and `git init` are already done; check that `data/`, `secrets/`, `.env`, and `.playwright-cli/` stay ignored.
  _Done when:_ `uv run order-extractor --help` works and `git status` shows no ignored paths. [R1.6, R6.1]
- [x] **T2. DOM discovery spike (needs human login)**: done 2026-11-19 on a live group. Results are in [`selectors.json`](../selectors.json) and [notes/zalo-dom.md](notes/zalo-dom.md).
  Key finding: `data-qid` gives the exact send time, sender uid, and conversation key.
- [ ] **T2b. DOM fixtures + open DOM questions (needs human login)**: save sanitized HTML fixtures (names, text, uids replaced) for: login screen, no chat open, an empty conversation, a 1-1 chat, and a group chat with text, image, album, sticker, file, voice, link-card, recalled, and self messages. Answer and record in the notes:
  (a) the 1-1 qid suffix (A3); (b) whether album/multi-part bubbles share `<tsMs>_<uid>` (A5); (c) whether the title gets an unread prefix like `(3) Zalo - …`; (d) selectors for voice, video, link cards, and recalled messages, plus a tighter sticker selector than `[class*="sticker"]`. [R1.2, R1.3, R2.1, R2.3, R2.6–R2.8, R2.13]

## Milestone 2 — Core: events, telemetry, database

- [ ] **T3. Events & mediator contract** (`events.py`): pydantic `Message`, `NewOrder`, and every event in the design §1 catalogue; the `Mediator` protocol; the `Colleague` base with `recover()`; `tests/fakes.py::FakeMediator`.
  _Tests:_ events validate and serialize; `FakeMediator` records events in order. [R3.1, R3.2, R3.6]
- [ ] **T4. Telemetry** (`telemetry.py`): JSON formatter, `tick_id` context var, stderr + rotating file handlers, nothing on stdout.
  _Tests:_ lines are valid JSON with `tick_id`; message text is absent at INFO; nothing written to stdout. [R6.1, R6.5]
- [ ] **T5. DB + migrations** (`db/`): WAL connection, `PRAGMA user_version` runner, `0001_init.sql` with the schema from design §7.
  _Tests:_ migrating an empty DB yields the expected schema; re-running is a no-op; a failing migration rolls back. [R3.4]

## Milestone 3 — Reading (ZaloWeb colleague)

- [ ] **T6. BrowserDriver**: `ensure_open`, `eval_json` (script as one argv element, no shell), `snapshot` into `data/snapshots/`, `scroll_messages`, timeouts, and the consecutive-restart counter. Measure the CPU cost of one `eval` every 2 s against NFR4 and record the number here.
  _Tests:_ the subprocess is mocked; covers session-missing → open, eval JSON error, timeout, counter reset after success, exhaustion. [R1.1, R1.5, NFR4]
- [ ] **T7. reader.js**: state detection in order (LOGIN, then NO_CHAT including empty conversations, then READING), conversation key from the qid suffix, `senders` map, message parsing, `at_bottom`, all driven by `selectors.json`.
  _Tests:_ each T2b fixture → expected JSON (headless Playwright), including an unread-count title. [R1.2, R1.3, R2.1, R2.6–R2.8, R2.13]
- [ ] **T8. messages_repo**: conversations (`ensure_conversation` sets the watermark once), messages (idempotent insert, `exists`, `unchecked_messages`, `mark_checked`, `unmark_checked`, tombstone `purge`), senders (upsert, lookup, purge).
  _Tests:_ duplicate insert ignored; watermark not overwritten; purge leaves tombstones; re-inserting a purged key is a no-op; unchecked messages are not purged; sender lookup after purge. [R2.3, R2.7, R2.11, R6.3]
- [ ] **T9. ZaloWeb poll + ingest**: state machine and events (`LoginRequired`, `NoChatOpen`, `ZaloReady`, `BrowserDied`, `SelectorDrift`), the single `ingest` path (chronological, stored senders, watermark), `mark_checked`, `recover()`.
  _Tests (FakeDriver + FakeMediator):_ the same page polled 3× → stored once, at most one event; a new bubble → 1 insert and 1 event; LOGIN/NO_CHAT → their event and no inserts; pre-watermark bubbles stored as checked and not emitted; a bubble without a name header gets the stored name; `recover()` re-emits only unchecked messages. [R1.2–R1.5, R2.1–R2.3, R2.7–R2.10, R3.5]
- [ ] **T10. Backfill, conversation switch, catch-up**: backfill through `ingest` with the four stop conditions of design §4.4, `--since` on first sight, catch-up when the list returns to the bottom with a gap, and snapshot cleanup.
  _Tests:_ each stop condition, including "oldest rendered bubble stored" not firing on the first screen; a first open without `--since` scrolls at most one screen; a switch gets its own watermark; catch-up runs with a gap and not without one; a restart backfills on the first READING poll. [R1.4, R2.4, R2.5, R2.11, R2.12, R6.3]

## Milestone 4 — Extraction and recording

- [ ] **T11. OrderRule + OrderExtractor**: `OrderRule` protocol, the stub rule (always `None`, R4.5), a `tests/` test rule, and the stateless extractor from design §5.
  _Tests (FakeMediator):_ self and media messages never reach the rule; a test rule hit → one `NewOrder` carrying the message copy; `MessagesChecked` lists every key and comes after the batch's `NewOrder`s. [R4.1–R4.5]
- [ ] **T12. orders_repo**: `store` (idempotent on `(conversation_key, message_key)`, `next_attempt_at = created_at`), `due(now, limit)`, `schedule_retry`, `mark_recorded`, `mark_failed`, `reset_failed`, `counts`, and purge of RECORDED content.
  _Tests:_ a fresh order is returned by `due()` immediately; a duplicate store is ignored; backed-off orders are not due early; `NEW`/`FAILED` content is not purged. [R4.3, R5.1, R5.6, R6.3]
- [ ] **T13. Sheet adapter + OrderRecorder**: header check/creation with the `served` checkbox, RAW `append_rows` batches, column-A read (string compare), `recover()` reconciliation of `NEW` and `FAILED`, re-read before retries, off-loop single in-flight job with client timeout, jittered backoff, `FAILED` at the cap, `RecorderBlocked`.
  _Tests (`InMemorySheet` with failure injection, frozen clock):_ every OrderRecorder case listed in design §11. [R5.1–R5.7, NFR1, NFR2]

## Milestone 5 — Orchestration and run

- [ ] **T14. Orchestrator**: event routing per design §1, startup `recover()` of each colleague, the tick (`poll()`, `kick()`, daily purge), and shutdown that waits up to `SHEETS_TIMEOUT_SECONDS` for the in-flight job.
  _Tests (fake colleagues):_ every event is routed; `BrowserDied(will_retry=False)` and `RecorderBlocked` exit non-zero; a recorder job that never completes does not delay `poll()`; an order stored before a simulated crash is recorded after restart. [R3.1–R3.5, R6.3, NFR1]
- [ ] **T15. CLI: `start`, `status`, `reprocess`**: config loading, `--since`, the status line on stdout, `data/app.lock`, and both `reprocess` forms.
  _Tests:_ `--since` parsing; status line contents; `reprocess` refuses while locked; `reprocess --order` resets a FAILED order; `reprocess --conversation` unmarks only live messages in range. [R1.1, R6.1, R6.2, R6.4, R7.1–R7.4]
- [ ] **T16. README + manual E2E**: log in, open a test group, send the example message from requirements §1, and verify the sheet row; tick `served` and confirm the app leaves it alone; confirm that a first open of a group with history creates no rows; unplug the network for a minute and confirm reading continues and the row lands afterwards. Record the results here.
  _Blocked on Q-rule:_ the stub rule produces no orders. Until the rule is specified, run the E2E with the T11 test rule. [NFR1, NFR2]

## Traceability

| Req | Tasks |
|---|---|
| R1 — Browser session & login | T1, T6, T7, T9, T10, T15 |
| R2 — Reading messages | T2, T2b, T7, T8, T9, T10 |
| R3 — Mediator architecture | T3, T5, T9, T14 |
| R4 — Order extraction | T11, T12 |
| R5 — Google Sheets output | T12, T13 |
| R6 — Operations, privacy | T1, T4, T8, T10, T12, T14, T15 |
| R7 — Maintenance commands | T15 |
| NFR1 — Read latency, incl. Sheets down | T13, T14, T16 |
| NFR2 — Order → row latency | T13, T16 |
| NFR3 — Unit coverage ≥ 80 % | T1 (threshold), T3–T15 |
| NFR4 — Idle CPU | T6 |
