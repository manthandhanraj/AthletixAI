# -*- coding: utf-8 -*-
"""Database boundary: schema, connections, migrations.

Owns *how* to reach the database and *what shape* the schema is. It does not
know about HTTP, sessions or business rules, and it performs no work at
import - `init_db()` must be called explicitly (create_app does this).

Portability rule (Phase 2.9)
----------------------------
Everything engine-specific lives in this module and in
`athletix/repositories/`. That means: the `?` parameter style, `PRAGMA`,
`AUTOINCREMENT`, `COLLATE NOCASE`, `PRAGMA table_info` introspection,
`ON CONFLICT ... DO UPDATE`, and the decision to store timestamps as
sortable ISO-8601 text. Routes, services, schemas and serializers contain no
SQL at all, so a PostgreSQL implementation is a change to two directories
rather than to the whole application.

`POSTGRESQL_READINESS.md` is the audit: schema mapping, the statements that
would have to change, transaction and concurrency differences, and the
migration and rollback sequence. Nothing here migrates any data.
"""

import datetime
import sqlite3

from flask import g

from athletix.config import DB_PATH, ensure_db_dir


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT    NOT NULL CHECK (role IN ('athlete','coach','admin','owner')),
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    phone         TEXT,
    pass_hash     TEXT    NOT NULL,
    photo         TEXT,                       -- base64 data URL (optional)
    verified      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    last_login    TEXT,
    -- Bumped whenever every existing session for this user must stop being
    -- valid: password reset, password change, e-mail change, role change,
    -- explicit "log out everywhere". Cookies carry the epoch they were
    -- minted with; current_user() rejects any cookie that is behind.
    sess_epoch    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS athlete_profiles (
    user_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    sport     TEXT,
    age       INTEGER,
    location  TEXT DEFAULT 'Urban'
);

CREATE TABLE IF NOT EXISTS coach_profiles (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    specialty    TEXT DEFAULT '',
    bio          TEXT DEFAULT '',
    experience   INTEGER DEFAULT 0,
    city         TEXT DEFAULT '',
    achievements TEXT DEFAULT '[]'            -- JSON array of strings
);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date       TEXT    NOT NULL,
    sport      TEXT,
    speed      INTEGER, agility INTEGER, strength INTEGER,
    stamina    INTEGER, technique INTEGER,
    overall    REAL,
    live       INTEGER DEFAULT 0,
    -- Phase 2.8 provenance: which provider produced this score, at which
    -- contract version, and whether the SERVER vouches for it. Scores
    -- computed in the browser are stored with scoring_trusted = 0; the
    -- column exists so a future server-verified score is distinguishable
    -- from today's, instead of both looking identical in the table.
    scoring_provider   TEXT,
    scoring_version    TEXT,
    scoring_trusted    INTEGER NOT NULL DEFAULT 0,
    scoring_confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_reports_athlete
    ON reports(athlete_id, date);

CREATE TABLE IF NOT EXISTS ai_results (
    report_id  INTEGER PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
    potential  TEXT, medal_prob INTEGER, risk TEXT, best_fit TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER REFERENCES reports(id) ON DELETE CASCADE,
    athlete_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename   TEXT, size_mb REAL, source TEXT, uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    to_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    from_nm TEXT NOT NULL,
    body    TEXT NOT NULL,
    date    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_id, date);

