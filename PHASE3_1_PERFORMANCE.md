# AthletixAI — Phase 3.1: API State + Frontend Data Optimization

**Date:** 2026-08-30 · **Scope:** end the frontend's dependence on bulk
`/api/state` hydration; remove the remaining measurable N+1s.

**Result: 510 tests passing, 0 failures. Dashboard hydration is 91–94%
smaller.** Every figure below was measured with the Phase 2.4 methodology — a
counting `sqlite3.Connection` around the real application — on a synthetic
**201-user / 3,800-report / 400-message** cohort. Nothing here is estimated.

---

## 1. The audit (3.1.1) — where the bytes actually were

Query counts were already fixed in Phase 2.4 (16 queries for `/api/state`).
The *size* was not, and the breakdown says why:

| `/api/state` component | athlete | coach | owner |
|---|---|---|---|
| **reports** | **907,833 B** (3,800 rows) | **1,425,693 B** | **1,425,693 B** |
| users | 24,500 B (201) | 24,500 B | 51,209 B |
| messages | 338 B | 4,593 B | 46,515 B |
| notifs | 290 B | 2 B | 39,555 B |
| ratings | 2,097 B (50) | 2,100 B | 1,993 B |
| leaders | 167 B | 167 B | 167 B |
| **total** | **935,301 B** | **1,457,131 B** | **1,565,208 B** |

**97% of the payload was the `reports` collection**, shipped so the browser
could derive five numbers per athlete in JavaScript — `latestOverall`,
`progressOf`, `weeklyDelta`, `pointsOf`, session count — plus a dozen platform
counters. A coach downloaded 1.4 MB of biomechanics to render a table of
twelve rows.

Baseline query counts (per request; ~10 of each is the per-connection floor —
7 PRAGMAs plus the session lookup):

| Endpoint | queries | bytes |
|---|---|---|
| `/api/state` (athlete / coach / owner) | 16 / 16 / 16 | 836,752 / 1,297,687 / 1,395,445 |
| `/api/export` | **36** | 12,499 |
| `/api/my/reports` | **30** | 6,728 |
| `/api/coach/athletes` | 11 | 10,394 |
| `/api/admin/users` | 13 | 23,011 |

`/api/export` and `/api/my/reports` were the two remaining N+1s: one
`ai_results` lookup per returned report.

## 2. What changed

### New endpoints (each mounted at `/api/…` **and** `/api/v1/…`)

| Endpoint | Replaces | Cost |
|---|---|---|
| `GET /directory/athletes` | the `users`+`reports` blocks, for every screen that only needed derived numbers | 12 queries, 63.9–99.1 KB |
| `GET /directory/coaches` | the `users`+`ratings` blocks for coach cards | 13 queries, 2.6 KB |
| `GET /summary/platform` | the browser-side scan of every report for dashboard counters | 16 queries (23 for operators), 0.8–2.8 KB |
| `GET /admin/coaches/<id>/ratings` | rating attribution, which used to be broadcast in `scoped_state` to operators | operators only |

All four go through `services/directory.py` → repositories → DTOs. No SQL in a
route, no business logic duplicated between the two namespaces.

### Query work moved into SQL
`reports.performance_series()` (one ordered query folded per athlete),
`reports.counters()`, `reports.average_latest_overall()`,
`reports.daily_counts()`, `reports.athletes_active_since()`,
`reports.recent_with_names()`, `ratings.aggregates()`,
`messages.sent_counts()`, `messages.parties_active_since()`.

### N+1 removed (3.1.3)
`reports.ai_results_for_reports(ids)` — one query per *page* instead of per
row — is used by `/api/my/reports` and `/api/coach/athletes/<id>`;
`/api/export` uses the existing `ai_results_for_athlete` batch.

### Frontend (`templates/index.html`)
* `hydrateFromServer()` no longer calls `/api/state`. It fetches
  `/directory/athletes`, `/directory/coaches`, `/summary/platform`,
  `/my/messages`, `/my/notifications` and — for athletes — `/my/reports`, in
  parallel.
* `DB.reports` now holds **the viewer's own reports only**. `DB.summary`
  holds one server-computed row per athlete.
