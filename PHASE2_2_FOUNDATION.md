# AthletixAI — Phase 2.2: Foundation & Safe Modularization

**Date:** 2026-08-29
**Predecessor:** `PHASE2_ARCHITECTURE_AUDIT.md` (Phase 2.1)
**Scope:** application factory, configuration boundary, import-time side effects,
health endpoints, error taxonomy, initial route/module extraction.
**Not in scope:** N+1 fixes, pagination, indexes, PostgreSQL, async jobs, service layer.

---

## 1. Executive summary

The 2,836-line `app.py` monolith was decomposed into a 27-module `athletix` package.
`app.py` is now a **75-line WSGI entrypoint**. Every line of security-critical logic was
moved **verbatim** and then verified by AST comparison against the pre-refactor file:
**42 of 45 security-critical functions are AST-identical**, and the 3 that differ do so
only by the additions this phase deliberately made.

| | Before | After |
|---|---|---|
| `app.py` | 2,836 lines | **75 lines** |
| Backend modules | 1 | 27 (`athletix/`, 3,432 lines) |
| Routes | 43 | **45** (43 unchanged + 2 health) |
| Tests | 183 | **237** (0 failed, 0 errors, 0 skipped) |
| Import-time DB mutation | yes | **no** |
| Health endpoint | none | `/health`, `/health/ready` |
| Circular imports | n/a (single file) | **none** |

Two real regressions were introduced and caught by verification, not by luck:
the PWA asset routes, and an import cycle. Both are fixed and covered by tests.

---

## 2. Before architecture

```
app.py (2,836 lines, 106 functions, 43 routes)
├── config globals (45 module-level constants)
├── email templates + synchronous SMTP
├── serializers (the privacy boundary)
├── auth / CSRF / rate limiting / headers
├── 43 route handlers  ── 87 inline SQL calls
└── seed / migrate / owner bootstrap   ← ran on import
```

## 3. After architecture

```
app.py (75 lines)                    WSGI entrypoint + public name re-exports
athletix/
├── __init__.py        create_app() factory, hook + blueprint registration
├── config.py          every environment-derived setting, one place
├── errors.py          exception taxonomy + HTTP handlers
├── database.py        SCHEMA, connections, migrations, init_db()
├── request_info.py    client_ip() — request-derived facts
├── validation.py      input validation and normalisation
├── serializers.py     viewer-aware serialization (THE privacy boundary)
├── mail.py            e-mail templates + SMTP transport
├── audit.py           security audit log
├── bootstrap.py       explicit startup tasks (seed, owner, safety checks)
├── security/
│   ├── passwords.py   bcrypt hashing
│   ├── tokens.py      hashed one-time OTPs
│   ├── session.py     session epoch, login_required, roles_required
│   ├── ratelimit.py   two-tier limiter
│   └── http.py        CSRF, body-size guard, security headers + CSP
└── api/               10 blueprints, 45 routes
    ├── health.py      liveness + readiness          (NEW)
    ├── pages.py       /, /privacy, /terms, PWA assets
    ├── auth.py        signup, verify, login, logout, reset
    ├── profile.py     self-service profile management
    ├── assessments.py reports + coach athlete views
    ├── messaging.py   direct messages + policy
    ├── notifications.py
    ├── ratings.py
    ├── account.py     /api/state, /api/me, exports, consent
    └── admin.py       operator endpoints
```

**Dependency layering** (verified acyclic):
`config → {database, validation, mail, errors, request_info, passwords} → {audit, tokens,
ratelimit, serializers} → {session, http} → bootstrap → api`.

The package is named `athletix`, not `app`, because a package named `app` would shadow
`app.py` and break `gunicorn app:app`. The Procfile, `render.yaml` and every deployment
doc keep working untouched.

---

## 4. Application factory

```python
create_app(config_overrides=None, initialize=True)
```

- Builds the Flask app with `template_folder`/`static_folder` pinned to the project root.
- Applies `config.flask_settings()`, then any overrides.
- Registers request hooks in order: body-size guard → CSRF → (handler) → security headers.
- Registers error handlers and the `close_db` teardown.
- Runs startup tasks **only** when `initialize=True`.

`initialize=False` yields an app object that has never touched a database — which is what
makes isolated unit testing possible for the first time.

## 5. Configuration boundary

