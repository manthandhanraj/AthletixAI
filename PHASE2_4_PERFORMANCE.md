# AthletixAI — Phase 2.4: Performance

**Date:** 2026-08-29 · **Scope:** N+1 elimination, measured indexes, pagination.
**Method:** counting `sqlite3.Connection` subclass around the real app on a synthetic
**518-user / 10,072-report** dataset — same methodology as the Phase 2.1 audit.

---

## 1. Baseline → after

| Request | Queries before | after | Size before | after | Time before | after |
|---|---|---|---|---|---|---|
| `/api/state` (athlete) | 537 | **16** | 1255.0 KB | 1255.0 KB | 51 ms | 38 ms |
| `/api/state` (coach) | 10,603 | **16** | 2152.6 KB | 2152.6 KB | 134 ms | 57 ms |
| `/api/state` (owner) | 10,603 | **16** | 2207.9 KB | 2207.9 KB | 134 ms | 62 ms |
| `/api/coach/athletes` | 522 | **11** | 55.9 KB | 11.0 KB¹ | 7 ms | 2 ms |
| `/api/admin/users` | 526 | **13** | 112.3 KB | 22.2 KB¹ | 7 ms | 3 ms |
| `/api/export` (athlete) | 22 | 22 | 2.3 KB | 2.3 KB | 3 ms | 0 ms |

**`/api/state`: 10,603 → 16 queries (663×).** Of those 16, 7 are the per-connection
`PRAGMA` statements from `_tune_connection` and 2 are session/auth lookups — so **~7 actual
data queries** for the whole payload.

¹ Size reduction on these two is pagination (default 100 of 518 rows), not lost data.
`/api/state` payload sizes are **byte-identical**, which is the evidence that semantics
did not change.

---

## 2. 2.4A — N+1 elimination

**Cause:** `user_public()` issued one profile query per user and `report_public()` one
`ai_results` query per detailed report. For a coach (detail on all 10,072 reports) that was
10,072 + 518 lookups.

**Fix:**

| Batch loader | Scope |
|---|---|
| `users.athlete_profiles_all()` / `coach_profiles_all()` | All profiles. Safe wholesale: sport/age/location and specialty/bio/city are directory fields `user_public()` already exposes to every viewer — not a widening. |
| `reports.ai_results_all()` | Coach/admin/owner only — exactly the set they may see in detail. |
| `reports.ai_results_for_athlete(id)` | Athlete viewers. The subquery encodes the scope, so an athlete never loads another athlete's verdicts, and it avoids an `IN` list with one parameter per report (`SQLITE_MAX_VARIABLE_NUMBER`). |

`serialize_users(rows, viewer)` batches the profile lookups; the **privacy decision is still
made per row by `user_public`**, which is unchanged. `user_public(row, viewer, profile=…)` and
`report_public(row, detail, ai=…)` take optional pre-loaded rows via an `_UNSET` sentinel, so
every existing call site (and the privacy-contract tests) still takes the original path.
`leaderboard_leaders()` now reuses the rows `scoped_state` already fetched instead of
re-querying (−2 queries).

**Equivalence is asserted, not assumed:** `test_performance.py` proves
`serialize_users(rows, viewer) == [user_public(r, viewer) for r in rows]` for athlete, coach,
owner, admin and `viewer=None`, and that a pre-loaded `ai` produces identical output to the
lookup — including the missing-profile-row case.

---

## 3. 2.4B — Five indexes, each with EXPLAIN evidence

| Query | Plan before | Index added | Plan after | Used |
|---|---|---|---|---|
| `reports ORDER BY date` | `SCAN reports; USE TEMP B-TREE FOR ORDER BY` | `idx_reports_date(date)` | `SCAN reports USING INDEX idx_reports_date` | ✅ |
| `messages WHERE to_id=? OR from_id=?` | **full `SCAN messages`** + temp B-tree | `idx_messages_from(from_id, date)` | `MULTI-INDEX OR` using both arms | ✅ |
| `notifications WHERE user_id=? ORDER BY date DESC` | index used, `USE TEMP B-TREE FOR ORDER BY` | `idx_notifs_user_date(user_id, date)` | `SEARCH … USING INDEX idx_notifs_user_date` | ✅ |
| `users WHERE role=? ORDER BY name` | index used, `USE TEMP B-TREE FOR ORDER BY` | `idx_users_role_name(role, name)` | `SEARCH … USING INDEX idx_users_role_name` | ✅ |
| `COUNT(*) FROM reports WHERE live=1` | `SCAN reports` | `idx_reports_live(live) WHERE live=1` (partial) | `SEARCH … USING COVERING INDEX` | ✅ |

