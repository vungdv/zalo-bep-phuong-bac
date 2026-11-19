# Requirements — Zalo Order Extractor

> Phase 1 of 3 (Requirements → Design → Tasks). Source idea: [../extract-order-spec.md](../extract-order-spec.md)
> **Rev 2 (2026-11-19):** messages now come from Zalo Web (`chat.zalo.me`) driven by `playwright-cli`, not the OA webhook.
> **Rev 3 (2026-11-19):** components communicate through a mediator (Orchestrator). LLM extraction and per-sender
> sessions are removed; an order is a single number taken from one message by a rule to be specified later.
> The Google Sheet gets a `served` checkbox that only the human ticks.
> **Rev 4 (2026-11-19):** retention purge keeps message keys as tombstones (R6.3); a per-conversation start
> watermark keeps old history out of the order rule (R2.11); the recorder re-checks column A to avoid duplicate rows
> (R5.5); and Sheets retries are scheduled across ticks, so they never block reading (R5.6).
> **Rev 5 (2026-11-19):** Sheets calls run off the poll loop (R5.6); new orders are due immediately (R5.1); startup
> reconciliation also covers `FAILED` (R5.5); stored sender names (R2.7); catch-up after scrolling (R2.12); conversation
> key from `data-qid` (R2.13); relaunch-then-exit (R1.5); sheet header check (R5.3); `reprocess` and `status` (R7);
> structured logs (R6.5); known limitations (§6). Each colleague owns its storage; orders carry a copy of their
> message, and the extractor reports `MessagesChecked` back through the mediator (R3.4, R4.3, R4.4, R6.3).

## 1. Overview

