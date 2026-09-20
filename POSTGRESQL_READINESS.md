# PostgreSQL Readiness — AthletixAI (Phase 2.9)

**Status: audit and preparation only. No database was migrated, no production
data was touched, and this build does not talk to PostgreSQL.** Setting a
`postgres://` `DATABASE_URL` makes the process refuse to start with an
explicit message rather than silently writing to a local SQLite file
(`athletix/database.py::require_supported_backend`).

Everything below was read out of the code, not assumed.

---

## 1. Where database-specific code lives now

| Layer | Contains SQL? | Engine-specific constructs |
|---|---|---|
| `athletix/api/*` (routes) | no | none |
| `athletix/services/*` | no | none |
| `athletix/schemas/*` (DTOs) | no | none |
| `athletix/serializers.py` | no *(2.9: was 8 raw queries)* | none |
| `athletix/security/session.py` | no *(2.9: was 2)* | none |
| `athletix/security/tokens.py` | no *(2.9: was 6, incl. `rowid`)* | none |
| `athletix/repositories/*` | **yes** | `?` paramstyle, `ON CONFLICT`, `lastrowid` |
| `athletix/database.py` | **yes** | schema DDL, `PRAGMA`, `AUTOINCREMENT`, `COLLATE NOCASE` |
| `athletix/bootstrap.py` | **yes** (seed + owner provisioning) | `?` paramstyle, `lastrowid` |

A PostgreSQL port therefore touches three files plus the repositories — not
the application. `athletix/bootstrap.py` is the one remaining module outside
`repositories/` that writes SQL directly; it is startup/CLI code and is listed
as remaining debt rather than rewritten in this phase.

## 2. Schema mapping

