# AthletixAI — Phase 2.1 Architecture Audit

**Date:** 2026-08-28
**Scope:** backend architecture, data access, scalability, testability, PostgreSQL readiness.
**Status:** audit only — **no code was modified.**
**Companion docs:** `SECURITY_AUDIT.md`, `SECURITY_REPORT.md` (Phase 1, complete and non-negotiable).

**Method.** Static inspection of every route, helper and query; plus *measured* evidence —
per-request query counters, `EXPLAIN QUERY PLAN` on a synthetic 518-user / 10,072-report
dataset, and function/line metrics. Every number below was measured, not estimated.

---

## 1. Current architecture

```
                    ┌──────────────────────────────────────────────┐
   Browser          │  templates/index.html   (5,807 lines)        │
   ───────          │  • UI + router + in-memory store (DB object) │
                    │  • MediaPipe pose analysis   ← ALL CV HERE   │
                    │  • Biomechanics scoring      ← ALL SCORING   │
                    │  • jsPDF report rendering    ← ALL PDF HERE  │
                    └───────────────┬──────────────────────────────┘
                                    │ 15 distinct endpoints
                                    │ (/api/state does the heavy lifting)
                    ┌───────────────▼──────────────────────────────┐
   Server           │  app.py   (2,836 lines, 106 defs, 43 routes) │
   ──────           │                                              │
                    │  config globals ─ 45 module-level constants  │
                    │  email/SMTP    ─ synchronous, in-request     │
                    │  serializers   ─ user_public / report_public │
                    │  auth+CSRF     ─ before_request hooks        │
                    │  43 route handlers ── 87 inline SQL calls    │
                    │  seed / migrate / owner bootstrap            │
                    └───────────────┬──────────────────────────────┘
                                    │ sqlite3, one connection per request
                    ┌───────────────▼──────────────────────────────┐
                    │  athletixai.db — 12 tables, 12 indexes       │
                    └──────────────────────────────────────────────┘
```

**There is no package structure.** No `models/`, `services/`, `repositories/`, `schemas/`.
One module holds configuration, email, security, serialization, routing, business logic,
data access, migrations and seeding.

### Measured inventory

| Metric | Value |
|---|---|
| `app.py` | 2,836 lines · 116 KB · 106 functions · 43 routes |
| `templates/index.html` | 5,807 lines · 403 KB (UI + CV + scoring + PDF) |
| SQL call sites | **151** — 87 inside 32 route handlers, 64 in helpers |
| Ad-hoc error responses | **67** `jsonify(ok=False, …)`, no error codes |
| Repeated boilerplate | `current_user()` ×28, `body()` ×19, `too_many()` ×15 |
| `.commit()` / `rollback()` | **26 / 0** |
| Server-side CV dependencies | **0** (`requirements.txt` = flask, bcrypt, gunicorn) |
| Health endpoint | **none** |
| API versioning | **none** |

### Largest functions
`seed` 100 · `signup` 86 · `scoped_state` 65 · `ensure_owner_account` 60 · `add_report` 55 ·
`startup_checks` 54 · `rate_coach` 50 · `login` 49 · `reset_password` 48 · `send_email` 47.
*(Two entries a naive scan reports as larger — `_bcrypt_rounds`, `close_db` — are artifacts:
module-level constants and the `SCHEMA` string sit between those defs.)*

**Circular dependencies: none** — a single module cannot have them. That is the one upside
of the current shape, and it means extraction can be done safely in dependency order.

---

## 2. Findings

Severity: **BLOCKER** (must fix before scale) · **HIGH** · **MEDIUM** · **LOW**.

### A. Performance / scalability

| # | Finding | Sev | Evidence |
|---|---|---|---|
| **A1** | **`/api/state` is an N+1 explosion.** `scoped_state()` calls `user_public()` per user (one profile query each) and `report_public()` per report (one `ai_results` query each). | **BLOCKER** | Measured on 518 users / 10,072 reports: **athlete 537 queries / 1.25 MB; coach 10,603 queries / 2.15 MB / 183 ms; owner 10,603 queries / 2.21 MB.** On the 18-user dev DB it is 37 queries — the cost is invisible until it isn't. At 5,000 athletes × 20 assessments this is ~100,000 queries and ~20 MB **per dashboard load**. |
| **A2** | **No pagination on any large collection.** Only 2 of 43 routes bound their result set (`/api/my/notifications` hardcodes `LIMIT 200`; `/api/admin/activity` takes a limit param). `users`, `reports`, `messages`, `ratings`, leaderboard and export are all unbounded. | **BLOCKER** | 29 unbounded `SELECT *` collection queries. |
| **A3** | **Synchronous SMTP inside request handlers.** 9 `send_email()` calls in routes; each tries up to 2 SMTP connections at `timeout=20`. `verify_email` sends **two** emails. Worst case ≈ **80 s of blocking I/O in one HTTP request**, against a `--workers 3 --threads 8` pool (24 slots). | **BLOCKER** | A slow or unreachable SMTP host stalls the whole app. |
| **A4** | **Missing indexes on real hot paths** (verified with `EXPLAIN QUERY PLAN`). | **HIGH** | `reports ORDER BY date` → `SCAN reports; USE TEMP B-TREE`. `messages WHERE to_id=? OR from_id=?` → **full `SCAN messages`** (`idx_messages_to` cannot serve the OR). `COUNT(*) FROM reports WHERE live=1` → `SCAN reports`. Notifications and the athlete directory use an index but still `USE TEMP B-TREE FOR ORDER BY`. |
| **A5** | 7 `PRAGMA` statements executed per connection, and connections are per-request. | **LOW** | Trivial today; becomes measurable under load and is wasted work against a pooled Postgres. |

