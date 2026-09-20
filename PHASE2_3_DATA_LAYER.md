# AthletixAI — Phase 2.3: Data Layer & Unit of Work

**Date:** 2026-08-29
**Predecessors:** `PHASE2_ARCHITECTURE_AUDIT.md` (2.1), `PHASE2_2_FOUNDATION.md` (2.2)
**Scope:** transaction atomicity (2.3A), repository layer (2.3B–D), regression (2.3E).
**Not in scope:** N+1 fixes, pagination, indexes, PostgreSQL, async jobs, service layer.

---

## 1. Executive summary

The Phase 2.1 finding **B1** — three security-critical operations split their credential/role
change and their session revocation across two separate commits — is fixed and covered by
failure-injection tests that were **proven to fail against the old code**.

A repository layer now owns all business data access: **85 direct database call sites across
the eight handler modules → 0**. The only SQL left outside the data layer is the health
endpoint's `SELECT 1` readiness probe.

| | Before 2.3 | After 2.3 |
|---|---|---|
| Transaction boundary | none | `unit_of_work()`, re-entrant |
| `rollback()` sites | **0** | 1 (the boundary itself) |
| Commits outside UoW/bootstrap/init | 12 | **0** |
| Handler DB call sites | 85 | **0** (+1 health probe) |
| Repositories | 0 | 6 (+ base), 49 methods |
| Tests | 237 | **287** (0 failed, 0 errors, 0 skipped) |
| Privacy boundary | — | **6/6 functions still AST-identical to the monolith** |

---

## 2. The transaction atomicity problem

`reset_password`, `change_password` and `admin_set_role` each did this:

```python
db.execute("UPDATE users SET pass_hash=? WHERE id=?", ...)
db.commit()          # ← transaction 1 ends here
bump_epoch(user_id)  # ← opens and commits transaction 2
```

A failure between the two commits leaves **the password changed and every pre-existing
session still valid** — precisely the guarantee Phase 1 added session revocation to provide.
For a role change it is worse in kind: new permissions in the database, old permissions still
live in issued cookies.

### Before / after

```
BEFORE                                  AFTER
──────                                  ─────
BEGIN                                   BEGIN
  UPDATE users SET pass_hash              UPDATE users SET pass_hash
COMMIT              ← window opens        UPDATE users SET sess_epoch+1
BEGIN               ← window closes     COMMIT      (or ROLLBACK — never half)
  UPDATE users SET sess_epoch+1
COMMIT
```

---

## 3. Unit-of-Work design (`athletix/unit_of_work.py`, 78 lines)

```python
with unit_of_work():
    users.update_password(uid, hashed)
    users.bump_session_epoch(uid)
# one COMMIT here, or one ROLLBACK if anything raised
```

Three design decisions, each forced by a real problem in this codebase:

**Re-entrant by depth.** Nested blocks join the outermost boundary instead of committing.
This matters because `bump_epoch()` and `log_activity()` legitimately commit when called
standalone but are *also* called from inside larger operations. Without nesting they would
split the transaction again and reintroduce B1.

**Only the outermost block commits or rolls back.** An exception in a nested block propagates
outward, so exactly one rollback happens. There is no partial-commit path.

**Depth is restored on the failure path**, so a rolled-back request leaves a usable connection
(covered by `test_depth_is_restored_after_a_failure`).

Honest framing: sqlite3 already opened a deferred transaction on the first mutating statement.
The guarantee added here is not "we introduced transactions" — it is **"we stopped ending them
in the middle of a business operation."**

### Independent committers removed

| Was committing on its own | Now |
|---|---|
| `bump_epoch()` | joins the caller's transaction |
| `log_activity()` | joins the caller's transaction (Phase 2.1 finding **B2**) |
| `issue_token()` / `consume_token()` (4 sites) | join if nested; unchanged standalone |

All eight token call sites were verified to be standalone today, so this changed no current
behaviour — it removed the hazard for future code. **Commits outside the unit of work,
bootstrap and `init_db` are now zero.**

---

## 4. Failure-injection tests (18 tests)

The happy path was already green *before* this phase; it was the failure path that was broken,
so that is what these test.

| Scenario | Assertion |
|---|---|
| Credential write succeeds, epoch update raises | password **unchanged**, epoch unchanged, old password still works |
| Exception raised *after* both mutations are staged | neither persists |
| Role change: epoch update raises | role **unchanged**, target keeps un-elevated session, still 403 on admin routes |
| Constraint violation mid-block (`role='wizard'`) | earlier write in the same block discarded |
| Nested block followed by outer failure | **neither** inner nor outer row persists |
| `log_activity` inside a failing transaction | audit row discarded with the rest |
| `log_activity` standalone | commits normally |
| Depth tracking through success and failure | balanced; connection reusable afterwards |

