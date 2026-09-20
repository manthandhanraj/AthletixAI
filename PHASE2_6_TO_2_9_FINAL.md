# AthletixAI — Phase 2.6 → 2.9: Final Architecture Hardening

**Date:** 2026-08-30 · **Scope:** request/response schemas + `/api/v1`, the asynchronous
work boundary, the scoring interface, PostgreSQL readiness.

**Result: 471 tests passing, 0 failures.** Every number below was measured, not estimated.

| Phase | Result |
|---|---|
| 2.6 Schemas + API v1 | **PASS** |
| 2.7 Async boundaries | **PASS** |
| 2.8 Scoring interface | **PASS** |
| 2.9 PostgreSQL readiness | **PASS** |

---

## 1. Phase 2.6 — Schemas and API v1

### Request schemas
`athletix/schemas/` is new: a 341-line dependency-free schema layer
(`fields.py`) plus 19 per-endpoint request schemas (`requests.py`). Ad-hoc
`d.get(...)`, `int(...)` and scattered required-field checks are gone from the
handlers.

Two rules keep the layer honest and kept behaviour identical:

* **The schema owns shape; the service owns meaning.** Trimming, lowercasing,
  coercion and range checks happen at the edge. "Is this role grantable",
  "is this address taken", "may this caller write here" stay in
  `athletix/services/`, which must keep working when called without a request.
* **Field order is error order.** When a body is wrong in two ways, the caller
  sees the message for whichever field is declared first. Each schema's field
  order mirrors the order its service validates in, so no client sees a
  different message than it did before.