**Missing indexes, justified by measured plans (not guesswork):**
`reports(date)` · `messages(from_id, date)` · `notifications(user_id, date)` ·
`users(role, name)` · partial index on `reports(live) WHERE live=1`.

### B. Correctness / transactions

| # | Finding | Sev | Evidence |
|---|---|---|---|
| **B1** | **Security-critical operations are split across two transactions.** `reset_password`, `change_password` and `admin_set_role` each `commit()` the credential/role change, *then* call `bump_epoch()` which commits separately. | **HIGH** | A crash between the two leaves **the password changed but every old session still valid** — precisely the Phase 1 guarantee (H3) that session revocation was added to provide. Same for a role change: new role, stale permissions in live cookies. Not currently exploitable remotely, but it is a real atomicity hole in a security control. |
| **B2** | **Zero `rollback()` and zero explicit `BEGIN`** across 26 commits. Atomicity relies on sqlite3's implicit transaction plus connection teardown. | **MEDIUM** | Works by accident today. `log_activity()` performs its *own* commit — if it is ever called between a write and its commit, it will prematurely commit partial work. Currently every call site is after the commit; nothing enforces that. |
| **B3** | No unit-of-work / transaction boundary abstraction, so the fix for B1/B2 has nowhere to live. | **MEDIUM** | Structural. |

### C. Structure / maintainability

| # | Finding | Sev | Evidence |
|---|---|---|---|
| **C1** | **Business logic lives in route handlers.** 87 of 151 SQL call sites are inside 32 handlers. `export_data` alone issues 12 queries; `add_report` 6; `signup` 4. | **HIGH** | Target flow (route → service → repository) does not exist. |
| **C2** | **Cross-cutting boilerplate repeated per handler:** `u = current_user()` ×28, `d = body()` ×19, the `too_many(...)` / `if limited:` pair ×15. | **MEDIUM** | Every new endpoint must remember all three. Forgetting the rate-limit pair is a silent security regression. |
| **C3** | **No error taxonomy.** 67 ad-hoc `jsonify(ok=False, error="…")` sites; status codes chosen inline (34×400, 13×403, 10×404, 4×401). Clients cannot branch on a machine-readable code. | **MEDIUM** | Requirement asks for `VALIDATION_ERROR`, `FORBIDDEN`, `NOT_FOUND`, … |
| **C4** | **Serialization is centralised but coupled to transport.** `user_public(row, viewer)` is *good* (it is the Phase 1 privacy boundary) but takes a viewer and does its own DB reads — mixing DTO, authorization and data access. | **MEDIUM** | Must be preserved behaviourally; refactor carefully. |
| **C5** | Response envelope is `{ok: bool, …}` — consistent in spirit, unstructured in practice (42 `ok=True`, 67 `ok=False`, 2 raw `make_response`). | **LOW** | |

### D. Configuration / testability

| # | Finding | Sev | Evidence |
|---|---|---|---|
| **D1** | **No application factory. `import app` has heavy side effects:** `_load_dotenv()`, `seed()`, `ensure_owner_account()`, `startup_checks()` all run at module import — creating/migrating the database, provisioning the owner, and possibly `raise RuntimeError`. | **BLOCKER** *(for Phase 2)* | This is why `tests/conftest.py` must pin ~10 environment variables *before* the import line. You cannot instantiate two configurations in one process, cannot import for CLI tooling, and cannot unit-test a service in isolation. Every later refactor is gated on this. |
| **D2** | **45 module-level constants** read `os.environ` at import time. | **HIGH** | No dev/test/prod config objects; behaviour is frozen at import. |
| **D3** | Rate limiter state is a process-local dict (`limiter._store`). Documented in Phase 1; still a correctness gap at >1 worker. | **MEDIUM** | With `--workers 3` the effective allowance is ~3×. |
| **D4** | Tests are excellent for security (121 tests, 14 classes) but are **all HTTP-level**. There are no service or repository tests, because there are no services or repositories. | **MEDIUM** | Coverage must not drop during extraction. |