* `latestOverall`, `progressOf`, `weeklyDelta`, `pointsOf` read the summary,
  and fall back to the local scan (which is what keeps offline mode working).
  New helpers: `latestReportOf`, `reportCountOf`, `lastActivityOf`,
  `platformSummary`, `myStarsFor`.
* A coach or owner opening one athlete's history fetches **that athlete's**
  reports on demand (`loadAthleteReports` → `/coach/athletes/<id>`), cached
  for the session.
* `ratingOf` reads the aggregate from the coach directory row; the browser no
  longer receives every rating on the platform.

`/api/state` is unchanged and still mounted. It is a compatibility endpoint,
not a dead one, and `test_api_state_still_works_unchanged` pins it.

## 3. Measured: before → after

### Dashboard hydration (what a login actually costs)

| Role | before | after | saving |
|---|---|---|---|
| athlete | 1 request, 836,752 B | 6 requests, **74,836 B** | **−91.1%** |
| coach | 1 request, 1,297,687 B | 5 requests, **83,572 B** | **−93.6%** |
| owner | 1 request, 1,395,445 B | 5 requests, **106,291 B** | **−92.4%** |

### Per-endpoint

| Endpoint | queries before → after | bytes before → after |
|---|---|---|
| `/api/export` | **36 → 17** | 12,499 (unchanged) |
| `/api/my/reports` | **30 → 11** | 6,728 (unchanged) |
| `/api/coach/athletes/<id>` | per-row `ai` lookup → one per page | unchanged |
| `/api/state` (athlete/coach/owner) | 16 (unchanged) | unchanged |
| `/api/coach/athletes`, `/api/admin/users`, `/api/admin/activity`, `/api/my/messages`, `/api/my/notifications` | unchanged | unchanged |

No existing query budget regressed.

### Honest accounting of query counts
Hydration is now 5–6 HTTP requests instead of 1, and each request pays the
10-query floor (7 PRAGMAs + session lookup) again. Totals:

| Role | queries before | after (total) | of which data queries |
|---|---|---|---|
| athlete | 16 | 72 | 12 (was 6) |
| coach | 16 | 60 | 10 (was 6) |
| owner | 16 | 68 | 18 (was 6) |

That is the trade: ~6 extra cheap queries and a few more round trips (issued
in parallel) in exchange for **1.2–1.3 MB less over the wire per login**. The
per-connection PRAGMA floor is a connection-per-request artifact, not new work
— removing it is a pooling change and was out of scope here.

### Scaling
`test_directory.py::TestQueryBudgets` asserts, by counting statements during
real requests, that the query count of `/api/my/reports`, `/api/export`,
`/api/coach/athletes/<id>`, `/api/directory/athletes` and
`/api/summary/platform` does **not** change when the underlying data grows.

## 4. Security and privacy verification (3.1.5)

The new endpoints derive numbers from other people's data, so each was checked
against the boundary `/api/state` already drew. `serializers.py` remains the
privacy boundary — every identity field still goes through `user_public`, and
every metric breakdown through `can_view_report_detail`.

| Guarantee | Test |
|---|---|
| Authentication required, in both namespaces | `test_authentication_is_required` (6 cases) |
| Athlete sees no foreign e-mail/phone/last_login | `test_athlete_sees_no_foreign_contact_details` |
| Athlete never sees another athlete's metric breakdown (`latestReport.m` is null) but does see the public `overall` | `test_athlete_never_sees_another_athletes_metric_breakdown` |
| Coach sees breakdowns, still no contact details | `test_coach_sees_metric_breakdowns_but_no_contact_details` |
| `myStars` is the caller's own rating and nobody else's | `test_coach_directory_shows_only_the_callers_own_stars` |
| Rating attribution is operator-only (athlete/coach → 403) | `test_coach_rating_attribution_is_operator_only` |
| Message counts and the activity feed are operator-only | `test_message_counts_are_operator_only`, `test_athlete_gets_no_operator_fields` |
| A private message never reaches an athlete's summary | `test_the_feed_is_not_reachable_by_an_athlete` |
| No internal columns anywhere in the new payloads | `test_directory_never_leaks_internal_columns` |

Pagination (3.1.4): every new collection is paginated through the existing
`page_params`/`page_meta` (default 200, clamped to 500), with a stable
`ORDER BY name, id` and full `page` metadata; hostile `?limit=`/`?offset=`
input falls back safely. Keyset paging was **not** introduced — offset paging
over one row per athlete is not the degradation case that justifies it.