All 45 constants moved to `athletix/config.py`, exposed as module-level names so ~200
call sites needed only an import change, not a rename. A **parity snapshot**
(`tests/_baseline_constants.json`, captured from the monolith before the move) is asserted
by `test_constants_match_the_pre_refactor_baseline`: **37 constants and all 8 Flask
settings verified identical**. Environment-derived values (`DB_PATH`, `SECRET_KEY`,
`OWNER_*`, `SEED_DEMO`, `BCRYPT_ROUNDS`) are excluded, since they legitimately differ per run.

## 6. Import-time side-effect removal (audit finding D1)

Previously `import app` ran `_load_dotenv()`, `seed()`, `ensure_owner_account()` and
`startup_checks()` — creating and migrating a database, provisioning an owner, and
potentially raising `RuntimeError`.

Now those live in `athletix/bootstrap.initialize()`, called only by `create_app()`.
Proven by subprocess tests that assert the database file **does not exist** after
importing `athletix`, `athletix.serializers`, `athletix.security.session`,
`athletix.api.auth`, `athletix.mail` and `athletix.bootstrap`.

`config.py` still reads the environment at import — that is a pure read, no mutation.

## 7. Health endpoints

| Route | Purpose | Behaviour |
|---|---|---|
| `GET /health` | liveness | `{"status":"ok"}`, touches nothing |
| `GET /health/ready` | readiness | `SELECT 1`; `503 {"status":"unavailable"}` on failure |

Deliberately split so a database hiccup cannot fail the liveness probe. Neither is
authenticated (a load balancer cannot log in), neither mutates state (asserted by
comparing `activity_logs` count before/after), and neither leaks paths, versions,
hostnames or configuration.

## 8. Error taxonomy

`athletix/errors.py` adds `AppError` + `ValidationError`, `AuthenticationError`,
`AuthorizationError`, `NotFoundError`, `ConflictError`, `RateLimitError`,
`PayloadTooLargeError`, and the `ErrorCode` vocabulary.

**Backward compatibility was the constraint.** The body keeps the exact Phase 1 shape
`{"ok": false, "error": "..."}` that the frontend already parses; a machine-readable
`code` is *added*. No message, status code or field was renamed or removed.

The taxonomy revealed a real gap: `login_required` and `roles_required` returned bespoke
JSON, bypassing the handlers, so 401/403 responses carried no code. They now raise typed
errors with **byte-identical messages and statuses**. `csrf_protect`, `limit_body_size`
and `too_many` gained a `code` field (the 429 keeps its `Retry-After` header).

The HTML 404 path still returns the SPA shell — client-side routing depends on it.

## 9. The `user_public` privacy boundary

Treated as the highest-risk part of the phase. Sequence was **behaviour first**:

1. Wrote `tests/test_privacy_contract.py` — **21 characterization tests** pinning the
   exact observable behaviour: self view, athlete→athlete, athlete→coach, coach→athlete,
   admin/owner, `viewer=None`, missing profile rows, NULL photo/phone, and the
   never-serialize list (`pass_hash`, `password`, `sess_epoch`).
2. Confirmed they pass against the **unmodified** monolith.
3. Only then moved `serializers.py`.
4. Confirmed `user_public`, `report_public`, `can_view_report_detail`, `scoped_state`,
   `is_privileged` and `leaderboard_leaders` are **AST-identical** to the originals.

The known N+1 defect inside these functions was left in place on purpose: changing data
access and moving the privacy boundary in the same step is how a leak gets introduced.
That is Phase 2.4 work.

## 10. Security regression results

Verified three independent ways.

**AST comparison vs the pre-refactor monolith** — 42/45 security-critical functions
byte-identical, including `user_public`, `scoped_state`, `current_user`, `consume_token`,
`issue_token`, `validate_photo`, `may_message`, `signup`, `login`, `reset_password`,
`admin_set_role`. The 3 differences are exactly the additive `code` field and the typed
error conversion, shown by diff.

**Test suite** — 183/183 Phase 1 security tests still pass, unmodified.

**Live black-box run** against a copy of the real database — **59/59 checks passed**:

| Area | Result |
|---|---|
| Auth lifecycle (signup → blocked pre-verify → verify → login → reset → revocation → logout) | 8/8 |
| Privacy scoping (no foreign emails / metrics / messages / notifications) | 4/4 |
| IDOR (own report 200, foreign report 404) | 2/2 |
| RBAC (admin 403, self-promote 403, signup-as-admin rejected) | 3/3 |
| Messaging policy (athlete→athlete 404, →owner 404, →coach 200) | 3/3 |
| Notifications (arbitrary recipient 403, self 200) | 2/2 |
| Ratings (non-coach 404, coach 200, coach-cannot-rate 403) | 3/3 |
| CSRF (missing 403 + code, cross-origin 403) | 2/2 |
| Coach + owner flows incl. audit log and role change | 13/13 |