### E. PostgreSQL readiness

| Item | Count | Verdict |
|---|---|---|
| `AUTOINCREMENT` | 6 | Rewrite → `GENERATED … AS IDENTITY` / `SERIAL`. |
| `COLLATE NOCASE` (on `users.email`) | 2 | **Behavioural risk.** Postgres has no `NOCASE`; needs `CITEXT` or a `LOWER(email)` unique index. Case-insensitive login/uniqueness is a *security-relevant* behaviour — must be preserved deliberately. |
| `PRAGMA` | 8 | SQLite-only; drop behind the connection abstraction. |
| `rowid` | 13 | Used in `consume_token` / token cleanup. **No `rowid` in Postgres** — needs a real surrogate key on `email_tokens`. |
| `ON CONFLICT … DO UPDATE` | 2 | Portable (ratings, profile_settings) — Postgres supports this. |
| `executescript` | 3 | Replace with a migration runner. |
| Dates stored as ISO **TEXT** and compared as strings | throughout | Works in both engines for ISO-8601, but should become `TIMESTAMPTZ`. Note existing precision inconsistency: `now_iso()` uses second precision while `issue_token` writes microseconds. |
| Foreign keys / `ON DELETE CASCADE` | present & correct | Portable. |

**No SQL injection surface** — every statement is parameterised; the single dynamic
fragment (`coach_profiles` SET clause) is built from a hardcoded column allowlist.
Re-verified in this audit.

### F. AI / CV integration

| # | Finding | Sev |
|---|---|---|
| **F1** | **There is no server-side AI/CV layer to isolate — because there is none at all.** `requirements.txt` is flask + bcrypt + gunicorn. MediaPipe pose analysis, biomechanics scoring and jsPDF report rendering all run **in the browser**. | **HIGH (design)** |
| **F2** | Consequence: **the client computes its own scores and POSTs them.** `POST /api/reports` accepts `m{speed,agility,strength,stamina,technique}` and trusts them (clamped 0–100, attribution forged-proof). The leaderboard is therefore gameable. | **HIGH (integrity)** |
| **F3** | `reports.ai` is `NULL` on all 72 production rows — the column and serializer exist, nothing populates them. | **MEDIUM** |

This reframes the brief's "isolate AI/CV behind an interface" task: the boundary to build is
**a scoring service the server owns**, with the current client-side pipeline as one
implementation — not a wrapper around code that doesn't exist server-side.

### G. API design & observability

| # | Finding | Sev |
|---|---|---|
| **G1** | No versioning. All routes are `/api/…`; there is no `/api/v1/…`. | MEDIUM |
| **G2** | **No health endpoint** (zero occurrences of "health"). Nothing for a load balancer or uptime check. | MEDIUM |
| **G3** | No request IDs in logs. The 500 handler mints a `ref` for the client but it is not attached to a structured log line. | MEDIUM |
| **G4** | Naming is inconsistent: `/api/my/reports`, `/api/reports/<id>`, `/api/coach/athletes`, `/api/admin/users`. Mixed resource-first and role-first. | LOW |
| **G5** | `/api/state` is an RPC-shaped bulk endpoint, not REST. It is also the *only* endpoint the dashboard hydrates from. | HIGH |

**Frontend coupling is small and that is the good news:** the UI calls only **15 distinct
endpoints**. `/api/state` is the one that matters. The backend can be restructured
extensively behind a stable contract.

---

## 3. Target architecture

Adapted to Flask + SQLite→Postgres. Same responsibilities as the brief's tree, expressed
as a Python package rather than a `backend/` rewrite.

```
app/
├── __init__.py            create_app() factory  ← fixes D1, unblocks everything
├── config.py              Dev / Test / Prod config objects  ← fixes D2
├── extensions.py          db handle, limiter, mail
├── api/
│   └── v1/                auth, users, athletes, coaches, assessments,
│                          reports, messaging, notifications, leaderboard,
│                          analytics, admin, health     ← thin blueprints
├── services/              auth, athlete, coach, assessment, report,
│                          messaging, notification, analytics, leaderboard
├── repositories/          user, athlete, coach, report, message,
│                          notification, rating, activity   ← all SQL lives here
├── schemas/               request validation + response DTOs (privacy boundary)
├── security/              session, csrf, rbac, rate_limit, password, tokens
├── database/              connection, unit_of_work, migrations/
├── integrations/          scoring/ (interface + client-submitted impl), mail/, storage/
├── jobs/                  callable units: email_send, report_build, export_build
└── common/                errors (taxonomy), pagination, logging, time
tests/
├── security/              ← Phase 1 suite, moved verbatim, must stay green
├── services/  repositories/  api/
```