**These tests were validated against the broken code.** I temporarily restored the
two-transaction pattern in `reset_password` and re-ran them; they failed with exactly the
right diagnosis:

```
AssertionError: PASSWORD PERSISTED despite failed revocation
assert '$2b$04$iP.mX...' == '$2b$04$MH./w...'
```

The fix was then restored and the suite returned to green. A regression test that has never
been seen to fail is not evidence.

### Session-revocation policy — unchanged

Explicitly re-tested, because the refactor touches exactly this machinery:
password change revokes **other** devices but keeps the acting tab; password reset revokes
**every** session; role change revokes the target's sessions.

---

## 5. Repository architecture (2.3B)

```
HTTP ROUTE          athletix/api/*.py        thin; no SQL
    ↓
UNIT OF WORK        athletix/unit_of_work.py transaction boundary
    ↓
REPOSITORIES        athletix/repositories/   all SQL lives here
    ↓
DATABASE            athletix/database.py     connection + schema

SERIALIZERS         athletix/serializers.py  privacy boundary — separate axis
```

**The rules, enforced by tests, not just documented:**

- Repositories **never** commit or roll back — `test_no_repository_module_calls_commit_or_rollback`
  greps the package, and `test_a_repository_write_is_undone_by_a_rollback` proves it behaviourally.
- Repositories **never** make authorization decisions (verified: no `current_user`,
  `roles_required` or `is_privileged` anywhere in the layer).
- Repositories **never** shape a response. They return raw `sqlite3.Row` objects — deliberately
  including `pass_hash` and `sess_epoch` — which is exactly why routes must keep passing them
  through the serializers.
- Dynamic column names go through **explicit allowlists** that raise `ValueError`
  (`update_athlete_profile_field`, `update_coach_profile`), parametrized against injection
  attempts like `"sport; DROP TABLE users"`.

| Repository | Lines | Responsibility |
|---|---|---|
| `users.py` | 140 | users, athlete/coach profiles, consents |
| `reports.py` | 58 | assessments, AI results, video metadata |
| `messages.py` | 48 | direct messages, conversation lookup |
| `notifications.py` | 26 | notification feed |
| `ratings.py` | 24 | coach ratings (upsert) |
| `activity.py` | 18 | audit trail |
| `base.py` | 29 | connection resolution; **no `commit()`** |

---

## 6. Handler migration (2.3C / 2.3D)

Migrated in three verified batches, full suite green between each — never all 85 at once.

| Batch | Modules | Result |
|---|---|---|
| 1 | `audit`, `ratings`, `notifications` | green |
| 2 | `messaging`, `assessments` | green |
| 3 | `admin`, `account`, `profile`, `auth` | one failure, fixed (below) |

| Module | DB call sites before | after |
|---|---|---|
| `api/account.py` | 18 | 0 |
| `api/profile.py` | 17 | 0 |
| `api/auth.py` | 16 | 0 |
| `api/assessments.py` | 12 | 0 |
| `api/admin.py` | 7 | 0 |
| `api/messaging.py` | 6 | 0 |
| `api/notifications.py` | 5 | 0 |
| `api/ratings.py` | 4 | 0 |
| **Total** | **85** | **0** |

### Additional operations made atomic in passing

Not required by the brief, but they are multi-write operations that were already sharing a
single commit — wrapping them makes the boundary explicit rather than incidental:

- **signup** — user row + role profile row
- **assessment creation** — report + AI result + video metadata
- **send message** — message + recipient notification
- **rate coach** — rating + coach notification
- **e-mail change** — address update + session revocation

### One bug found and fixed during migration

`coach_profile` built `fields` as a list of SQL fragments (`"specialty=?"`) with a parallel
`params` list; the repository expects a column→value mapping. Caught immediately by
`test_coach_profile_response_reflects_the_write`. The handler now builds an `OrderedDict`
and the repository re-checks every key against its own allowlist — so a client key cannot
reach the SET clause even if the builder is later changed.

### Deliberately not migrated

| Module | Sites | Why |
|---|---|---|
| `serializers.py` | 12 | The privacy boundary. Moving data access *and* the privacy logic in one step is how a leak gets introduced. Its N+1 is Phase 2.4. |
| `security/tokens.py` | 9 | Security primitive with its own module; made UoW-aware instead. |
| `security/session.py` | 2 | Auth middleware, reads on every request. |
| `api/health.py` | 1 | `SELECT 1` readiness probe — a database liveness check, not business data access. |

---

## 7. Security regression results

