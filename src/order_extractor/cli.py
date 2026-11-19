"""Command-line entrypoint and composition root (design §3, §10; R1.1, R6.4, R7).

* `start [--since TIME]` — load settings, take `data/app.lock`, open the database (migrations), wire the
  Orchestrator and its colleagues, and run until Ctrl-C / SIGTERM or a fatal event.
* `status` — order counts per status and the oldest NEW/FAILED order (read-only, no lock).
* `reprocess --order ID` | `reprocess --conversation KEY --since TIME` — queue work for the next `start`; refuses
  while `start` holds the lock.

Every command runs inside a tick (`telemetry.tick()`), so lines logged before the Orchestrator's own ticks (e.g.
`migration_applied` from `open_database`) still carry a `tick_id` [R6.5].

As the composition root this module may call each repository directly, but never mixes two repositories in one
operation. Exit codes: 0 ok, 1 failure (unexpected error, nothing to reprocess), 2 usage or configuration error,
3 browser could not be relaunched, 4 recorder blocked (sheet header mismatch), 5 lock held.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from order_extractor.config import ConfigError, Settings
from order_extractor.db.connection import open_database
from order_extractor.extractor.order_extractor import OrderExtractor
from order_extractor.extractor.rules import OrderRule, default_rule
from order_extractor.lock import AppLock, LockHeld, lock_path_for
from order_extractor.orchestrator import EXIT_ERROR, Orchestrator, epoch_ms
from order_extractor.recorder.order_recorder import OrderRecorder
from order_extractor.recorder.orders_repo import STATUSES, OrdersRepo
from order_extractor.recorder.sheet import GoogleSheet, Sheet
from order_extractor.telemetry import configure_logging, get_logger, tick
from order_extractor.timeparse import parse_since
from order_extractor.zalo.browser import BrowserDriver, Driver
from order_extractor.zalo.messages_repo import MessagesRepo
from order_extractor.zalo.reader import DEFAULT_SELECTORS_PATH, SelectorsError, build_reader_script, load_selectors
from order_extractor.zalo.zalo_web import ZaloWeb

log = get_logger(__name__)

EXIT_FAILED = EXIT_ERROR
EXIT_CONFIG = 2
EXIT_LOCKED = 5

SELECTORS_PATH: Path = DEFAULT_SELECTORS_PATH
TIME_FORMAT = "%Y-%m-%d %H:%M"

app = typer.Typer(
    name="order-extractor",
    help="Read orders from the open Zalo Web chat and append them to Google Sheets.",
    no_args_is_help=True,
    add_completion=False,
)


# --- helpers ------------------------------------------------------------------------------------------------


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


def _settings() -> Settings:
    try:
        return Settings.load()
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_CONFIG) from exc


def _since_ms(value: str, settings: Settings) -> int:
    try:
        return parse_since(value, settings.tz)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since") from exc


def _locked_message(settings: Settings) -> str:
    return f"order-extractor start is running ({lock_path_for(settings.db_path)} is locked); stop it first"


def _format_ms(value_ms: int, settings: Settings) -> str:
    return datetime.fromtimestamp(value_ms / 1000, settings.tz).strftime(TIME_FORMAT)


# --- composition (module-level so tests can swap the external boundaries) -----------------------------------


def build_driver(settings: Settings, selectors: dict[str, str]) -> Driver:
    return BrowserDriver(settings, scroller_selector=selectors["scroller"])


def build_sheet(settings: Settings) -> Sheet:
    return GoogleSheet.from_settings(settings)


def build_rule() -> OrderRule:
    return default_rule()


def build_orchestrator(
    settings: Settings, conn: sqlite3.Connection, selectors: dict[str, str], since_ms: int | None
) -> Orchestrator:
    orchestrator = Orchestrator(settings)
    zalo = ZaloWeb(
        orchestrator,
        build_driver(settings, selectors),
        MessagesRepo(conn),
        settings,
        reader_script=build_reader_script(selectors),
        since_ms=since_ms,
    )
    extractor = OrderExtractor(orchestrator, build_rule())
    recorder = OrderRecorder.from_settings(orchestrator, OrdersRepo(conn), build_sheet(settings), settings)
    orchestrator.attach(zalo, extractor, recorder)
    return orchestrator


# --- commands -----------------------------------------------------------------------------------------------


@app.command()
def start(
    since: Annotated[
        str | None,
        typer.Option(
            help="Start watermark for conversations first seen in this run: YYYY-MM-DD, 'YYYY-MM-DD HH:MM' "
            "(TZ_DISPLAY) or ISO 8601."
        ),
    ] = None,
) -> None:
    """Open Zalo Web and keep reading the open conversation."""
    settings = _settings()
    since_ms = _since_ms(since, settings) if since is not None else None
    if not settings.sheet_id:
        raise _fail("SHEET_ID is not set: put the Google Sheet id in .env", EXIT_CONFIG)
    if not settings.google_service_account_file.is_file():
        raise _fail(f"GOOGLE_SERVICE_ACCOUNT_FILE {settings.google_service_account_file} does not exist", EXIT_CONFIG)
    try:
        selectors = load_selectors(SELECTORS_PATH)
    except SelectorsError as exc:
        raise _fail(str(exc), EXIT_CONFIG) from exc
    configure_logging(settings.log_dir, settings.retention_days)
    try:
        with tick(), AppLock(lock_path_for(settings.db_path)):
            conn = open_database(settings.db_path)
            try:
                orchestrator = build_orchestrator(settings, conn, selectors, since_ms)
                code = asyncio.run(orchestrator.run())
            finally:
                conn.close()
    except LockHeld as exc:
        raise _fail(_locked_message(settings), EXIT_LOCKED) from exc
    if code:
        typer.echo(f"order-extractor stopped with exit code {code}; see {settings.log_dir}", err=True)
    raise typer.Exit(code=code)


@app.command()
def status() -> None:
    """Print order counts per status and the oldest NEW/FAILED order."""
    settings = _settings()
    if not settings.db_path.exists():
        typer.echo(f"No database yet at {settings.db_path}; run `order-extractor start` first.")
        return
    with tick():
        conn = open_database(settings.db_path)
        try:
            repo = OrdersRepo(conn)
            counts = repo.counts()
            oldest = repo.oldest_pending()
        finally:
            conn.close()
    typer.echo("orders by status:")
    for name in STATUSES:
        typer.echo(f"  {name:<9}{counts[name]:>6}")
    if oldest is None:
        typer.echo("oldest NEW/FAILED: none")
        return
    line = (
        f"oldest NEW/FAILED: #{oldest.id} {oldest.status}, created {_format_ms(oldest.created_at_ms, settings)},"
        f" attempts {oldest.attempts}"
    )
    if oldest.error:
        line += f", last error: {oldest.error}"
    typer.echo(line)


@app.command()
def reprocess(
    order: Annotated[int | None, typer.Option(help="Reset a FAILED order to NEW.")] = None,
    conversation: Annotated[str | None, typer.Option(help="Conversation key whose messages are re-checked.")] = None,
    since: Annotated[
        str | None, typer.Option(help="With --conversation: re-check messages sent at or after this time.")
    ] = None,
) -> None:
    """Queue a FAILED order or a range of messages for another pass on the next start."""
    if (order is None) == (conversation is None):
        raise typer.BadParameter("give exactly one of --order or --conversation")
    if conversation is not None and since is None:
        raise typer.BadParameter("--conversation needs --since", param_hint="--since")
    if order is not None and since is not None:
        raise typer.BadParameter("--since only applies to --conversation", param_hint="--since")
    settings = _settings()
    since_ms = _since_ms(since, settings) if since is not None else None
    configure_logging(settings.log_dir, settings.retention_days)  # an audit trail of manual changes
    try:
        with tick(), AppLock(lock_path_for(settings.db_path)):
            conn = open_database(settings.db_path)
            try:
                if order is not None:
                    _reprocess_order(conn, order)
                else:
                    assert conversation is not None and since_ms is not None
                    _reprocess_conversation(conn, conversation, since_ms, settings)
            finally:
                conn.close()
    except LockHeld as exc:
        raise _fail(_locked_message(settings), EXIT_LOCKED) from exc


def _reprocess_order(conn: sqlite3.Connection, order_id: int) -> None:
    repo = OrdersRepo(conn)
    current = repo.get(order_id)
    if current is None:
        raise _fail(f"order {order_id} not found", EXIT_FAILED)
    if not repo.reset_failed(order_id, epoch_ms()):
        raise _fail(f"order {order_id} is {current.status}; only FAILED orders can be reprocessed", EXIT_FAILED)
    log.info("order_reprocess", extra={"order_id": order_id})
    typer.echo(f"order {order_id} reset to NEW; it is recorded on the next start (column A is checked first)")


def _reprocess_conversation(conn: sqlite3.Connection, key: str, since_ms: int, settings: Settings) -> None:
    repo = MessagesRepo(conn)
    if repo.get_conversation(key) is None:
        raise _fail(f"conversation {key} not found", EXIT_FAILED)
    count = repo.unmark_checked(key, since_ms)
    log.info("conversation_reprocess", extra={"conversation_key": key, "since": since_ms, "count": count})
    typer.echo(
        f"{count} message(s) of {key} sent since {_format_ms(since_ms, settings)} will be re-checked on the next start"
    )
