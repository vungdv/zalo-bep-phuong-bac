import sqlite3

import pytest

from order_extractor.db.connection import (
    MigrationError,
    connect,
    load_migrations,
    migrate,
    open_database,
    transaction,
    user_version,
)

# table -> [(column, declared type, not null, primary key position)]
EXPECTED_SCHEMA = {
    "conversations": [
        ("key", "TEXT", 0, 1),
        ("title", "TEXT", 0, 0),
        ("is_group", "INTEGER", 1, 0),
        ("first_seen", "INTEGER", 1, 0),
        ("start_ms", "INTEGER", 1, 0),
    ],
    "messages": [
        ("id", "INTEGER", 0, 1),
        ("conversation_key", "TEXT", 1, 0),
        ("message_key", "TEXT", 1, 0),
        ("sender_uid", "TEXT", 1, 0),
        ("sender", "TEXT", 0, 0),
        ("is_self", "INTEGER", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("text", "TEXT", 0, 0),
        ("sent_at", "INTEGER", 1, 0),
        ("observed_at", "INTEGER", 1, 0),
        ("extracted", "INTEGER", 1, 0),
        ("purged_at", "INTEGER", 0, 0),
    ],
    "senders": [
        ("uid", "TEXT", 0, 1),
        ("name", "TEXT", 1, 0),
        ("updated_at", "INTEGER", 1, 0),
    ],
    "orders": [
        ("id", "INTEGER", 0, 1),
        ("conversation_key", "TEXT", 1, 0),
        ("message_key", "TEXT", 1, 0),
        ("number", "INTEGER", 1, 0),
        ("sent_at", "INTEGER", 1, 0),
        ("conversation_title", "TEXT", 0, 0),
        ("sender", "TEXT", 0, 0),
        ("text", "TEXT", 0, 0),
        ("status", "TEXT", 1, 0),
        ("attempts", "INTEGER", 1, 0),
        ("created_at", "INTEGER", 1, 0),
        ("next_attempt_at", "INTEGER", 1, 0),
        ("sheet_row", "INTEGER", 0, 0),
        ("error", "TEXT", 0, 0),
    ],
}


def schema(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {t: [(c[1], c[2], c[3], c[5]) for c in conn.execute(f"PRAGMA table_info({t})")] for t in tables}


def unique_sets(conn, table):
    result = set()
    for idx in conn.execute(f"PRAGMA index_list({table})"):
        if idx["unique"] and idx["origin"] == "u":
            result.add(tuple(r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})")))
    return result


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "sub" / "app.db")
    yield conn
    conn.close()


def test_empty_db_migrates_to_expected_schema(db):
    assert schema(db) == EXPECTED_SCHEMA
    assert user_version(db) == load_migrations()[-1].version == 1
    assert unique_sets(db, "messages") == {("conversation_key", "message_key")}
    assert unique_sets(db, "orders") == {("conversation_key", "message_key")}


def test_connection_settings(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert isinstance(db.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)


def test_rerunning_migrate_is_a_noop(db):
    before = schema(db)
    assert migrate(db) == 1
    assert schema(db) == before


def test_constraints(db):
    db.execute("INSERT INTO conversations (key, first_seen, start_ms) VALUES ('g1', 1, 1)")
    insert_msg = (
        "INSERT INTO messages (conversation_key, message_key, sender_uid, kind, sent_at, observed_at)"
        " VALUES (?, '1_2', '2', 'text', 1, 1)"
    )
    db.execute(insert_msg, ("g1",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert_msg, ("g1",))  # UNIQUE(conversation_key, message_key)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert_msg, ("unknown",))  # FK to conversations
    insert_order = (
        "INSERT INTO orders (conversation_key, message_key, number, sent_at, created_at, next_attempt_at, status)"
        " VALUES ('g1', ?, 2, 1, 5, 5, ?)"
    )
    db.execute(insert_order, ("1_2", "NEW"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert_order, ("1_2", "NEW"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert_order, ("3_4", "DONE"))
    row = db.execute("SELECT status, attempts, next_attempt_at FROM orders").fetchone()
    assert tuple(row) == ("NEW", 0, 5)


def test_order_ids_are_never_reused(db):
    insert = (
        "INSERT INTO orders (conversation_key, message_key, number, sent_at, created_at, next_attempt_at)"
        " VALUES ('g1', ?, 1, 1, 1, 1)"
    )
    db.execute(insert, ("a",))
    db.execute("DELETE FROM orders")
    assert db.execute(insert, ("b",)).lastrowid == 2


def test_failing_migration_rolls_back(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (migrations / "0002_bad.sql").write_text("CREATE TABLE b (x INTEGER);\nINSERT INTO missing VALUES (1);")
    (migrations / "notes.txt").write_text("ignored")
    conn = connect(tmp_path / "app.db")
    with pytest.raises(MigrationError, match=r"0002_bad\.sql"):
        migrate(conn, migrations)
    assert user_version(conn) == 1
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {"a"}
    assert not conn.in_transaction

    (migrations / "0002_bad.sql").write_text("CREATE TABLE b (x INTEGER);")
    assert migrate(conn, migrations) == 2  # fixed file applies on the next run


def test_database_newer_than_code_is_refused(tmp_path):
    conn = connect(":memory:")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(MigrationError, match="newer"):
        migrate(conn)


def test_duplicate_migration_numbers_are_refused(tmp_path):
    (tmp_path / "0001_a.sql").write_text("")
    (tmp_path / "0001_b.sql").write_text("")
    with pytest.raises(MigrationError, match="unique"):
        load_migrations(tmp_path)


def test_transaction_commits_or_rolls_back(db):
    with transaction(db):
        db.execute("INSERT INTO senders (uid, name, updated_at) VALUES ('1', 'Lan', 1)")
    with pytest.raises(RuntimeError), transaction(db):
        db.execute("INSERT INTO senders (uid, name, updated_at) VALUES ('2', 'Mai', 1)")
        raise RuntimeError("boom")
    assert [r[0] for r in db.execute("SELECT uid FROM senders")] == ["1"]
