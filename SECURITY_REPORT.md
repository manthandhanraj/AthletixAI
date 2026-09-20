# AthletixAI — Phase 1 Security Report (post-fix, second audit)

**Date:** 2026-08-28
**Companion document:** `SECURITY_AUDIT.md` (the pre-change audit and severity register)
**Verification:** 183 automated tests, live black-box attack sweep, browser verification,
migration testing against both a clean database and a copy of the real development database.

---

## 1. Second audit — BEFORE vs CURRENT

| # | Severity | Vulnerability | Status | Evidence |
|---|---|---|---|---|
| C1 | CRITICAL | Public signup could create `admin` accounts | **FIXED** | `PUBLIC_ROLES = ("athlete","coach")` allowlist at `app.py:1494`. 10 parametrized role payloads rejected; live sweep `signup role=admin/owner` → 400. |
| C2 | CRITICAL | `/api/state` dumped the entire database to any user | **FIXED** | `full_state()` deleted; `scoped_state()` filters by identity and role. Live: athlete sees **0** other users' emails, **0** other athletes' metrics, only own messages/notifications. |
| C3 | CRITICAL | Notification forgery to arbitrary `toId` | **FIXED** | Self-only for normal users, operators may broadcast. Live sweep: notify victim → 403, notify admin → 403, **0** rows injected into the victim's feed. |
| C4 | CRITICAL | E-mail verification was decorative | **FIXED** | Signup inserts `verified=0`; login auto-verify removed. Live: signup → `need_verify`, login before verify → 403, after verify → 200. |
| C5 | CRITICAL | Reset OTP brute-forceable; tokens stored in plaintext | **FIXED** | SHA-256 hashes only, `attempts` counter (5), expiry, one-time use, constant-time compare, rate limited. Test asserts the plaintext code never appears in the DB. |
| C6 | CRITICAL | Flask debug defaulted ON | **FIXED** | `DEBUG_MODE` requires explicit `FLASK_DEBUG=1`. Verified: production config starts with `secure=True hsts=True dev_codes=False`; `FLASK_DEBUG=1` + production → refuses to start. |
| H1 | HIGH | Messaging accepted any recipient id | **FIXED** | `may_message()` policy. Live: athlete→athlete 404, athlete→admin 404, athlete→owner 404, athlete→coach 200. |
| H2 | HIGH | Anyone could rate any coach | **FIXED** | Athletes only, coach targets only, attribution from session. Forged `athleteId`/`byId` recorded against the attacker, not the victim. |
| H3 | HIGH | Sessions could not be revoked | **FIXED** | `users.sess_epoch` + per-request check. Reset/change/role-change/logout-all all revoke. Test: attacker's live session dies the moment the victim resets. |
| H4 | HIGH | Blanket CSRF exemption for `/api/auth/*` | **FIXED** | Prefix exemption removed; constant-time compare + Origin/Referer check. All six auth endpoints tested for rejection without a token. |
| H5 | HIGH | Missing `SECRET_KEY` fell back to a random per-process key | **FIXED** | Fatal in production (verified: refuses to start, and refuses a key < 32 chars). Local runs persist `.dev_secret_key` (gitignored). |
| H6 | HIGH | 6-character password policy | **FIXED** | 8–72 chars, common-password and low-entropy rejection. Applied to signup, reset, change **and** the owner bootstrap. |
| H7 | HIGH | Rate limiting on login only | **FIXED** | 14 buckets, two-tier (per-subject + per-IP). The per-IP tier is what actually stops mass signup — found and fixed during testing. |
| H8 | HIGH | Admin/owner authorization existed only in the frontend | **FIXED** | `roles_required()` on every admin route; `/api/admin/users`, `/api/admin/activity`, `/api/admin/metrics` all 403 for athlete and coach. |
| M1 | MEDIUM | No CSP/HSTS/Permissions-Policy | **FIXED** | Full CSP built from the app's real asset origins. **Zero CSP violations** in the browser across all 28 pages. |
| M2 | MEDIUM | No error handlers; 500s on bad input | **FIXED** | Handlers for 400/401/403/404/405/413/429 + catch-all with a correlation id. Verified: no `Traceback`, `sqlite3`, `SELECT`, `app.py`, or filesystem paths in any error body. |
| M3 | MEDIUM | Unvalidated / desynchronised `sport` | **FIXED** | 34-sport allowlist shared with the frontend; unknown sport → 400. |
| M4 | MEDIUM | Sensitive fields returned for every user | **FIXED** | `user_public(row, viewer)` — contact details only for self and operators. |
| M5 | MEDIUM | Account enumeration | **FIXED** | `resend`, `forgot`, and e-mail-change now return identical generic responses. |
| M6 | MEDIUM | Weak photo validation | **FIXED** | MIME allowlist + strict base64 + decoded size cap + **magic-byte** check. 7 hostile payloads rejected. |
| M7 | MEDIUM | Login revealed an account's role | **FIXED** | Role mismatch is only reported *after* the password verifies. |
| M8 | MEDIUM | No OTP attempt counter / token cleanup | **FIXED** | `attempts` column, expired-row purge on issue. |
| M9 | MEDIUM | Owner-e-mail squatting | **FIXED** | Signup on `OWNER_EMAIL` → 403. |
| M10 | MEDIUM | Audit log never readable | **FIXED** | `/api/admin/activity`, owner/admin only, with redaction. |
| M11 | MEDIUM | Inconsistent output escaping | **PARTIALLY FIXED** | `clean()` strips control chars + angle brackets; `esc()` applied to the highest-risk render paths. See remaining risks. |
| L1 | LOW | Minimal `.gitignore` | **FIXED** | Now covers `.env*`, `.dev_secret_key`, all DB files/WAL/SHM, `__pycache__`, venvs. |
| L2 | LOW | In-process rate limiter | **DOCUMENTED** | `RateLimiter` is a documented, swappable interface. See deferred items. |
| L3 | LOW | Sports list out of sync | **FIXED** | Backend is now authoritative with all 34 sports. |
| L4 | LOW | No cache headers on API responses | **FIXED** | `Cache-Control: no-store, private` on `/api/*`. |
| L5 | LOW | OTP codes in dev-mode responses | **FIXED** | `DEV_CODES` requires dev mode **and** non-production. Verified `False` under production config. |