Fields a service validates *conditionally* (an athlete's sport is checked, a
coach's stray `sport` is ignored) are declared `Raw` rather than pre-empted —
pre-empting would answer 400 where Phase 1 answered 200.

A schema returns exactly its declared keys, so an undeclared client key cannot
reach a service call. `ProfileUpdateRequest` has no `role` and no `verified`
field at all; `RoleChangeRequest`'s vocabulary has no `owner`.

### Response DTOs
`athletix/schemas/responses.py` declares what leaves the process.
`serializers.py` **remains the privacy boundary** — `user_dto`/`report_dto`
delegate to `user_public`/`report_public` unchanged. What moved into the DTO
module is the shapes carrying no privacy decision (message, notification,
pagination envelope) plus the rating shape, which were previously rebuilt
inline in three modules and could drift apart. No raw row is ever returned.

### `/api/v1`
Every API blueprint now declares **relative** routes and is mounted twice:

```
/api/...      the Phase 1 paths, kept as permanent compatibility aliases
/api/v1/...   the versioned namespace
```

Both mounts are the *same blueprint object*, so both run the same handler,
schema, authorization and service call. There is no duplicated business logic:
`test_both_mounts_run_the_same_handler` asserts the view function behind
`/api/x` and `/api/v1/x` is literally the same object, for all 38 endpoints.

| | Count |
|---|---|
| Route rules total | 83 |
| `/api/...` (Phase 1 paths, all present) | 38 |
| `/api/v1/...` (exact mirror) | 38 |
| Pages + health probes (deliberately unversioned) | 7 |

Rate limiting, CSRF, the body-size cap and `Cache-Control: no-store` all key
off the shared `/api/` prefix, so no control is bypassable by switching
namespace — proved by `test_rate_limit_counters_are_shared_across_mounts`.
The one path-specific exemption (the profile-photo body-size carve-out) is
declared for both mounts and tested on both.

---

## 2. Phase 2.7 — Async boundaries

**The defect (Phase 2.1 finding A3):** `send_email()` ran inside request
handlers. Its SMTP client waits up to 20 seconds per attempt across two ports,
so one unreachable mail host blocked a worker for ~40 seconds on signup, on
login of an unverified account, and on password reset.

**The fix:** `athletix/jobs/` — one interface, two implementations.

| Dispatcher | When | `submit()` returns True when |
|---|---|---|
| `InlineDispatcher` | no SMTP configured (sending is a console print) | the job ran and succeeded |
| `BackgroundDispatcher` | SMTP configured | the job was queued |

Application code calls `mail.queue_email()`; `mail.send_email()` is the
transport and is never called from a handler
(`test_no_service_calls_the_blocking_transport_directly` enforces this).

**The rule that makes the boundary safe:** nothing behind it decides whether
the request succeeded. The verification code is issued and **committed** in the
request; only the SMTP conversation is deferred. A password is changed and every
session revoked in the request; only the notice is deferred. With the mail
transport dead, the security-relevant work has still happened —
`test_password_reset_completes_and_revokes_sessions_with_async_mail` asserts
exactly that.

**Measured, not asserted.** The tests install a transport that sleeps 750 ms and
then time the request:

* signup, `/auth/forgot` and unverified login each return in **< 300 ms**;
* the transport's thread name is recorded and compared to the request thread —
  SMTP never runs on it;
* a failing transport does not fail the request, and does not kill the worker.

**Deliberately not added:** Celery, RQ, Redis. The honest limits of an
in-process queue are documented in `athletix/jobs/dispatcher.py`, in
`.env.example` and in §5 below. The queue is **bounded**: a saturated queue
refuses work rather than growing, which is why `/auth/forgot` can still answer
502 instead of promising a code that will never arrive.

The process-local rate limiter is unchanged, as instructed; its ~N× allowance
under N workers remains documented in `athletix/security/ratelimit.py`.

---

## 3. Phase 2.8 — Scoring interface

**The fact this phase is built on:** the AI/CV pipeline runs in the **browser**.
`templates/index.html` loads MediaPipe Tasks Vision and derives five metrics
from 33 pose keypoints. The server has never computed a score, and this phase
did not pretend otherwise or migrate the pipeline.

`athletix/scoring/` is a narrow, implementation-agnostic boundary:

```
route → schema → service → ScoringService → ScoringProvider
```

* `contracts.py` — `ScoreInput` (validated in one place, `from_request`),
  `ScoreResult`, contract version `1.0`, the source vocabulary, metric
  validation and AI-verdict bounding.
* `base.py` — the `ScoringProvider` interface: `name`, `version`, `trusted`,
  `supports()`, `score()`.
* `client_submitted.py` — `ClientSubmittedScoring`, the honest model of what
  happens today.

**The trust boundary:**

* the server **always** recomputes `overall` from the metrics it stores; a
  client-supplied overall is never persisted;
* `trusted` is decided by the provider, never read from the request. A body
  saying `{"scoring": {"trusted": true, "provider": "server-verified"}}`
  changes nothing — the row still records `client-submitted`, `trusted=0`;
* the client AI verdict is bounded (`potential` ≤ 40 chars, `medal` 0–100,
  angle brackets neutralised) and stored as presentation data on an untrusted
  report, not as a server judgement;
* `confidence` is `None` for client-submitted scores. The browser reports no
  calibrated confidence, and inventing one would look like evidence.

**Persistence.** `reports` gained four additive columns —
`scoring_provider`, `scoring_version`, `scoring_trusted`,
`scoring_confidence` — with an `ALTER TABLE ADD COLUMN` migration. Verified
against a copy of the real database: all 72 existing reports preserved, columns
added, existing rows correctly read as untrusted. Report DTOs now carry a
`scoring` block; the frontend ignores unknown fields and is unaffected.

**Implementation-agnosticism is tested, not claimed.**
`test_a_different_provider_changes_nothing_above_the_boundary` installs a fake
server-side provider (`trusted=True`, `confidence=0.9`) and asserts the route,
the repository, the stored row and the DTO all carry it through with no other
change. Static tests assert no server module imports MediaPipe, OpenCV, torch
or tensorflow, and that no route or service names a scoring implementation.

---

## 4. Phase 2.9 — PostgreSQL readiness

**No database was migrated. No production data was touched.** The full audit —
schema mapping, every incompatibility, the SQL that would have to change,
transaction and concurrency differences, the migration sequence, the rollback
strategy and the environment requirements — is in
**`POSTGRESQL_READINESS.md`**.

Code changes made to earn that readiness:

| Change | Why |
|---|---|
| `email_tokens` gained an explicit `id` primary key | it was addressed by SQLite's implicit `rowid`, which PostgreSQL has no equivalent for — the last such dependency |
| `security/tokens.py` → `repositories/tokens.py` | 6 raw statements out of the security layer; it now holds policy only |
| `serializers.py` → repositories | 8 raw queries removed from the privacy boundary |
| `security/session.py` → repositories | 2 raw queries; `bump_epoch` now goes through `users.bump_session_epoch` |
| `api/health.py` → `database.ping()` | the readiness probe held the last SQL in a route |
| `bootstrap.py` → `database.connect()` | it called `sqlite3.connect` directly, bypassing the connection tuning, the directory creation and the backend check |
| `config.DATABASE_URL` / `DB_BACKEND` / `safe_database_url()` | recognise a PostgreSQL URL, refuse it explicitly, never log a password |

The `email_tokens` rebuild is the only non-additive migration and it is the one
table where that costs nothing: its rows are 15-minute one-time codes, so the
effect is that a pending OTP must be re-requested. Verified against a copy of
the real database — every other table's row count unchanged
(users 18, reports 72, activity_logs 20, profiles 13/3).

A `postgres://` URL now fails fast with a message naming
`POSTGRESQL_READINESS.md`, instead of silently writing to a local SQLite file
nobody is backing up.

**Bug found and fixed on the way:** `bootstrap.startup_checks()` referenced an
undefined global `app`. In production — its only branch — it raised
`NameError` instead of verifying that session cookies are marked Secure, so a
genuine safety check had never run. It now takes the application.

---

## 5. Final architecture audit

```
ROUTE → SCHEMA → AUTHORIZATION → SERVICE → UNIT OF WORK → REPOSITORY
      → DATABASE → SERIALIZER/DTO → API
```

| Check | Result | Evidence |
|---|---|---|
| No circular dependencies | pass | all 54 modules import cleanly in isolation, one subprocess each |
| No import-time side effects | pass | importing the package opens no database, spawns no thread |
| No direct DB access in routes | pass | `test_routes_and_services_never_open_a_connection` |
| No SQL outside the data layer | pass | `test_no_sql_outside_the_data_layer` |
| No hidden commits | pass | `.commit()` exists only in `unit_of_work`, `database.migrate` and `bootstrap` (its own connection) |
| No privacy bypass | pass | 21 privacy-contract + 8 versioned-privacy tests |
| No authorization bypass | pass | 183 security tests + 30 versioned security-control tests |
| No blocking SMTP in the request path | pass | timed with a 750 ms transport; thread identity checked |
| No duplicated API-v1 business logic | pass | same view-function object for all 38 endpoints |
| Scoring interface isolated | pass | no CV import server-side; no implementation named in routes/services |
| DB-specific concerns isolated | pass | `?`, `PRAGMA`, `AUTOINCREMENT`, `COLLATE NOCASE` confined to `database.py` + repositories |

---

## 6. Verification

### Tests — 471 passing, 0 failures

| Suite | Tests | |
|---|---|---|
| `test_security.py` | 183 | authn/authz, CSRF, rate limits, sessions, tokens |
| `test_api_v1.py` | 46 | **new** — namespace parity, versioned privacy and security |
| `test_scoring.py` | 44 | **new** — contracts, trust boundary, provider swap, isolation |
| `test_foundation.py` | 35 | app factory, errors, route registration (2 added) |
| `test_repositories.py` | 32 | data layer |
| `test_performance.py` | 25 | N+1 equivalence, pagination |
| `test_postgres_readiness.py` | 23 | **new** — containment, surrogate key, backend config |
| `test_schemas.py` | 23 | **new** — field semantics, schema contract, DTOs |
| `test_jobs.py` | 21 | **new** — dispatchers, timed request path, boundary enforcement |
| `test_privacy_contract.py` | 21 | the pinned privacy boundary |
| `test_unit_of_work.py` | 18 | transaction boundary |

Started at 312, added 159, **all 471 green**. One existing test was updated:
`test_public_api_contract_is_unchanged` now allows the intended `/api/v1`
additions — and was made *stricter*, asserting both directions of the mirror.

### Performance — no regression

Counting `sqlite3.Connection` subclass around the real app, 201 users /
3,800 reports:

| Request | Queries | Phase 2.4 baseline |
|---|---|---|
| `/api/state` (athlete) | 16 | 16 |
| `/api/v1/state` (athlete) | 16 | — |
| `/api/state` (coach) | 16 | 16 |
| `/api/state` (owner) | 16 | 16 |
| `/api/coach/athletes` | 11 | 11 |
| `/api/v1/coach/athletes` | 11 | — |
| `/api/admin/users` | 13 | 13 |

Routing the serializers through repositories issues the same SQL. The versioned
paths cost exactly what the legacy paths cost.

`/api/my/reports` (30) and `/api/export` (36) scale with the caller's report
count: both call `report_public` per row, which looks up `ai_results` per
report. That is the pre-existing N+1 Phase 2.4 fixed only inside
`scoped_state`; `/api/my/reports` is bounded by pagination, `/api/export` is
not. Listed as remaining debt below, unchanged by this phase.

### Frontend — working

Real browser against the real app (throwaway database, demo roster seeded):

* **Athlete:** login → dashboard → reports (6 seeded reports render) →
  performance analysis panel; new assessments posted through the app's own API
  layer appear ("8 assessments").
* **Coach:** login (role mismatch correctly rejected with the Phase 1 403 when
  the wrong role is selected) → coach dashboard → `/coach/athletes` returns 12
  athletes with **no `email` and no `phone`** — the privacy boundary holding in
  the live app.
* **Owner:** login → owner panel renders → `/admin/metrics`, `/admin/users`,
  `/admin/activity` all 200; owner *does* see contact details, as designed.
* **Full flow over both namespaces:** signup → verify → login → message →
  rating → notification → report → export → password change → authenticated
  reads. All 200; no 500 anywhere in the server log.
* **Trust boundary, live:** a report posted with
  `scoring: {trusted: true, provider: "server-verified"}` came back
  `{"provider": "client-submitted", "trusted": false, "confidence": null}`.

No console errors originate from the application. The only console errors are
the sandbox blocking external CDN assets (Google Fonts, Chart.js, jsPDF), which
is an artifact of the test environment, not of the app.

---

## 7. Remaining technical debt

1. **`/api/export` has an unbounded N+1** — one `ai_results` query per report,
   with no pagination. Rate limited to 10/hour per user, so it is a latency
   issue rather than an availability one. Pre-existing; a batch loader
   (`reports.ai_results_for_athlete`) already exists to fix it.
2. **`/api/state` payload is still 0.8–1.3 MB** at 201 users. The query count
   was solved in 2.4; the size needs the frontend off bulk hydration, which is
   a frontend change.
3. **The job queue is in-process and not durable.** A deploy or crash loses
   queued mail; there is no retry and no dead-letter queue. The adapter is
   clean, so a broker is a one-class change.
4. **The rate limiter is process-local** (~N× allowance under N workers,
   counters reset on deploy). Unchanged by instruction; documented.
5. **`bootstrap.py` still writes SQL directly** (demo seeding and owner
   provisioning). It is startup/CLI code, but it is the one module outside
   `repositories/` that a PostgreSQL port would also have to touch.
6. **Routes still read through repositories directly** for simple list
   queries, rather than through a service. Deliberate and pre-existing: those
   endpoints have no business rules to enforce beyond authorization.
7. **Timestamps are naive local-time ISO strings.** Correct today because
   ISO-8601 sorts lexicographically and one process writes them; §3.3 of
   `POSTGRESQL_READINESS.md` explains why this must be decided before any
   engine move.

---

## 8. Phase 3 recommendations

In the order that buys the most per unit of risk:

1. **Get the frontend off `/api/state`.** Every per-resource endpoint it needs
   already exists and is paginated. This removes the largest payload in the
   system and the last reason the whole user table is serialized per request.
2. **Server-side scoring.** The interface, the trust flag and the version field
   are in place and tested with a fake provider. A real one can be introduced
   behind `SCORING_PROVIDER`, scoring a sample of submissions and comparing
   against the client's numbers before anything is marked trusted.
3. **Durable job queue**, if mail volume or a second web process justifies it —
   the same swap also gives the rate limiter a shared backend.
4. **PostgreSQL**, following §5 of `POSTGRESQL_READINESS.md`: port the data
   layer behind the existing backend switch, run the suite against both engines
   in CI, then move timestamps as a separate step.
5. **Calibration**, which is a data-collection milestone rather than an
   architectural one, and is what would let any score be called trusted.