**Deliberately NOT doing:** microservices, Redis, Celery, an ORM migration, or a
PostgreSQL cutover in the same step as the refactor.

---

## 4. Recommended migration sequence

Strictly incremental. **The Phase 1 suite (183 tests) runs after every step and must stay
at 183/183.** Nothing merges on a red suite.

| Sub-phase | Work | Risk | Why this order |
|---|---|---|---|
| **2.2 Foundation** | `create_app()` factory, `config.py`, package skeleton, `common/errors.py` taxonomy, `/api/v1/health`. Keep `app.py` importable as a shim. | **Low** | D1 gates every later step. Nothing else can be tested in isolation until import is side-effect-free. |
| **2.3 Data layer** | `database/connection.py`, `unit_of_work` (fixes **B1/B2**), repositories for user/report/message/notification/rating/activity. Move SQL out of handlers. | Medium | Highest-value structural change; makes A1 fixable. |
| **2.4 Performance** | Fix **A1** (batch-load profiles and `ai_results` — 10,603 queries → <10), add the 5 measured indexes (**A4**), add cursor pagination (**A2**). | Medium | Do *after* repositories exist so the fix lands in one place. |
| **2.5 Services** | Extract auth, assessment, report, messaging, notification, leaderboard, analytics services. Routes become thin. | Medium | Depends on 2.3. |
| **2.6 Schemas & API v1** | Request/response schemas; mount blueprints under `/api/v1/*` **with `/api/*` aliases retained** so the frontend keeps working; standardised errors + pagination envelope. | Medium | Backward compatibility is mandatory. |
| **2.7 Async boundaries** | Move `send_email` (**A3**) and export building behind `jobs/` — invoked inline at first, but as independent callables. Recommend a queue only if measurement justifies it. | Low | Decouples without adding infrastructure. |
| **2.8 Scoring boundary** | `integrations/scoring/` interface; current client-submitted scores become the `ClientSubmittedScoring` implementation, marked untrusted. Enables future server-side scoring (**F2**) without touching the assessment flow. | Low | Interface only; no model/behaviour change. |
| **2.9 Postgres readiness** | `POSTGRESQL_READINESS.md` + resolve `rowid` (**email_tokens** surrogate key), `COLLATE NOCASE` → citext/functional index, `AUTOINCREMENT`, timestamps. **No data migration yet.** | Medium | Behaviour-preserving prep only. |

**Rollback posture:** each sub-phase is independently revertible; `app.py` stays as a shim
re-exporting the factory until 2.6, so nothing outside the package needs to change at once.

---

## 5. Risks

1. **Privacy regression is the #1 risk.** `user_public(row, viewer)` *is* the Phase 1
   privacy boundary (C4). Splitting it into DTO + repository is exactly where an
   e-mail or phone field could quietly reappear. Mitigation: move it verbatim first,
   refactor second, with `TestPrivacy` green at every step.
2. **Rate-limit coupling.** `too_many()` is invoked per handler (C2). Converting it to a
   decorator risks silently dropping a bucket. Mitigation: assert bucket coverage per route.
3. **`/api/state` contract.** The dashboard renders entirely from its shape, including the
   `leaders` block and `m: null` scoping. Any change breaks the UI. Mitigation: freeze the
   response shape; add a contract test before touching `scoped_state`.
4. **Import-time side effects during extraction** — moving `seed()`/`ensure_owner_account()`
   out of import can change startup ordering. Mitigation: explicit CLI/bootstrap entrypoint.
5. **Behavioural drift on case-insensitive email** if Postgres prep is rushed.

## 6. Do NOT change yet

- Phase 1 security semantics: RBAC, IDOR/BOLA scoping, CSRF, session epoch, rate-limit
  values, password policy, token hashing, audit logging.
- The `/api/state` response shape and the dashboard's data contract.
- AI/CV scoring behaviour and report content.
- The database engine — **stay on SQLite through Phase 2**; prepare only.
- The frontend (redesign is complete and out of scope).
- `templates/index.html` structure.

---

## 7. Verification baseline (must hold after every sub-phase)

- **183/183** security tests passing.
- All 28 pages across athlete / coach / owner render, zero console errors, zero CSP violations.
- Scoped-state privacy: 0 foreign e-mails, 0 foreign athlete metrics, 0 foreign messages,
  0 foreign notifications.
- Login, signup→verification, reports, leaderboard, messaging, notifications, profile,
  exports, admin/owner functionality all working.
