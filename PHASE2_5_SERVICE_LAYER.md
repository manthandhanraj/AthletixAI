# AthletixAI — Phase 2.5: Service Layer

**Date:** 2026-08-30 · **Scope:** extract business rules from routes into services.
**Not in scope:** `/api/state` redesign, PostgreSQL, Redis, async, versioning.

---

## 1. Services created

| Service | Lines | Owns |
|---|---|---|
| `services/auth.py` | 236 | register, verify, resend, authenticate, password reset/change, session revocation |
| `services/profile.py` | 128 | profile/photo update, e-mail change request+confirm, consent, account deletion |
| `services/messaging.py` | 121 | `may_message` policy, send message, notifications, coach ratings |
| `services/assessment.py` | 84 | metric parsing, assessment creation, report authorization, athlete detail |
| `services/analytics.py` | 62 | platform metrics, public stats, audit trail, data export |
| `services/__init__.py` | 0 | — |
| **Total** | **639** | |

## 2. Business logic moved out of routes

- **Role allowlist on signup**, owner-email squatting block, e-mail-exists handling.
- **Credential workflows**: password reset/change including the atomic
  credential + `bump_epoch` transaction, unverified-login handling.
- **Messaging policy** (`may_message`) — was duplicated in the route module and the
  service during migration; now defined **once**, in the service.
- **Notification authorization** (self-only unless operator) and rating rules
  (athletes only, coach targets only, session-derived attribution).
- **Report authorization** (`get_report_for` — 404 not 403, so an id cannot be probed).
- **Assessment creation** including the one-transaction report + AI result + video write.
- **Export and metric aggregation**.

Services raise typed `AppError`s (`ValidationError`, `AuthorizationError`, `NotFoundError`,
`ConflictError`) rather than building responses, so a rule produces the same status and
message wherever it is called. Messages and status codes are unchanged from Phase 2.4.

Two things deliberately stayed in the route layer because they are transport concerns:
**rate limiting** (returns a `Response` with `Retry-After`) and **session cookies**
(`start_session`, `session.clear()`). A service reaching for `flask.session` would not be
testable in isolation.

`login` keeps one non-generic branch in the route: `NeedsVerification` produces a 403 with a
`need_verify` flag and an optional dev code — a response shape the generic error handler does
not model, and one the frontend depends on.

## 3. Route reduction

| Module | Before | After |
|---|---|---|
| `api/auth.py` | 334 | 174 |
| `api/profile.py` | 203 | 105 |
| `api/account.py` | 168 | 139 |
| `api/assessments.py` | 134 | 81 |
| `api/messaging.py` | 93 | 47 |
| `api/admin.py` | 87 | 79 |
| `api/notifications.py` | 67 | 44 |
| `api/ratings.py` | 61 | 34 |
| **Total** | **1,147** | **703** (−39%) |

Routes are now: parse → rate-limit → authorize (decorator) → service → serialize.

## 4. Dependency direction

```
api  →  services  →  repositories  →  database
                 ↘  serializers (privacy boundary)
```

Verified programmatically: **no circular imports**; **no repository imports a service or a
route**; **no service imports the api layer**. Services depend only on
`config, database, errors, audit, mail, validation, security.*, serializers, repositories,
unit_of_work`.

## 5. Transactions

Every multi-step mutation runs inside the existing `unit_of_work()`; services own the
boundary, repositories still never commit. **Commits outside `unit_of_work`/`bootstrap`/
`init_db`: 0** (unchanged from Phase 2.3). The Phase 2.3A atomicity guarantees survive — the
18 failure-injection tests were re-pointed at the service module (where `bump_epoch` is now
called from) and still pass, including the case that proves a failed revocation rolls back
the password.

## 6. Security / privacy verification

- **`serializers.py` was not modified in this phase** (mtime still Phase 2.4). The privacy
  boundary is untouched by the service extraction.
- **21/21 privacy-contract tests** and **183/183 Phase 1 security tests** pass.
- **Live black-box: 59/59** — RBAC, IDOR, messaging/notification/rating policy, CSRF,
  session revocation, auth lifecycle.
- Browser: athlete and coach both see **0 foreign e-mails and 0 foreign metrics**.

## 7. API compatibility

No route, method, request field, response field or status code changed. `templates/index.html`
was **not modified**. Full browser lifecycle verified: signup → `need_verify` → verify →
login → report (overall 82) → message → rating → consent → profile rename → password change
(still logged in on the acting tab).

## 8. Tests

**312 passed / 312** — 0 failed, 0 errors, 0 skipped. Same count as Phase 2.4: this phase
moved code rather than adding behaviour, and the existing suites are what prove the move was
behaviour-preserving.

| Suite | Tests |
|---|---|
| `test_security.py` | 183 |
| `test_privacy_contract.py` | 21 |
| `test_foundation.py` | 33 |
| `test_unit_of_work.py` | 18 |
| `test_repositories.py` | 32 |
| `test_performance.py` | 25 |

## 9. Incident during this phase

An automated unused-import cleaner I wrote used a regex over source blocks and stripped
parentheses from five files, corrupting `Blueprint('x', __name__)`, `@bp.get("/path")` and the
whole `RateLimiter` class. Caught immediately by the syntax check. Repaired: the four API
files by targeted pattern restoration, `security/ratelimit.py` by rewriting it from the
known-good monolith source plus the documented Phase 2.2/2.4 edits. The cleaner was then
rewritten to edit only AST import nodes. Full suite green afterwards; no behaviour changed.

## 10. Remaining debt

1. **`/api/state` payload is still 1.2–2.2 MB** at 518 users. Query count was solved in 2.4;
   size needs the frontend off bulk hydration. `scoped_state` is now the single clean
   aggregation point for that work.
2. **Synchronous SMTP** inside request handlers (up to ~40 s blocking). Now isolated behind
   `services/auth.py` and `services/profile.py`, so it is one call-site swap away from a job
   queue.
3. **Rate limiter is process-local** (~N× allowance under N workers).
4. **`serializers.py` still calls `get_db()` directly** rather than going through
   repositories.
5. No `/api/v1` namespace.
6. `bootstrap.py` and `validation.py` have a few pre-existing unused imports, left alone
   deliberately.

## 11. Phase 2.6 recommendation — Request/response schemas + API v1

The service layer now has stable, typed entry points, which is the precondition for:

1. **Request schemas**: replace ad-hoc `d.get(...)` parsing in routes with declared
   input schemas per endpoint, so validation lives in one place and is introspectable.
2. **Response DTOs**: formalise what each endpoint returns, keeping `serializers.py` as the
   privacy boundary underneath.
3. **`/api/v1` namespace** mounted alongside the existing paths, with `/api/*` retained as
   aliases so the frontend keeps working — the blueprint boundary from 2.2 makes this cheap.

Do schemas before versioning: versioning a contract you have not yet declared just freezes
the ambiguity.