## 5. Correctness: the numbers did not move

The strongest check is behavioural, run in a real browser against the running
app: for **all 12 athletes × 5 derived fields**, compare what the UI now reads
from the server against what the old code computed from the `/api/state`
payload.

```
athletesChecked: 12,  mismatches: 0
```

`services/directory.py` folds the series with the frontend's formulas,
including JavaScript's round-half-up (Python's `round` is half-even), which is
why `points` and `weekly` match exactly. `test_state_and_directory_agree_on_the_derived_scores`
pins the same equality in the suite.

## 6. Tests — 510 passing, 0 failures

| Suite | Tests |
|---|---|
| `test_security.py` | 183 |
| `test_api_v1.py` | 46 |
| `test_scoring.py` | 44 |
| `test_directory.py` | **39 (new)** |
| `test_foundation.py` | 35 |
| `test_repositories.py` | 32 |
| `test_performance.py` | 25 |
| `test_postgres_readiness.py` | 23 |
| `test_schemas.py` | 23 |
| `test_jobs.py` | 21 |
| `test_privacy_contract.py` | 21 |
| `test_unit_of_work.py` | 18 |

Started at 471, added 39. The route-contract test was updated to allow the
four intentional new endpoints (and still proves every Phase 1 route exists and
that each has a `/api/v1` twin).

## 7. Browser verification

Real browser, real server, demo roster seeded:

* **Athlete** — login, dashboard, reports, leaderboard (podium renders),
  progress, card. `DB.reports` holds **6** own reports instead of 72.
* **Coach** — All Athletes renders 12 rows with sessions/latest/progress/points
  while the client store holds **0** reports; recruiter, compare and analytics
  render; opening one athlete fetches exactly that athlete's 6 reports on
  demand.
* **Owner** — dashboard counters (72 total, 12 today, 36 in 7 days, avg 69.4,
  12 active today, 12 feed events, 14-day histogram), All Athletes (12 rows),
  All Coaches (3 rows with aggregate ratings and messages-sent), audit log.
* **Flows** — rating a coach (optimistic local update agrees with the server
  after re-hydration), messaging, notifications, report creation.
* The server access log for a full login shows **no `/api/state` request at
  all** — the dashboard no longer touches it.

Console errors are limited to the sandbox blocking external CDN assets
(Google Fonts, Chart.js, jsPDF), which is an artifact of the test environment.

**One pre-existing bug found and fixed while smoke-testing:** `showToast()`
declared `const t` on the line after calling the translation helper `t()`, so
every toast threw a TDZ `ReferenceError` and aborted whatever flow raised it
(rating a coach, saving a report). The local was renamed to `el`. This was in
the working tree before Phase 3.1 and is unrelated to it.

## 8. Remaining performance debt

1. **`/api/state` is still unbounded** — it remains the compatibility
   endpoint, and it still serializes every report for whoever calls it. It is
   no longer on the dashboard path; retiring it needs a deprecation window for
   any other consumer.
2. **`_summaries_by_athlete()` reads the whole report table server-side** (one
   query, folded in one pass). At ~4k reports that is 12 queries and ~10 ms;
   at 100k+ it will want a materialized per-athlete summary updated on write.
   Nothing above the repository would have to change.
3. **Directory pagination default is 200 (max 500).** A cohort larger than 500
   athletes would need the frontend to page; today it requests `limit=500` and
   the `page` metadata is there for when it must.
4. **Per-request PRAGMA floor (10 queries).** Hydration now pays it 5–6 times.
   A connection pool would remove it; that is an infrastructure change, not a
   query change.
5. **`/api/export` is still unbounded in rows** (it is an export), though it is
   now one `ai_results` query rather than one per report.
6. **Owner "All Coaches" shows `—` for experience** — the template reads
   `c.exp`, but the API field is `experience`. Pre-existing and unchanged by
   this phase; cosmetic, listed so it is not lost.
7. **Dead render functions** (`renderODash`, `renderCDash`, `renderADash` are
   each defined twice; the earlier definitions are overridden) still scan
   `DB.reports`. They are unreachable, so they were left alone rather than
   modified blind.