**Fixed: 30 of 31. Partially fixed: 1 (M11). Unresolved: 0.**

---

## 2. Issues found and fixed *during* verification

These were not in the original audit — they were found by testing the fixes.

1. **Rate-limiter design flaw (HIGH).** Buckets were keyed on IP+subject, so every new e-mail
   address got a fresh allowance and mass registration was still unlimited. Fixed with a
   two-tier scheme (per-subject *and* per-IP). Caught by `test_signup_is_rate_limited`.
2. **`as_int` truncated non-integral floats (MEDIUM).** `stars: 2.5` silently became a 2-star
   rating. Now rejected.
3. **Leaderboard broke under the new scoping (functional).** `leaderboardExtras()` read every
   athlete's metrics. Rather than re-exposing that data, the three badges are now computed
   server-side (`leaderboard_leaders()`) — the feature is identical, the exposure is three names.
4. **Stale-read regression from my own session caching (functional).** `current_user()` memoizes
   per request, so handlers that wrote to the user row echoed pre-update values. Fixed with
   `reload_user()`; regression tests added.
5. **JavaScript syntax error** from an apostrophe in a patched string — the whole page failed to
   parse. Fixed and now guarded by a `node --check` pass over the inline script.
6. **`test_email.py` would be collected by pytest**, sending a real e-mail through the owner's
   Gmail and aborting the run. Fixed with `pytest.ini` (`testpaths = tests`).
7. **`.dev_secret_key` was not gitignored** — a session-signing key I introduced could have been
   committed. Fixed.
8. **Owner-email change silently destroys the previous owner account** (pre-existing, present in
   committed `HEAD`). Behaviour kept — removing the stale privileged account is the
   security-conservative choice — but it now prints an explicit warning.
