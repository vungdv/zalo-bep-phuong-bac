# Zalo Order Extractor

Reads the messages of the chat that is open in **Zalo Web** (`chat.zalo.me`), applies an order rule, and appends
each order to a **Google Sheet**. Staff tick the `served` checkbox in the sheet when the order is delivered.

The tool is read-only: it never types into Zalo. You log in and pick the chat yourself.
Specs live in [specs/](specs/) ([requirements](specs/requirements.md), [design](specs/design.md), [tasks](specs/tasks.md)).

> **Order rule is a placeholder.** Until the rule is specified (Q-rule), the shipped rule returns no orders, so a
> normal run reads and stores messages but writes no sheet rows. See [Trying it without the real rule](#trying-it-without-the-real-rule).

## 1. Prerequisites

| Need | Check |
|---|---|
| macOS or Linux | |
| [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you) | `uv --version` |
| `playwright-cli` (the `@playwright/cli` npm package, 0.1.14 or newer) | `playwright-cli --version` |
| A Google account for the sheet | |
| A Zalo account (a dedicated shop account is safest, see [Risks](#risks)) | |

## 2. Install

```sh
uv sync
uv run order-extractor --help
```

Run every command from the repo root. `start` reads `./selectors.json` relative to the working directory.

## 3. Google Sheet setup (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Google Sheets API**.
2. Create a **service account** and download its JSON key. Save it as `secrets/sa.json` (the `secrets/` folder is git-ignored).
3. Create a Google Sheet with a tab named `Orders` (or choose another name in `.env`).
4. **Share the sheet** with the service account's email (the `client_email` in the JSON), as **Editor**.
5. Copy the sheet id from its URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

Leave row 1 empty. On first start the tool writes the header and adds the `served` checkbox column:

```
order_id | time | conversation | sender | messages | order | served
```

If row 1 already holds something different, the tool exits with code 4 and writes nothing.

## 4. Configure

```sh
cp .env.example .env
```

Edit `.env`. These two are required:

```
SHEET_ID=<your sheet id>
GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/sa.json
```

The rest have working defaults:

| Key | Default | Meaning |
|---|---|---|
| `READ_INTERVAL_SECONDS` | 2 | How often the chat is read |
| `BACKFILL_MAX_MESSAGES` | 200 | Most history read when a chat is first opened |
| `SHEETS_TIMEOUT_SECONDS` | 10 | Timeout of one Google call |
| `RECORD_MAX_ATTEMPTS` | 5 | Tries before an order is marked `FAILED` |
| `RETENTION_DAYS` | 30 | Message text is purged after this many days |
| `TZ_DISPLAY` | `Asia/Ho_Chi_Minh` | Time zone for the `time` column and for `--since` |
| `DB_PATH`, `LOG_DIR` | `./data/...` | Local database and logs |

`.env`, `secrets/`, and `data/` (which holds the browser profile with your Zalo login) are git-ignored.
Never commit them.

## 5. Run

```sh
uv run order-extractor start
```

1. A visible Chromium window opens `chat.zalo.me`. If you are not logged in, scan the QR code with your phone.
   The console shows `Waiting for login: scan the QR code in the browser`.
2. Open the chat group to monitor. The console shows `Open the chat group to monitor` until you do.
3. The tool reads the history (backfill), then keeps reading new messages. A status line on the console shows
   login state, conversation, and counts read / found / recorded / failed.
4. Stop with `Ctrl-C`. Anything unfinished is picked up on the next start.

Your login is kept in `data/browser-profile`, so the next run usually skips the QR code.

### Only check messages from a given time

Messages sent before the start time are stored but never checked for orders. By default that time is the moment a
chat is first opened, so old history creates no rows. To check older messages, pass `--since`:

```sh
uv run order-extractor start --since "2026-11-19 08:00"   # read in TZ_DISPLAY
uv run order-extractor start --since 2026-11-19            # or a date, or ISO 8601
```

`--since` applies to every chat first seen during that run, and never changes a chat that is already known.

## 6. Other commands

```sh
uv run order-extractor status                      # order counts per status + oldest NEW/FAILED order
uv run order-extractor reprocess --order 42        # FAILED order -> NEW, recorded again on next start
uv run order-extractor reprocess --conversation <key> --since "2026-11-19 08:00"
                                                   # run the order rule again on stored messages
```

`reprocess` refuses to run while `start` is running. The work it queues is done on the next `start`.
It never creates duplicate sheet rows: column A of the sheet is checked first.

## 7. Trying it without the real rule

The test rule in [tests/rules_for_tests.py](tests/rules_for_tests.py) treats the first number in a message as the
order (`FirstIntegerRule`). To try the full flow, temporarily make `build_rule` in
[src/order_extractor/cli.py](src/order_extractor/cli.py) return it instead of `default_rule()`, run `start`, and
send "cho em 2 áo" in a test group. Expect one row with order `2` and `served` unticked. Revert this before real use.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Exit code 2 | Config error: check `SHEET_ID`, the service-account path, and `--since` format. The message names the problem |
| Exit code 3 | The browser could not be relaunched `BROWSER_MAX_RESTARTS` times. Check `playwright-cli` works and nothing else uses the `zalo` session |
| Exit code 4 | Sheet header row does not match. Empty row 1 or fix the headers |
| Exit code 5 | Another `start` or `reprocess` holds `data/app.lock` |
| `selectors may be outdated` | Zalo changed its page. A snapshot is saved under `data/snapshots/`. Update [selectors.json](selectors.json) (see [specs/notes/zalo-dom.md](specs/notes/zalo-dom.md)) |
| Rows not appearing | Run `status`. `NEW` means retrying, `FAILED` means it gave up: check the sheet is shared with the service account, then `reprocess --order <id>` |
| Need more detail | Logs are JSON lines in `data/logs/app.jsonl` (message text appears only at DEBUG) |

## 9. Development

```sh
uv run pytest            # unit + acceptance tests, coverage floor 80%
uv run ruff check && uv run ruff format --check
```

The `reader.js` tests run in headless Chromium: `uv run playwright install chromium-headless-shell` once.
No test touches the network, Google, or your real Zalo session.

## Risks

- Automating Zalo Web is unofficial and may be flagged by Zalo. The tool only reads and polls slowly; prefer a dedicated shop account.
- Zalo Web allows one web session per account, so logging in here may log out another Zalo Web tab.
- Recalled or edited messages are not noticed after they are read; staff handle those in the sheet by hand.