A local tool that reads chat messages from **Zalo Web (https://chat.zalo.me)** in a browser controlled by
**`playwright-cli`**. A human logs in and opens a chat (private or group). The tool then reads the messages of that
conversation **one by one**, applies an **order rule** to detect orders (each order is a single number), and
appends each order to a **Google Sheet** together with its message. Staff tick a **served** checkbox in the
sheet once the order has been delivered.

### Operator flow

1. Run `order-extractor start`. A visible (headed) Chromium window opens `chat.zalo.me` with a saved profile.
2. **Human** scans the QR code to log in (only needed when the saved session has expired).
3. **Human** opens the group/chat to monitor.
4. The tool detects the open conversation, reads existing messages (backfill), then keeps reading new ones as they arrive.
5. Orders appear in the Google Sheet with `served` unticked.
6. **Human** ticks `served` in the sheet when the order is delivered.

### Example

Message from group member "Lan":

```
Lan: Shop ơi cho em 2 áo thun đen size M
```

Expected sheet row, assuming the (future) rule reads `2` from this message:

| order_id | time | conversation | sender | messages | order | served |
|---|---|---|---|---|---|---|
| 42 | 2026-11-19 10:38 | Nhóm khách sỉ | Lan | Shop ơi cho em 2 áo thun đen size M | 2 | ☐ |

## 2. Scope

**In scope (MVP)**
- Drive a headed browser session at `chat.zalo.me` with `playwright-cli`, using a persistent profile.
- Human-in-the-loop login (QR) and conversation selection; no credential handling by the tool.
- Read text messages of the **currently open** conversation (group or 1-1): backfill, then live.
- Detect orders with a pluggable rule (one message → at most one order, value = one number).
- Append orders to Google Sheets with a human-owned `served` checkbox.

**Out of scope (MVP)**
- Automatic login, automatic switching between conversations, or watching several conversations at once.
- Sending/replying to messages. The tool is **read-only** and must never type into the chat.
- Images, stickers, voice, files (stored as placeholders, never turned into orders).
- LLM-based extraction; parsing customer name, phone, address, or item details.
- Orders spanning several messages; amendments/cancellations; admin UI; product catalog.
- Reading the `served` status back from the sheet.

## 3. Glossary

| Term | Meaning |
|---|---|
| Browser session | The `playwright-cli -s=zalo` browser; profile kept in `./data/browser-profile` |
| Conversation | The chat currently open in Zalo Web. Its **key** is the suffix of any bubble's `data-qid` (e.g. `g1366…` for a group); the title is display-only and the URL never changes (R2.13) |
| Message | One rendered chat bubble: key, sender uid, sender name, text, send time, is_self, kind |
| Sender | A Zalo uid with its last seen display name, stored so names survive scrolling (R2.7) |
| Order | A number extracted from one customer message by the order rule |
| Order rule | The function `extract(message) -> number or none`; its logic is to be specified |
| Start watermark | Per-conversation time; messages sent before it are stored but never checked by the order rule |
| Tombstone | A purged message row: key and ids kept for de-duplication, text and sender name removed |
| Mediator / Orchestrator | The component that receives every event and decides which component handles it next |
| Selectors | CSS/JS used to read the Zalo Web DOM, kept in `selectors.json` (not hard-coded) |

## 4. Requirements (EARS format)

### R1 — Browser session & login
**User story:** As the operator, I log in myself, and the tool reuses my login afterwards.

1. WHEN `order-extractor start` runs THE SYSTEM SHALL open (or attach to) the `playwright-cli` session `zalo`. The browser SHALL be headed, use the persistent profile, and be at `https://chat.zalo.me`.
2. WHILE the login (QR) screen is shown THE SYSTEM SHALL print "Waiting for login: scan the QR code in the browser" and poll without reading messages. Login detection SHALL be checked before the logged-in check and SHALL tolerate an unread-count prefix in the title (e.g. `(3) Zalo - …`).
3. WHILE logged in but no conversation is open, or the open conversation has no rendered messages yet, THE SYSTEM SHALL print "Open a chat to monitor" and wait.
4. THE SYSTEM SHALL NOT click, type, or send anything inside the Zalo UI, except scrolling the message list during backfill and catch-up (R2.4, R2.12).
5. IF the browser is closed or the session dies THE SYSTEM SHALL stop reading, log an error, and relaunch the session. IF `BROWSER_MAX_RESTARTS` (default 3) consecutive relaunches fail THE SYSTEM SHALL exit non-zero. The counter SHALL reset after a successful read.
6. The browser profile directory SHALL be git-ignored and treated as a secret (it holds the Zalo login).

### R2 — Reading messages one by one
**User story:** As the shop owner, I want every message in the open group read exactly once.

1. WHILE a conversation is open THE SYSTEM SHALL poll the DOM every `READ_INTERVAL_SECONDS` (default 2) and extract all rendered messages as `{message_key, sent_at_ms, sender_uid, sender, is_self, kind, text}`.
2. THE SYSTEM SHALL process new messages **one at a time, in chronological order**, persisting each before handling the next.
3. THE SYSTEM SHALL de-duplicate on `(conversation_key, message_key)`, where `message_key = <sentAtMs>_<senderUid>` parsed from the bubble's `data-qid` (see [notes/zalo-dom.md](notes/zalo-dom.md)). Only if no qid is present does it fall back to a hash of sender + text + ordinal. The stored message row (live or tombstone, R6.3) is the de-duplication record and SHALL outlive the retention purge.
4. THE SYSTEM SHALL **backfill** by scrolling the message list up and re-reading until one of these holds: the oldest rendered bubble is already stored; the oldest rendered bubble was sent before the start watermark (R2.11); `BACKFILL_MAX_MESSAGES` (default 200) messages were gathered; or the start of the chat is reached. It SHALL then return to the bottom. Backfill SHALL run when a conversation is first opened in a run and on catch-up (R2.12).
5. WHEN the human switches to another conversation THE SYSTEM SHALL start reading the new one (backfill included).
6. WHEN the message is a non-text kind (image, sticker, file, voice) THE SYSTEM SHALL store a placeholder like `[image]` with no further processing.
7. Sender identity SHALL be the Zalo **uid** from `data-qid`. THE SYSTEM SHALL store the last seen display name per uid and use it for bubbles that render without a name header. IF no name is known yet the sender SHALL be stored as the uid and updated in later rows only (stored messages are not rewritten).
8. A message is **from the shop** WHEN `is_self` is true (uid `0`).
9. IF the reader finds 0 messages in an open conversation for `SELECTOR_ALERT_SECONDS` (default 60), or the selectors throw, THE SYSTEM SHALL log "selectors may be outdated" with a DOM snapshot path.
10. WHEN one or more new messages have been stored in a poll THE SYSTEM SHALL emit one `NewMessages` event containing them, oldest first. Messages before the start watermark (R2.11) SHALL NOT be included. Backfill and catch-up SHALL store messages through the same path, with the same watermark check.
11. WHEN a conversation is opened for the first time THE SYSTEM SHALL set its **start watermark** to the `--since` time given to `start`, or else to the moment it was first opened. `--since` SHALL apply to every conversation first seen during that run, including ones the human opens later. Messages with `sent_at` before the watermark SHALL be stored as already checked and SHALL NOT reach the order rule. The watermark SHALL NOT change after it is set.
12. WHEN the list returns to the bottom after being scrolled up, and the oldest rendered bubble is not already stored, THE SYSTEM SHALL run a catch-up backfill (R2.4) before continuing live reading.
13. The conversation key SHALL be taken from the `data-qid` suffix of the rendered bubbles, not from the URL or title.

### R3 — Mediator architecture
**User story:** As the developer, I want each part replaceable and testable on its own.

1. THE SYSTEM SHALL consist of an **Orchestrator** (mediator) and three colleagues: **ZaloWeb** (R1, R2), **OrderExtractor** (R4), and **OrderRecorder** (R5).
2. Colleagues SHALL NOT reference each other or each other's storage. Each SHALL communicate only by sending events to the Orchestrator, which routes them (design §1 event catalogue).
3. The Orchestrator SHALL open Zalo Web on start and drive the polling loop.
4. Each colleague SHALL own its storage and persist what it owns **before** emitting the event that announces it. A message SHALL be marked checked only after any order it produced has been stored.
5. WHEN the service restarts THE SYSTEM SHALL ask each colleague to recover its own unfinished work: ZaloWeb re-emits messages not yet checked by the rule, and OrderRecorder reconciles and records orders not yet `RECORDED`.
6. Each colleague SHALL be testable with a fake mediator that records emitted events.

### R4 — Order extraction
**User story:** As the shop owner, I want customer messages that contain an order turned into an order record.

1. WHEN OrderExtractor receives new messages THE SYSTEM SHALL apply the order rule to each customer text message, in order.
2. Shop messages (`is_self`) and non-text messages SHALL never produce an order.
3. WHEN the rule returns a number THE SYSTEM SHALL emit `NewOrder` carrying the number and a copy of the message fields the sheet needs. OrderRecorder SHALL store it, idempotent on `(conversation_key, message_key)`, so a message produces at most one order.
4. After a batch is handled THE SYSTEM SHALL emit `MessagesChecked` with the keys of every message in it, whether or not it produced an order.
5. The order rule SHALL be a replaceable component. **Its logic is to be specified (Q-rule).** Until then the shipped rule SHALL return nothing for every message.

### R5 — Google Sheets output
**User story:** As shop staff, I want every order in a sheet where I can mark it served.

1. A stored order SHALL be due for recording immediately. THE SYSTEM SHALL append one row per due order to `SHEET_ORDERS_TAB` of `SHEET_ID`.
2. Columns: `order_id, time, conversation, sender, messages, order, served`. `time` is the message send time in `TZ_DISPLAY`; `messages` is the message text; `order` is the number. Values SHALL be written as raw input, so text starting with `=` is never run as a formula.
3. On startup THE SYSTEM SHALL read the header row. IF it is empty THE SYSTEM SHALL create it and apply checkbox validation to the `served` column. IF it differs from the expected columns THE SYSTEM SHALL exit non-zero and name the mismatch, without writing anything.
4. New rows SHALL be written with `served` unticked. THE SYSTEM SHALL NOT write the `served` column after the row is appended; it is owned by the human.
5. Idempotent: `order_id` = the stored order id, and column A of the sheet is the record of what was written. `order_id`s SHALL be compared as trimmed strings. Retries SHALL never duplicate rows. To ensure this:
   - On startup THE SYSTEM SHALL read column A into the set of recorded `order_id`s. Any `NEW` or `FAILED` order found there SHALL be marked `RECORDED` without appending. This covers a crash between the append and the status update, and a last attempt whose response was lost.
   - Before every retry THE SYSTEM SHALL re-read column A. If the `order_id` is there, the earlier attempt succeeded (for example, a response lost to a timeout), and THE SYSTEM SHALL mark the order `RECORDED` instead of appending.
6. Sheets calls SHALL NOT block the poll loop: at most one call SHALL be in flight, it SHALL be bounded by `SHEETS_TIMEOUT_SECONDS` (default 10), and the loop SHALL NOT wait for it. IF a call fails THE SYSTEM SHALL increment `attempts` on the orders it carried, set `next_attempt_at` by exponential backoff with jitter (`RECORD_BACKOFF_BASE_SECONDS`, default 2, capped at `RECORD_BACKOFF_MAX_SECONDS`, default 300), and retry them once due. After `RECORD_MAX_ATTEMPTS` (default 5) attempts THE SYSTEM SHALL mark the order `FAILED` and log it.
7. WHEN a row is written THE SYSTEM SHALL emit `OrderRecorded`.

### R6 — Operations, privacy
1. Config via `.env`; no secrets in code or logs; message text not logged at INFO.
2. Message text leaves the machine only as the `messages` column of order rows written to the configured Google Sheet.
3. Message content SHALL be purged after `RETENTION_DAYS` (30), counted from `sent_at`. Purging turns the row into a **tombstone**: `text` and `sender` are set to NULL, while `conversation_key`, `message_key`, `sender_uid`, `sent_at`, and `extracted` are kept, so de-duplication (R2.3) still works when a backfill scrolls past the purge point. THE SYSTEM SHALL NOT purge a message that has not been checked by the rule. An order's copied content SHALL be purged on the same schedule, but only once the order is `RECORDED`. DOM snapshots (R2.9) older than the window SHALL be deleted. The purge SHALL run at startup and then once a day.
4. Console status line (stdout): login state, conversation, messages read, orders found, orders recorded, orders failed.
5. Logs SHALL be structured JSON lines written to `LOG_DIR` and to stderr (never stdout), each carrying a `tick_id` and, where relevant, `conversation_key`, `message_key`, and `order_id`.

### R7 — Maintenance commands
1. `order-extractor status` SHALL print, from the local database, counts per order status and the oldest `NEW`/`FAILED` order.
2. `order-extractor reprocess --order <id>` SHALL reset a `FAILED` order to `NEW` with `attempts = 0`, so it is recorded again (with the column-A check of R5.5).
3. `order-extractor reprocess --conversation <key> --since <time>` SHALL mark the stored, not purged messages of that conversation sent at or after `<time>` as not checked, so the order rule runs on them again. Messages that already have an order SHALL keep it (R4.3).
4. `reprocess` SHALL refuse to run while `start` is running (lock file in `data/`). The work it queues is picked up by recovery (R3.5) on the next `start`.

## 5. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR1 | New message → stored within `READ_INTERVAL_SECONDS + 1 s`, including while Google Sheets is down or slow. |
| NFR2 | p95 order found → sheet row ≤ 10 s when Google Sheets is reachable. |
| NFR3 | Unit coverage ≥ 80 % for the orchestrator, reader, extractor, and recorder. |
| NFR4 | Reader CPU < 5 % while idle (polling only, no heavy snapshots). |

## 6. Risks, assumptions, open questions

- **Risk (ToS):** automating Zalo Web is unofficial. The tool is read-only and polls slowly, but Zalo may still flag the account. Prefer a dedicated shop account.
- **Risk (DOM drift):** Zalo Web markup changes without notice. Selectors live in `selectors.json` and are re-discovered with `playwright-cli snapshot` (task T2).
- **Risk (virtualized list):** only rendered bubbles are readable. Messages that arrive while the human is scrolled up are recovered by catch-up (R2.12) once the list returns to the bottom, but only within `BACKFILL_MAX_MESSAGES`.
- **Risk (sheet edits):** if staff sort or delete rows, the row number stored for an order no longer matches. Idempotency relies on `order_id` in column A, not on row numbers. Inserting or reordering columns is caught by the header check (R5.3).
- **A1:** Zalo Web allows one web session per account. Logging in here may log out another Zalo Web tab (desktop/mobile are unaffected).
- ~~A2~~ **Resolved (T2):** `data-qid` carries the exact send time (ms) and sender uid, so no time-label parsing is needed.
- **A3:** The 1-1 chat `data-qid` suffix format is still unverified (only a group was inspected). Verify in T2b.
- **A4:** Only the logged-in account counts as "shop". Other staff accounts in the group are treated as customers (`SHOP_SENDER_NAMES` removed in Rev 3).
- **A5:** `<sentAtMs>_<senderUid>` is unique within a conversation. Albums and multi-part sends may share a timestamp. Verify in T2b; if they collide, add the qid ref (unique for other users' messages) or the image index to the key.
- **Known limitation (recall/edit):** a recalled or edited message keeps its key, so the tool does not notice the change. An order that is already recorded stays in the sheet unchanged. Staff handle these by hand.
- **Q-rule:** how the order number is extracted from a message (to be provided by the shop owner).
- **Q-group:** whether an order can span several messages from the same sender. Assumed no for the MVP.
