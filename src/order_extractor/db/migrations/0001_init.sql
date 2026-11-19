-- 0001_init: initial schema (design §7).
--
-- Conventions:
--   * Every time column (first_seen, start_ms, sent_at, observed_at, purged_at, updated_at, created_at,
--     next_attempt_at) is INTEGER epoch milliseconds, UTC. data-qid already gives ms, comparisons are plain
--     integer compares, and there is no text-format/timezone ambiguity. Convert to TZ_DISPLAY only for output.
--   * Booleans are INTEGER 0/1 with a CHECK.
--   * Ownership: conversations, messages, senders -> zalo/messages_repo.py; orders -> recorder/orders_repo.py.
--     No foreign key crosses an ownership boundary; orders reference their message by value.
--   * Forward-only: never edit this file once released; add NNNN_name.sql instead. The runner wraps it in a
--     transaction and sets PRAGMA user_version, so do not put BEGIN/COMMIT here.

CREATE TABLE conversations (
    key         TEXT    PRIMARY KEY,                       -- data-qid suffix, e.g. g1366548983169703056
    title       TEXT,                                      -- display only
    is_group    INTEGER NOT NULL DEFAULT 0 CHECK (is_group IN (0, 1)),
    first_seen  INTEGER NOT NULL,
    start_ms    INTEGER NOT NULL                           -- start watermark, set once [R2.11]
);

CREATE TABLE messages (
    id                INTEGER PRIMARY KEY,
    conversation_key  TEXT    NOT NULL REFERENCES conversations (key),
    message_key       TEXT    NOT NULL,                    -- <sentAtMs>_<senderUid> [R2.3]
    sender_uid        TEXT    NOT NULL,
    sender            TEXT,                                -- NULL after purge
    is_self           INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
    kind              TEXT    NOT NULL,
    text              TEXT,                                -- NULL after purge
    sent_at           INTEGER NOT NULL,
    observed_at       INTEGER NOT NULL,
    extracted         INTEGER NOT NULL DEFAULT 0 CHECK (extracted IN (0, 1)),
    purged_at         INTEGER,                             -- NULL = live, else tombstone [R6.3]
    UNIQUE (conversation_key, message_key)
);

CREATE INDEX messages_unchecked ON messages (extracted, sent_at);
CREATE INDEX messages_conversation_sent ON messages (conversation_key, sent_at);

CREATE TABLE senders (
    uid         TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT, -- AUTOINCREMENT: an order_id written to the sheet is never reused
    conversation_key    TEXT    NOT NULL,
    message_key         TEXT    NOT NULL,
    number              INTEGER NOT NULL,
    sent_at             INTEGER NOT NULL,
    conversation_title  TEXT,                              -- NULL after purge
    sender              TEXT,                              -- NULL after purge
    text                TEXT,                              -- NULL after purge
    status              TEXT    NOT NULL DEFAULT 'NEW' CHECK (status IN ('NEW', 'RECORDED', 'FAILED')),
    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at          INTEGER NOT NULL,
    next_attempt_at     INTEGER NOT NULL,                  -- = created_at on insert (due at once) [R5.1]
    sheet_row           INTEGER,
    error               TEXT,
    UNIQUE (conversation_key, message_key)
);

CREATE INDEX orders_due ON orders (status, next_attempt_at);
