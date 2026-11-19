"""SQLite connection and forward-only migration runner (design §7, §7.2).

* `connect(path)` opens the single database file in WAL mode with foreign keys on, `sqlite3.Row` rows and
  **autocommit** (`isolation_level=None`): each statement commits on its own. Wrap multi-statement units in
  `with transaction(conn): ...`.
* `migrate(conn)` applies the packaged `migrations/NNNN_name.sql` files whose number is greater than
  `PRAGMA user_version`, in order, each in its own transaction together with the `user_version` bump. A failing
  file rolls back completely and raises `MigrationError`; earlier files stay applied. Applied files are never
  re-run, so re-running `migrate` is a no-op.
* `open_database(path)` = `connect` + `migrate`; the one call the composition root needs.

All time columns are INTEGER epoch milliseconds (see the header of 0001_init.sql).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from order_extractor.telemetry import get_logger

MIGRATIONS_PACKAGE = "order_extractor.db.migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")

log = get_logger(__name__)


class MigrationError(RuntimeError):
    """A migration could not be applied, or the database is newer than this code."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating parent dirs) the SQLite file at `path`; `":memory:"` is accepted for tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` … `COMMIT`, or `ROLLBACK` if the block raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_migrations(source: Path | Traversable | None = None) -> list[Migration]:
    """Read `NNNN_name.sql` files from `source` (default: the packaged migrations), sorted by number."""
    root = source if source is not None else resources.files(MIGRATIONS_PACKAGE)
    found: list[Migration] = []
    for entry in root.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions) or 0 in versions:
        raise MigrationError(f"migration numbers must be unique and start at 0001: {versions}")
    return found


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, source: Path | Traversable | None = None) -> int:
    """Apply pending migrations; returns the resulting `user_version`."""
    migrations = load_migrations(source)
    current = user_version(conn)
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        raise MigrationError(f"database user_version {current} is newer than the code's latest migration {latest}")
    for m in migrations:
        if m.version <= current:
            continue
        try:
            # executescript commits any open transaction first, so BEGIN/COMMIT go inside the script.
            conn.executescript(f"BEGIN IMMEDIATE;\n{m.sql}\n;PRAGMA user_version = {m.version};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.error("migration_failed", extra={"migration": m.name, "error": str(exc)})
            raise MigrationError(f"migration {m.name} failed: {exc}") from exc
        log.info("migration_applied", extra={"migration": m.name, "user_version": m.version})
        current = m.version
    return current


def open_database(path: str | Path) -> sqlite3.Connection:
    """Connect and bring the schema up to date."""
    conn = connect(path)
    migrate(conn)
    return conn