## 11. Full test results

| Suite | Tests | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|---|
| `tests/test_security.py` (Phase 1) | 183 | 183 | 0 | 0 | 0 |
| `tests/test_privacy_contract.py` (new) | 21 | 21 | 0 | 0 | 0 |
| `tests/test_foundation.py` (new) | 33 | 33 | 0 | 0 | 0 |
| **Total** | **237** | **237** | **0** | **0** | **0** |

Baseline at the start of Phase 2.2 was 183/183. **+54 tests, 0 regressions.** Runtime 6.8s.

## 12. Browser / API smoke test

Clean process on port 5101 against a copy of the production database; migration ran on boot.

- **API matrix: 59/59 passed** (see §10).
- **Browser:** all **28 pages** across athlete / coach / owner render; **0 console errors,
  0 CSP violations**; Chart.js, jsPDF and 23 fonts load.
- Full UI lifecycle: signup → `need_verify` → verify → login → report → message → rating,
  dashboard re-renders correctly after writes.
- Fresh-account empty state still shows `—`/`0`, never fabricated numbers.
- Owner audit feed renders 8 real rows from `/api/admin/activity`.

The only console error is the service-worker registration failure, previously A/B-proven
(CSP report-only vs enforcing) to be an artifact of the embedded browser pane, not the app.

## 13. Regressions found and fixed during this phase

1. **PWA assets 404'd.** `send_from_directory("static", ...)` resolves relative to the
   blueprint root, which became `athletix/` after the move, so `/manifest.json` and
   `/sw.js` broke — and the SPA 404 handler masked it as an HTML response. Fixed to use
   `current_app.static_folder`; covered by `test_static_rooted_assets_are_served`.
2. **Import cycle** `errors → audit → security.ratelimit → errors`. `audit` depended on
   the rate limiter only for `client_ip`, which is the wrong direction. Extracted
   `athletix/request_info.py`. Graph is now acyclic.

## 14. Frontend compatibility

`templates/index.html` was **not modified** in this phase (last touched during the
dashboard redesign; confirmed by mtime and `git diff`). All 15 endpoints the frontend
calls are unchanged in path, method, status codes and response fields. The added `code`
field is additive and ignored by the existing client, which reads `data.error`.

## 15. Remaining technical debt

Carried forward from the audit, all deliberately untouched:

1. **`/api/state` N+1** — 10,603 queries and 2.1 MB at 518 users (Phase 2.4).
2. **No pagination** — 29 unbounded collection queries (Phase 2.4).
3. **Missing indexes** — 5 identified from `EXPLAIN QUERY PLAN` (Phase 2.4).
4. **Split transactions** — `reset_password`, `change_password`, `admin_set_role` commit
   the credential/role change and the session revocation separately. Still the highest
   priority correctness defect (Phase 2.3 unit-of-work).
5. **Synchronous SMTP in handlers** — up to ~40 s of blocking I/O (Phase 2.7).
6. **Business logic still in routes** — 87 SQL call sites remain in handlers; this phase
   organised them, it did not thin them (Phase 2.3/2.5).
7. **Rate limiter is process-local** — ~N× allowance under N workers.
8. **No `/api/v1`** — the blueprint boundary makes it cheap, but versioning was not
   introduced to avoid touching the frontend contract.

## 16. What was intentionally NOT changed

Schema (SCHEMA string verified byte-identical), SQLite as the engine, AI/CV behaviour,
report content, the frontend, all Phase 1 security semantics and rate-limit values, every
API path/method/status, and `render.yaml` / `Procfile` / `.env.example` / `.gitignore`.

## 17. Recommended Phase 2.3

**Data layer + unit of work.**

1. `athletix/database/unit_of_work.py` — an explicit transaction boundary, then convert
   `reset_password`, `change_password` and `admin_set_role` so the credential/role change
   and `bump_epoch()` commit **atomically**. This is a security-correctness fix, and it is
   the single highest-value item outstanding.
2. Introduce repositories (`user`, `report`, `message`, `notification`, `rating`,
   `activity`) and move the 87 in-handler SQL call sites behind them — one repository per
   step, full suite green between each.
3. Add repository-level tests.

Doing 2.3 before 2.4 matters: the N+1 fix should land in one place (the repository), not
be scattered across handlers.