**Privacy boundary: 6/6 functions still AST-identical to the pre-Phase-2 monolith** —
`user_public`, `report_public`, `can_view_report_detail`, `scoped_state`, `is_privileged`,
`leaderboard_leaders`. Not "equivalent"; byte-identical logic.

`may_message` is the one security function changed this phase. Its diff is exactly the data
access swap — every policy branch (self, privileged, athlete↔coach, reply-to-existing-thread)
is untouched:

```
-    prior = get_db().execute('SELECT 1 FROM messages WHERE from_id=? AND to_id=? LIMIT 1', ...)
-    return bool(prior)
+    return messages.has_written_to(recipient['id'], sender['id'])
```

**Live black-box run** against a copy of the real database: **59/59 checks passed** — auth
lifecycle, privacy scoping (no foreign e-mails / metrics / messages / notifications), IDOR,
RBAC, messaging policy, notification policy, rating policy, CSRF, coach and owner flows.

**183/183 Phase 1 security tests** pass, unmodified.

---

## 8. Full test results

| Suite | Tests | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|---|
| `test_security.py` (Phase 1) | 183 | 183 | 0 | 0 | 0 |
| `test_privacy_contract.py` | 21 | 21 | 0 | 0 | 0 |
| `test_foundation.py` (Phase 2.2) | 33 | 33 | 0 | 0 | 0 |
| `test_unit_of_work.py` (**new**) | 18 | 18 | 0 | 0 | 0 |
| `test_repositories.py` (**new**) | 32 | 32 | 0 | 0 | 0 |
| **Total** | **287** | **287** | **0** | **0** | **0** |

Baseline at the start of Phase 2.3 was 237/237. **+50 tests, 0 regressions.**

---

## 9. Frontend compatibility

`templates/index.html` was **not modified**. All 28 pages across athlete / coach / owner
render with **0 console errors and 0 CSP violations**. Full lifecycle exercised through the
UI's own API layer: signup → verification → login → report → message → rating → consent →
password change (still logged in on the acting tab, confirming the atomic path). API paths,
methods, status codes and response fields are unchanged.

---

## 10. Architectural quality check

| Check | Result |
|---|---|
| Repositories committing independently | none |
| Commits outside UoW / bootstrap / `init_db` | **0** |
| Raw SQL in the API layer | only `health.py` `SELECT 1` |
| `get_db()` in the API layer | only `health.py` |
| Repositories importing HTTP or serializers | none |
| Repositories making authorization decisions | none |
| Circular imports | **none** |
| Hidden commits / rollbacks | none — 1 rollback, in the boundary |

---

## 11. Known technical debt (carried forward, unchanged)

1. **`/api/state` N+1** — 10,603 queries and 2.1 MB at 518 users. Now isolated in
   `serializers.py` and `reports.list_all()`, so the fix lands in one place (Phase 2.4).
2. **No pagination** — `list_all()` methods are deliberately unbounded and flagged in-code.
3. **Missing indexes** — 5 identified from `EXPLAIN QUERY PLAN` (Phase 2.4).
4. **Synchronous SMTP in handlers** — up to ~40 s of blocking I/O (Phase 2.7).
5. **No service layer** — handlers are the use-case layer for now. They are thin on data
   access but still hold business rules (Phase 2.5).
6. **Rate limiter is process-local** — ~N× allowance under N workers.
7. **No `/api/v1`** — the blueprint boundary makes it cheap; not introduced to avoid touching
   the frontend contract.
8. `serializers.py` still calls `get_db()` directly; it should move onto repositories as part
   of the Phase 2.4 N+1 work, once the privacy tests can guard the change in isolation.

---

## 12. Phase 2.4 recommendation — performance

Now that all data access is behind repositories, the audit's performance findings can be
fixed in one place each:

1. **Kill the N+1 (finding A1).** Add `users.profiles_for(ids)` and
   `reports.ai_results_for(ids)` batch loaders; rewrite `scoped_state` to two batched queries
   instead of one-per-row. Expected: **10,603 → under 10 queries**. The 21 privacy-contract
   tests are the safety net — they must stay green through the rewrite.
2. **Add the 5 measured indexes (A4):** `reports(date)`, `messages(from_id, date)`,
   `notifications(user_id, date)`, `users(role, name)`, partial `reports(live)`.
   Re-run `EXPLAIN QUERY PLAN` to confirm each is actually used.
3. **Add pagination (A2)** to the `list_all()` methods and their endpoints, with a consistent
   envelope, keeping `/api/state` backward compatible for the current frontend.

Do the N+1 before pagination: the batch loaders change the shape the pagination has to work
with.