| SQLite (current) | PostgreSQL | Notes |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED ALWAYS AS IDENTITY` (or `bigserial`) | 7 tables: `users`, `reports`, `videos`, `messages`, `notifications`, `activity_logs`, `email_tokens` |
| `TEXT` | `text` | direct |
| `INTEGER` (counts, ages, metrics) | `integer` | direct |
| `INTEGER` used as a boolean (`verified`, `live`, `read`, `scoring_trusted`) | `boolean` **or** keep `smallint` | see §3.4 |
| `REAL` (`overall`, `size_mb`, `scoring_confidence`) | `double precision` | direct |
| `TEXT COLLATE NOCASE` (`users.email`, `email_tokens.email`) | `citext`, or `text` + `UNIQUE (lower(email))` | see §3.1 |
| `TEXT` ISO-8601 timestamps | `text` (phase 1) → `timestamptz` (phase 2) | see §3.3 |
| `TEXT` holding JSON (`coach_profiles.achievements`) | `jsonb` (optional) | app already `json.dumps`/`loads`; `text` works unchanged |
| `CHECK (role IN (...))`, `CHECK (stars BETWEEN 1 AND 5)` | identical | portable |
| `REFERENCES ... ON DELETE CASCADE` | identical | portable; PG enforces FKs always |
| `CREATE INDEX ... WHERE live=1` (partial) | identical | PG supports partial indexes |
| composite PK `(coach_id, athlete_id)` | identical | portable |

## 3. Incompatibilities found, and the required change

### 3.1 `COLLATE NOCASE` on e-mail — **behavioural, must be handled**
`users.email` is `TEXT NOT NULL UNIQUE COLLATE NOCASE`, so `WHERE email=?`
matches case-insensitively and the UNIQUE constraint blocks
`Bob@x.com`/`bob@x.com` from coexisting. PostgreSQL has no `NOCASE`.

*Mitigating fact:* every address is already lowercased at the edge
(`schemas.fields.Email`) before it reaches a repository, so lookups would
still work. The UNIQUE constraint is the part that must not be lost.

**Required:** `CREATE EXTENSION citext;` and `email citext`, or keep `text`
and add `CREATE UNIQUE INDEX ON users (lower(email))`. `tests/test_repositories.py`
pins the case-insensitive lookup, so a regression fails the suite.

### 3.2 `rowid` — **resolved in this phase**
`security/tokens.py` addressed `email_tokens` rows by SQLite's implicit
`rowid`, which PostgreSQL does not have. The table now carries an explicit
`id INTEGER PRIMARY KEY AUTOINCREMENT` and every statement uses it. The
migration rebuilds that table; its rows are 15-minute one-time codes, so the
only effect is that a pending OTP must be re-requested.

### 3.3 Timestamps — **must be decided before any data move**
Every timestamp is `TEXT` from
`datetime.datetime.now().isoformat(timespec="seconds")` — **local time, no
timezone**. Ordering and range comparisons are string comparisons, which is
correct only because ISO-8601 sorts lexicographically.

Consequences to accept or fix:
* two processes in different timezones would write incomparable values;
* `expires_at < now.isoformat()` (token expiry) is a string compare and stays
  correct only while the format is fixed-width.

**Recommended:** migrate to `timestamptz` in a *separate* step from the engine
move, converting with `to_timestamp(col, 'YYYY-MM-DD"T"HH24:MI:SS')` and
switching `now_iso()` to `datetime.now(timezone.utc)`. Doing both at once
makes a rollback ambiguous.

### 3.4 Booleans stored as 0/1
Reads are safe (`bool(row["live"])` works for `smallint` and `boolean`), but
two *queries* compare literally and would need `= TRUE`:
`reports.count_live()` (`WHERE live=1`) and `idx_reports_live`
(`WHERE live=1`). Keeping the columns as `smallint` avoids both changes and
is the lower-risk option for the first migration.

### 3.5 Parameter style — **every repository statement**
The whole data layer uses qmark (`?`). psycopg uses `%s`. There is no
compatibility shim in the standard library. Options, in order of preference:

1. a thin `Repository` translation (one `re.sub` in `base.py`, since every
   statement is a module-level constant with no `%` literals) — smallest
   diff, keeps the SQL readable;
2. SQLAlchemy Core — larger change, but also solves §3.6 and §3.7;
3. rewrite every statement — 78 statements across the repositories, highest churn.

### 3.6 `cursor.lastrowid`
Used by `users.create`, `reports.create`, `messages.create`,
`tokens.insert`, and three times in `bootstrap.py`. psycopg does not populate it.
**Required:** append `RETURNING id` and read the row.

### 3.7 `PRAGMA`
`_tune_connection` issues seven PRAGMAs (`foreign_keys`, `journal_mode=WAL`,
`busy_timeout`, `synchronous`, `cache_size`, `temp_store`, `mmap_size`) and
`migrate()` introspects with `PRAGMA table_info`. None exist in PostgreSQL.
Equivalents: foreign keys are always enforced; WAL/synchronous/mmap are
server-side settings, not per-connection; introspection becomes
`information_schema.columns`. All of it is inside `database.py`.

### 3.8 `ON CONFLICT ... DO UPDATE` — **already portable**
`ratings.upsert` and `users.set_consent` use
`ON CONFLICT (cols) DO UPDATE SET x=excluded.x`, which is PostgreSQL 9.5+
syntax and needs no change.

### 3.9 Type affinity
SQLite would store `'abc'` in an `INTEGER` column; PostgreSQL raises. This is
a *tightening*, and the application already coerces every numeric field
through `validation.as_int` before it reaches SQL, so it is a safety net
rather than a blocker.

### 3.10 Redundant index
`idx_users_email` duplicates the implicit index behind `users.email UNIQUE`.
Harmless on SQLite; drop it under PostgreSQL rather than carrying two.

## 4. Transactions and concurrency

| | SQLite (today) | PostgreSQL |
|---|---|---|
| Writers | one at a time; WAL lets readers continue | MVCC, concurrent writers |
| Blocking | `busy_timeout = 8000` waits for the lock | row-level locks; set `lock_timeout`/`statement_timeout` |
| Transaction start | driver opens a deferred transaction on the first write | connection is in a transaction until commit/rollback |
| Isolation | serialised by the single writer | READ COMMITTED by default |

`athletix/unit_of_work.py` is depth-re-entrant and only the outermost block
commits or rolls back, which is engine-independent. Two specific concerns:

* **Read-modify-write on `sess_epoch`.** `bump_session_epoch` is
  `SET sess_epoch = sess_epoch + 1`, computed in the database, so it is safe
  under concurrency in both engines.
* **Longer transactions become visible.** Under SQLite the single writer
  hides interleaving; under READ COMMITTED, two concurrent password changes
  for the same user can interleave between the `SELECT` in the service and
  the `UPDATE`. The credential change and the revocation already share one
  transaction, so the failure mode is a lost update, not a stale session.
  If that matters, `SELECT ... FOR UPDATE` on the user row is the fix — and
  it is a repository change, nothing above it.
* **Idle-in-transaction.** The Flask teardown closes the connection per
  request, so a pooled PostgreSQL deployment needs the same discipline plus
  a pooler (`pgbouncer` in transaction mode, or `psycopg_pool`).

## 5. Migration sequence (when it is actually done)

1. **Freeze the shape.** Full test suite green on SQLite; tag the release.
2. **Port the data layer** — §3.5 paramstyle, §3.6 `RETURNING id`, §3.7
   `PRAGMA`/introspection — behind the existing `DB_BACKEND` switch, with the
   SQLite path untouched. Run the entire suite against both backends in CI.
3. **Create the PostgreSQL schema** from the mapping in §2 (`citext` or the
   `lower(email)` unique index; keep `smallint` booleans; keep `text`
   timestamps for now).
4. **Dry run on a copy.** Restore a production SQLite backup, export table by
   table in dependency order (`users` → profiles → `reports` → `ai_results`
   → `videos` → `messages` → `ratings` → `notifications` →
   `profile_settings` → `activity_logs`; `email_tokens` is not migrated —
   pending OTPs are re-requested), then verify: row counts per table, three
   `bcrypt` hashes round-trip, foreign keys resolve, and
   `SELECT COUNT(*) FROM users WHERE lower(email) IN (...)` finds no
   duplicate that `COLLATE NOCASE` had been preventing.
5. **Cut over** during a short read-only window: stop writes, re-export,
   point `DATABASE_URL` at PostgreSQL, restart, run the smoke flows
   (login, `/api/state`, create report, send message, admin metrics).
6. **Timestamps** as a separate, later change (§3.3).

## 6. Rollback strategy

* **Before cutover:** nothing to roll back — SQLite is still authoritative.
* **During cutover:** the window is read-only, so the SQLite file is the
  unchanged source of truth. Rollback = unset `DATABASE_URL`, restart.
  Recovery time is one restart.
* **After cutover, with writes taken:** rolling back means replaying
  PostgreSQL rows written since cutover back into SQLite. Keep the pre-cutover
  SQLite file immutable, and treat the first 24 hours as the rollback window;
  after that, roll *forward*.
* **Schema changes are additive.** Every migration in `database.py` is
  `ALTER TABLE ADD COLUMN` (the exception, `email_tokens`, holds only
  short-lived OTPs), so a rolled-back deployment reads a newer schema without
  failing.

## 7. Environment and configuration requirements

| Variable | Meaning | Today |
|---|---|---|
| `DB_PATH` | SQLite file location | in use |
| `DATABASE_URL` | full connection URL; `postgres://…` is recognised and **refused** | recognised |
| `SECRET_KEY` | session signing key; fatal if missing in production | in use |