**All five verified in use; every temp B-tree sort eliminated.** No speculative indexes were
added, none removed. They live in `SCHEMA` as `CREATE INDEX IF NOT EXISTS`, and `init_db()`
runs `SCHEMA` on every start, so existing databases pick them up with no separate migration.

---

## 4. 2.4C — Pagination

`athletix/pagination.py`: `page_params()` (clamped, default 100, max 500; hostile input falls
back to the default rather than 400-ing) and `page_meta()`.

**Contract is additive** — the collection keeps its original key and shape; a `page` object is
added alongside. Existing clients are unaffected.

```json
{"ok": true, "reports": [...],
 "page": {"limit":100,"offset":0,"total":1234,"returned":100,
          "has_more":true,"next_offset":100}}
```

| Endpoint | Paginated |
|---|---|
| `/api/my/reports`, `/api/my/messages`, `/api/my/notifications` | ✅ |
| `/api/coach/athletes`, `/api/coach/athletes/<id>` (reports) | ✅ |
| `/api/messages/<other_id>` | ✅ |
| `/api/admin/users`, `/api/admin/activity` | ✅ |
| **`/api/state`** | **deliberately not** — the dashboard hydrates from the complete scoped set; paginating it would break the frontend. Tracked as Phase 2.5+ frontend work. |

Every paginated query carries the primary key as a tiebreaker, so offset paging is
deterministic. A test walks all pages and asserts **no duplicates and no lost rows**, and
another asserts paging does not widen scope (a paged `/api/my/messages` still returns only
the caller's conversations).

---

## 5. Tests

| Suite | Tests |
|---|---|
| `test_security.py` (Phase 1) | 183 |
| `test_privacy_contract.py` | 21 |
| `test_foundation.py` | 33 |
| `test_unit_of_work.py` | 18 |
| `test_repositories.py` | 32 |
| `test_performance.py` (**new**) | 25 |
| **Total** | **312 passed / 312 · 0 failed · 0 errors · 0 skipped** |

Baseline entering 2.4 was 287. **+25 tests, 0 regressions.**

## 6. Security regression

- **Live black-box: 59/59** against a copy of the real database — privacy scoping, IDOR,
  RBAC, messaging/notification/rating policy, CSRF, session revocation, auth lifecycle.
- **183/183** Phase 1 tests unmodified; **21/21** privacy-contract tests green.
- Batch loading did not bypass authorization: an athlete's `/api/state` still shows
  **0 foreign e-mails, 0 foreign metrics, 0 foreign AI verdicts** (a dedicated test injects an
  AI verdict on another athlete's report and asserts it never appears).

## 7. Frontend

All 28 pages across athlete / coach / owner render; **0 console errors, 0 CSP violations**;
charts intact; owner audit feed renders 8 live rows. Data volumes unchanged (19 users,
72 reports on the dev dataset). `templates/index.html` was **not modified**.

## 8. Remaining performance debt

1. **`/api/state` payload is still 1.2–2.2 MB** at 518 users. Query count is solved; response
   size is not, and it cannot be without changing the frontend's bulk-hydration model.
   This is now the dominant cost (~57 ms is mostly serialization, not I/O).
2. **`/api/export` is 22 queries** and unbounded — a per-user export, small today, but it
   should become a background job (Phase 2.7) rather than be paginated.
3. **Synchronous SMTP** in request handlers (up to ~40 s blocking) — Phase 2.7.
4. **Rate limiter is process-local** — ~N× allowance under N workers.
5. Offset pagination degrades on very deep offsets; keyset paging is the upgrade if any
   collection grows past ~10⁵ rows.

## 9. Next phase recommendation — 2.5 Service layer

Handlers are now thin on data access but still hold business rules. Extract services
(`auth`, `assessment`, `messaging`, `report`, `analytics`) so routes become
validate → authorize → call service → serialize. That is also the natural place to move
`/api/state` toward per-resource hydration, which is the only way to fix remaining debt #1.