CREATE TABLE IF NOT EXISTS ratings (
    coach_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    athlete_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stars      INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (coach_id, athlete_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text    TEXT NOT NULL,
    date    TEXT NOT NULL,
    read    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS activity_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action  TEXT NOT NULL,
    detail  TEXT,
    ip      TEXT,
    at      TEXT NOT NULL,
    ua      TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_at ON activity_logs(at);
CREATE INDEX IF NOT EXISTS idx_logs_user ON activity_logs(user_id, at);

-- OTPs are stored as SHA-256 hashes only: a leaked database backup must not
-- be enough to reset somebody's password. `attempts` kills a token after a
-- handful of wrong guesses so a 6-digit code cannot be brute-forced.
CREATE TABLE IF NOT EXISTS email_tokens (
    -- An explicit surrogate key (Phase 2.9). This table used to be addressed
    -- by SQLite's implicit `rowid`, which does not exist in PostgreSQL and
    -- was the only place the application depended on it.
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL COLLATE NOCASE,
    purpose    TEXT NOT NULL,                 -- 'verify' | 'reset' | 'change'
    code_hash  TEXT NOT NULL,                 -- sha256 of the 6-digit OTP
    token_hash TEXT NOT NULL,                 -- sha256 of the long link token
    payload    TEXT,                          -- e.g. user id for 'change'
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens ON email_tokens(email, purpose);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON athlete_profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_coachprofiles_user ON coach_profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_ratings_coach ON ratings(coach_id);
CREATE INDEX IF NOT EXISTS idx_ratings_athlete ON ratings(athlete_id);
CREATE INDEX IF NOT EXISTS idx_notifs_user ON notifications(user_id, read);

-- Phase 2.4B: five indexes, each added only after EXPLAIN QUERY PLAN showed
-- a scan or a temp B-tree sort on a query the application actually issues.
-- reports ORDER BY date  -> SCAN reports; USE TEMP B-TREE FOR ORDER BY
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date);
-- messages WHERE to_id=? OR from_id=?  -> full SCAN (idx_messages_to cannot
-- serve the OR; this gives the optimizer the second arm for a MULTI-INDEX OR)
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_id, date);
-- notifications WHERE user_id=? ORDER BY date  -> USE TEMP B-TREE FOR ORDER BY
CREATE INDEX IF NOT EXISTS idx_notifs_user_date ON notifications(user_id, date);
-- users WHERE role=? ORDER BY name  -> USE TEMP B-TREE FOR ORDER BY
CREATE INDEX IF NOT EXISTS idx_users_role_name ON users(role, name);
-- COUNT(*) FROM reports WHERE live=1  -> SCAN reports (partial index: the
-- live rows are a small minority, so this stays tiny)
CREATE INDEX IF NOT EXISTS idx_reports_live ON reports(live) WHERE live=1;
"""


def _tune_connection(conn):
    """Apply the settings that let SQLite serve many users at once.

    WAL lets readers and the writer work at the same time instead of
    blocking each other; busy_timeout makes a request wait briefly for a
    lock instead of failing; the rest trade a little disk safety for speed
    on a read-heavy workload. These run once per connection.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")       # concurrent reads + write
    conn.execute("PRAGMA busy_timeout = 8000")      # wait up to 8s for a lock
    conn.execute("PRAGMA synchronous = NORMAL")     # safe with WAL, much faster
    conn.execute("PRAGMA cache_size = -16000")      # ~16 MB page cache
    conn.execute("PRAGMA temp_store = MEMORY")      # temp tables in RAM
    conn.execute("PRAGMA mmap_size = 134217728")    # 128 MB memory-mapped I/O
    return conn


def get_db():
    if "db" not in g:
        g.db = _tune_connection(
            sqlite3.connect(DB_PATH, timeout=8.0, check_same_thread=False))
    return g.db


def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _columns(conn, table):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def migrate(conn):
    """Bring an existing database up to the hardened schema.

    Runs on every start and is idempotent. Only additive changes are made,
    except for email_tokens, whose rows are short-lived OTPs - dropping and
    recreating that table is safe and is what removes the plaintext codes.
    """
    ucols = _columns(conn, "users")
    if ucols and "sess_epoch" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN sess_epoch "
                     "INTEGER NOT NULL DEFAULT 0")
        print("[MIGRATE] users.sess_epoch added")

    tcols = _columns(conn, "email_tokens")
    if tcols and ("code_hash" not in tcols or "id" not in tcols):
        # Two reasons to rebuild this table, both safe: the oldest version
        # stored OTPs in plaintext, and the pre-2.9 version had no surrogate
        # key (it was addressed by SQLite's rowid, which PostgreSQL has no
        # equivalent for). Its rows are 15-minute one-time codes, so nothing
        # of value is lost - a user simply requests a new one - and this is
        # the only table in the schema for which that is true.
        conn.execute("DROP TABLE email_tokens")
        conn.executescript(SCHEMA)
        print("[MIGRATE] email_tokens rebuilt (hashed codes + surrogate key; "
              "pending OTPs invalidated)")

    rcols = _columns(conn, "reports")
    if rcols and "scoring_provider" not in rcols:
        # Additive only. Existing rows keep their scores and become
        # scoring_trusted = 0, which is the truth about them: they were
        # computed in the browser.
        conn.execute("ALTER TABLE reports ADD COLUMN scoring_provider TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN scoring_version TEXT")
        conn.execute("ALTER TABLE reports ADD COLUMN scoring_trusted "
                     "INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE reports ADD COLUMN scoring_confidence REAL")
        print("[MIGRATE] reports scoring provenance columns added")

    lcols = _columns(conn, "activity_logs")
    if lcols and "ua" not in lcols:
        conn.execute("ALTER TABLE activity_logs ADD COLUMN ua TEXT")
    conn.commit()


def require_supported_backend():
    """Fail fast when configured for a backend that is not implemented.

    Phase 2.9 made the codebase ready for PostgreSQL; it did not implement
    it. A deployment that sets a postgres:// DATABASE_URL must be told that
    plainly, at startup - the alternative is a service that quietly writes to
    an ephemeral local SQLite file that nobody is backing up.
    """
    from athletix.config import DB_BACKEND, safe_database_url
    if DB_BACKEND != "sqlite":
        raise RuntimeError(
            "DATABASE_URL selects the %r backend (%s), which this build does "
            "not implement. The migration plan is in "
            "POSTGRESQL_READINESS.md; unset DATABASE_URL to use SQLite at "
            "DB_PATH." % (DB_BACKEND, safe_database_url()))


def connect(path=None):
    """Open a tuned connection outside a request context (bootstrap, CLI)."""
    require_supported_backend()
    ensure_db_dir(path or DB_PATH)
    return _tune_connection(sqlite3.connect(path or DB_PATH, timeout=8.0))


def ping():
    """Cheapest possible "is the database answering?" query.

    Lives here rather than in the readiness route: `SELECT 1` is still SQL,
    and routes hold no SQL.
    """
    return get_db().execute("SELECT 1").fetchone() is not None


def init_db(path=None):
    """Create the schema and apply migrations. Idempotent.

    This is the ONLY thing that touches the database at startup, and it is
    called explicitly by create_app() - never as an import side effect.
    """
    require_supported_backend()
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
    finally:
        conn.close()