* No credential is hard-coded anywhere: `DATABASE_URL` is read from the
  environment and nothing else.
* `config.safe_database_url()` redacts the password, and it is the only form
  that appears in an error message or a log line.
* An unknown URL scheme is a startup error, not a silent fallback.
* A PostgreSQL deployment additionally needs: `psycopg[binary]` in
  `requirements.txt`, `sslmode=require` in the URL for a managed provider, a
  connection pooler, and the standard managed-service backup/PITR settings.
  None of these are added here, because none of them should be added before
  the port in §5 step 2 exists.

## 8. What this phase changed in code

* `email_tokens` gained a surrogate primary key; the last `rowid` dependency
  is gone.
* `serializers.py`, `security/session.py`, `security/tokens.py` and
  `api/health.py` no longer contain SQL — they go through repositories or
  `database.ping()`.
* `bootstrap.py` opens its connection through `database.connect()` instead of
  calling `sqlite3.connect` itself, so it gets the tuning, the directory
  creation and the backend check.
* `config` gained `DATABASE_URL`, `DB_BACKEND`, `db_backend()` and
  `safe_database_url()`; `database.require_supported_backend()` enforces them.
* `bootstrap.startup_checks()` referenced an undefined global `app`, so in
  production — its only branch — it raised `NameError` instead of verifying
  that session cookies are marked Secure. It now takes the app.