9. **18-minute test suite.** A security suite nobody runs is not a control. `BCRYPT_ROUNDS` is
   now overridable for tests and **clamped to ≥ 12 whenever a production signal is present**
   (three tests assert it cannot be weakened). Suite: 18m39s → 5s.

---

## 3. Verification performed

**Automated:** 183 tests — 183 passed, 0 failed, 0 errors, 0 skipped. Also run at the production
bcrypt factor (174 tests at the time) with identical results, and stress-run 15× consecutively
with zero flakes.

**Live black-box attack sweep** (34 probes as an ordinary authenticated athlete): every
identifier-manipulation attempt was blocked, and the three probes that legitimately return 200
were verified *by database effect* — the victim's row was unmodified, forged `athleteId`/`byId`
values were recorded against the attacker, and zero notifications were injected. `PUT`/`PATCH`/
`DELETE` on report and message resources return 405 (no such surface exists).

**Privacy:** response bodies inspected directly, using the victim's real e-mail and phone number
as canaries. `/api/state`, `/api/me`, `/api/my/*`, `/api/export`, `/api/public/stats` all clean —
no password hashes, no `$2b$` strings, no token or OTP material, no other user's contact details.

**Browser:** all 28 pages across athlete, coach and owner render with **zero console errors and
zero CSP violations**. Chart.js, jsPDF, Google Fonts and MediaPipe all load under the enforcing
CSP. Full signup → verification → login → report → message → rating → logout lifecycle exercised.

**Migration:** clean database boots, seeds and serves. A copy of the real development database
migrates with every user, report, profile, ownership relation and password hash preserved;
`PRAGMA integrity_check` = ok, `foreign_key_check` = empty. **The real `athletixai.db` was never
written to** — all destructive testing used copies.

---

## 4. Remaining risks (honest register)

1. **`/api/state` is still a bulk endpoint (MEDIUM-LOW).** It is now fully scoped and leaks
   nothing a user may not see, but it returns the whole authorized dataset in one response.
   The narrower per-resource endpoints exist and are authorization-checked; migrating the
   frontend onto them is a Phase 2 change.
2. **Coaches can see every athlete's performance data (accepted by design).** There is no
   coach↔athlete relationship model in the schema — the product is an open scouting platform.
   Coaches get performance data but **not** contact details. Introducing a real roster/consent
   model is a product decision for Phase 2.
3. **Athlete-submitted metrics are trusted (MEDIUM, integrity not confidentiality).** Scoring
   happens client-side in MediaPipe, so a crafted `POST /api/reports` can inflate leaderboard
   position. Values are clamped 0–100 and attribution cannot be forged. Server-side scoring was
   explicitly out of scope for this phase.
4. **Rate limiting is per-process (documented).** With `--workers 3` the effective allowance is
   ~3× and counters reset on deploy. Not a defence against a distributed attacker.
5. **Output escaping is defence-in-depth, not complete (M11).** `clean()` neutralises angle
   brackets and control characters on write, and `esc()` covers the high-risk render paths, but
   the frontend still uses `innerHTML` widely with inconsistent escaping. The CSP cannot fully
   compensate because ~64 inline `onclick` handlers force `'unsafe-inline'` in `script-src`.
6. **Production e-mail is unconfigured.** `render.yaml` now declares the SMTP variables as
   `sync: false`, but until they are set in the dashboard, production users cannot verify an
   address or reset a password. OTP leakage is *not* a risk (force-disabled in production).
7. **Local `.env` secrets should be rotated.** No secret was ever committed (verified across all
   history). But the working tree's `.env` holds a real Gmail app password and owner password —
   if this folder was ever zipped or shared, rotate both.

---

## 5. Deferred to Phase 2

- Move the frontend off `/api/state` onto the per-resource endpoints.
- Shared rate-limit backend (Redis) behind the existing `RateLimiter` interface.
- Server-side or signed/attested AI scoring to close the metric-integrity gap.
- Coach↔athlete relationship/consent model.
- Remove inline `onclick` handlers so the CSP can drop `'unsafe-inline'`.
- Structured logging/alerting on the audit trail.
- 2FA for owner/admin accounts.
