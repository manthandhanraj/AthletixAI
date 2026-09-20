# AthletixAI — Security Audit (Phase 1: Security Fortress)

**Audit date:** 2026-08-27
**Scope:** `app.py` (Flask + SQLite backend), `templates/index.html` (single-page frontend),
`static/sw.js`, deployment config (`Procfile`, `render.yaml`), secrets handling (`.env`, `.gitignore`).
**Method:** full manual source review of every route, serializer, auth path and client-controlled
input; git-history scan for committed secrets; frontend review for client-side trust.

**Line references are against the pre-fix `app.py` (1409 lines).**

---

## 0. Architecture as found

- **Backend:** single-file Flask app, SQLite (WAL), bcrypt password hashing, Flask signed-cookie
  sessions, double-submit CSRF cookie, SMTP e-mail with a "dev mode" console fallback.
- **Frontend:** one 4479-line HTML file. On boot it fetches `/api/csrf`, `/api/session`, then
  **`/api/state`, which hydrates a global in-memory `DB` object**. Every screen (leaderboard,
  coach dashboard, recruiter hub, owner panel, messages, notifications) is rendered **client-side
  from that single global object.** There is an offline `localStorage` fallback when the backend
  is unreachable.
- **Roles:** `athlete`, `coach`, `admin`, `owner` (DB `CHECK` constraint).
- **Consequence:** *authorization was almost entirely a frontend concern.* The server handed over
  the whole database and the browser decided what to draw. This is the root cause of most
  findings below.

---

## 1. Findings

### CRITICAL

| # | Vulnerability | File / location | Why it is dangerous |
|---|---|---|---|
| **C1** | **Public signup can create `admin` accounts** | `app.py:707` — `if role not in ("athlete","coach","admin")` | `POST /api/auth/signup {"role":"admin"}` creates a fully privileged, pre-verified admin. Instant privilege escalation from an unauthenticated request. Unlocks `/api/admin/metrics` and every admin-gated path. |
| **C2** | **`/api/state` dumps the entire database to any logged-in user** | `app.py:570` (`full_state`), `app.py:856` (`api_state`) | Returns **every** user row (e-mail, phone, photo, verified flag, created_at, last_login), **every** report, **every private message body between every pair of users**, **every** notification and every rating. Any athlete who signs up for a free account exfiltrates the whole platform, including minors' contact details (the product's stated users are 14–19 year olds). Mass BOLA + DPDP/GDPR-class personal-data breach. |
| **C3** | **Notification forgery to arbitrary users** | `app.py:1142` (`push_notification`) | `POST /api/notifications {"toId": <any id>, "text": "..."}` — `toId` is taken straight from the request body with no authorization check. Any user can inject arbitrary text into any other user's notification feed. Direct phishing channel ("Your account is suspended, click here"), plus spam/harassment. |
| **C4** | **E-mail verification is decorative** | `app.py:718` (`INSERT ... verified ... ,1,`), `app.py:821-824` (auto-verify on login) | Signup writes `verified=1` unconditionally, **and** login silently flips `verified=1` for anyone who is not. Result: an attacker can register an account under **someone else's e-mail address** and use it immediately. No account is ever proven to belong to its e-mail owner. |
| **C5** | **Password-reset OTP is brute-forceable; tokens stored in plaintext** | `app.py:889` (`reset_password`), `app.py:445` (`issue_token`), `app.py:470` (`consume_token`) | The reset OTP is 6 digits and `rate_limited()` is applied **only to login**. `/api/auth/reset` accepts unlimited guesses → full account takeover of any known e-mail address in minutes. Additionally, OTPs and long tokens are stored **in plaintext** in `email_tokens`, so any read-only DB exposure (backup, `/var/data` disk, SQL injection elsewhere) becomes account takeover. |
| **C6** | **Flask debug mode defaults to ON** | `app.py:1408` — `debug = os.environ.get("FLASK_DEBUG", "1") == "1"` | Running `python app.py` without `FLASK_DEBUG=0` starts the Werkzeug interactive debugger: full stack traces, source disclosure, and remote code execution via the debugger console if the PIN is obtained or disabled. The same env var also gates `SESSION_COOKIE_SECURE` (`app.py:143`), so a mis-set variable silently ships session cookies over plain HTTP. |

### HIGH

| # | Vulnerability | File / location | Why it is dangerous |
|---|---|---|---|
| **H1** | **Messaging accepts any recipient ID** | `app.py:1087` (`send_message`) | Only checks that the recipient row exists. Athlete → any athlete, any user → the `admin`/`owner` account, unlimited. No relationship model, no rate limit. Harassment, spam, and a direct channel to the privileged accounts. |
| **H2** | **Anyone can rate any coach** | `app.py:1113` (`rate_coach`) | No role check. A coach can rate themselves 5★ and a competitor 1★; admins and the owner can rate. Reputation system is trivially gamed. |
| **H3** | **Sessions cannot be revoked; password reset does not invalidate them** | `app.py:889` (`reset_password`), `app.py:963` (`change_password`), `app.py:594` (`current_user`) | Flask signed-cookie sessions with no server-side epoch. After a compromise, resetting the password **does not** log the attacker out — the single most important recovery control does not work. Same for password change, e-mail change and role change. |
| **H4** | **Blanket CSRF exemption for `/api/auth/*`** | `app.py:622-628` | `if request.path.startswith("/api/auth/"): return` skips CSRF for login, logout, signup, forgot, reset and verify — **and for every future endpoint added under that prefix**. Enables login-CSRF (victim silently logged into the attacker's account, so their subsequent uploads land in the attacker's history) and forced logout. The frontend already sends a valid token on these calls, so the exemption bought nothing. |
| **H5** | **Missing `SECRET_KEY` silently falls back to a random per-process key** | `app.py:117` — `SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32)` | Under `gunicorn --workers 3` each worker signs cookies with a *different* key, so sessions break at random — and the misconfiguration is invisible instead of fatal. A production deploy that forgets the variable ships in this state. |
| **H6** | **Weak password policy** | `app.py:508` — `valid_password` = `len(pw) >= 6` | 6 characters, no other checks, no length cap (bcrypt silently truncates at 72 bytes). Seeded demo accounts use `1234`, which is below even this bar. |
| **H7** | **No rate limiting outside login** | `app.py:513` (`rate_limited`), applied only at `app.py:812` | Unlimited: signup (mass account creation), `/api/auth/resend` and `/api/auth/forgot` (mail-bomb any victim's inbox from your server, wrecking sender reputation), `/api/auth/verify` (OTP brute force), `/api/messages`, `/api/ratings`, `/api/notifications`, `/api/profile/email/request`. |
| **H8** | **Admin/owner authorization exists only in the frontend** | `templates/index.html:2587` (`NAVS`), `app.py:856` | The owner panel ("All Athletes" with e-mails, "All Coaches", "Activity Log") is rendered *entirely in the browser* from `/api/state`. The server never distinguishes an owner from an athlete on that endpoint. Anyone can obtain the owner view's data with `curl`. |

### MEDIUM

| # | Vulnerability | File / location | Why it is dangerous |
|---|---|---|---|
| **M1** | No CSP, HSTS, Permissions-Policy or COOP | `app.py:611` (`security_headers`) | Only `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` are set. No restriction on script/connect origins, no HTTPS pinning, camera/mic left open to any embedded content. |
| **M2** | No global error handlers; several endpoints 500 on bad input | `app.py:722` `int(d.get("age") or 16)`, `app.py:1181` `int(d.get("experience") or 0)`, `app.py:1119` | `{"age":"abc"}` → uncaught `ValueError` → Flask's default 500 page (and a full traceback in debug). Non-JSON error bodies also break the frontend's `r.json()` handling. |
| **M3** | Unvalidated / desynchronised `sport` | `app.py:722` (signup, unvalidated), `app.py:935` (update, validated against a 5-item list) | Signup stores any attacker-supplied string as an athlete's sport. Meanwhile `profile_update` validates against `SPORTS` (5 sports) while the UI offers 34 — 29 sports silently fail to save (functional bug caused by inconsistent validation). |
| **M4** | Sensitive fields returned for every user | `app.py:533` (`user_public`) | `email`, `phone`, `verified`, `created_at`, `last_login` are serialized for **all** users, not just the viewer, and fed into `/api/state`. No DTO/field filtering by audience. |
| **M5** | Account enumeration | `app.py:781` (`resend`, 404 "No account found"), `app.py:988` (409 "already in use"), `app.py:714` (signup 409) | Confirms which e-mail addresses are registered. (`/api/auth/forgot` is correctly non-enumerating — that one was already right.) |
| **M6** | Weak profile-photo validation | `app.py:946` | Only `photo.startswith("data:image/")`. No MIME allowlist, no base64 validation, no decoded-size check, no magic-byte check. 500 KB of arbitrary attacker text can be stored per account and is later injected into the DOM as an image `src`. |
| **M7** | Login reveals an account's role | `app.py:818` | `"This account is registered as a coach"` is returned **after a correct password check but also on role mismatch**, disclosing role and existence to anyone who guesses an e-mail. |
| **M8** | No OTP attempt counter, no expired-token cleanup | `app.py:470` (`consume_token`) | Expired rows accumulate forever; a token can be guessed indefinitely (see C5). |
| **M9** | Owner-e-mail squatting | `app.py:1380` (`ensure_owner_account`) + `app.py:703` (signup) | Nothing stops a public signup on the configured `OWNER_EMAIL`. The bootstrap then rewrites that row's password on restart — a denial of the owner's own bootstrap identity and a confusing security state. |
| **M10** | Audit log written but never readable through an authorized endpoint | `app.py:311` (`log_activity`), no reader route | `activity_logs` is populated but the owner's "Activity Log" screen reconstructs a fake feed client-side from the global state dump. Real security events are invisible to the operator. |
| **M11** | `clean()` neutralizes `<`/`>` but not quotes; frontend interpolates unescaped | `app.py:495`; `templates/index.html:3223`, `:3286`, `:3255` | Stored-XSS surface is largely closed by the `<`/`>` stripping, but several render paths interpolate server strings into HTML without `esc()`, leaving attribute-context edge cases. |

### LOW

| # | Vulnerability | File / location |
|---|---|---|
| **L1** | `.gitignore` covers only `.env` and `athletixai.db` — no `__pycache__/`, `*.db-wal`, `*.db-shm`, `.env.*`, venvs. | `.gitignore` |
| **L2** | Rate limiter is an in-process dict — with `--workers 3` the effective limit is 3× the configured value, and it resets on every deploy. | `app.py:156` |
| **L3** | Backend `SPORTS` list (5) out of sync with the frontend (34). | `app.py:152` |
| **L4** | No `Cache-Control: no-store` on authenticated API responses. | `app.py:611` |
| **L5** | OTP codes are returned in the JSON response in dev mode. Correctly gated on `EMAIL_DEV_MODE`, but a production deploy that forgets `SMTP_HOST` would hand out reset codes over the API. | `app.py:751`, `:797`, `:877` |

---

## 2. Secrets / configuration review

**Result: no secret has ever been committed to this repository.**

- `git log --all` over every revision of every tracked file: `.env` has never been tracked;
  `.env.example` has only ever contained placeholders (`youremail@gmail.com`,
  `your-16-char-app-password`, `change-this-to-a-long-random-string`).
- `.gitignore` already blocks `.env` and `athletixai.db`. Verified with `git ls-files`.
- A **local, untracked `.env` does exist** on this machine and contains real values for the
  following **types** of secret (values deliberately not printed, quoted or logged anywhere in
  this audit):
  1. an **owner/admin account password** (`OWNER_PASSWORD`),
  2. an **SMTP application password** for the sending Gmail mailbox (`SMTP_PASS`),
  3. the sending mailbox address / SMTP username.
  These are correctly kept out of source control. **Recommendation:** if this project folder was
  ever zipped, shared, uploaded, or handed to anyone (including as an "archive" for review),
  treat all three as compromised and rotate them — Gmail app password revoked and reissued,
  owner password changed.
- `render.yaml` correctly uses `generateValue: true` for `SECRET_KEY` and sets `FLASK_DEBUG=0`.
  It does **not** set `SMTP_*`, so a production deploy currently runs in **e-mail dev mode**,
  which (see L5) would return OTP codes in API responses. This must be fixed before launch.
- `athletixai.db` is untracked but present locally and contains real bcrypt hashes and user rows.

---

## 3. Database review

- **All SQL is parameterized.** Every `execute()` call in `app.py` uses `?` placeholders.
  The one dynamic fragment is `app.py:1190` (`"UPDATE coach_profiles SET " + ",".join(fields)`),
  where `fields` is built from a **hardcoded allowlist** of column names — values still bound.
  **No SQL injection was found.**
- Foreign keys are declared and `PRAGMA foreign_keys = ON` is applied per connection.
- `users.email` is `UNIQUE COLLATE NOCASE`; `ratings` has a composite PK and a `CHECK` on stars;
  `role` has a `CHECK` constraint. Ownership columns (`reports.athlete_id`, `messages.to_id`,
  `notifications.user_id`) exist and are correct — the schema was fine, **the queries reading it
  were not scoped**.
- Gap: no server-side session store, no token-hash columns, no attempt counters.

## 4. Upload review

- There is **no server-side file upload**. Video analysis runs entirely in the browser
  (MediaPipe WASM); only *metadata* (`filename`, `size`, `source`) is POSTed to `/api/reports`
  (`app.py:1071`) and stored in `videos`. That metadata was unvalidated: any 200-char string,
  any float size, any `source` value.
- Profile photos are base64 data URLs stored in the `users.photo` column — see **M6**.
- `MAX_CONTENT_LENGTH = 8 MB` was already set (good), but 413 returned an HTML page.

## 5. Frontend review

- **No credentials or session tokens in `localStorage`.** The session lives only in the
  `HttpOnly` cookie. `localStorage` holds the offline demo dataset (`athletixai_v3`), the
  language choice, and a splash flag.
- Role is read from the server (`/api/session` → `CU.role`) and used **only** to choose which
  nav items and pages to render. That is legitimate — but it was the *only* enforcement point
  (see H8).
- The offline fallback (`DB.users.find(x => x.pass === pass)`) does a plaintext password
  comparison, but only against locally-seeded demo data; server-hydrated users never carry a
  `pass` field. Acceptable for the offline demo mode.
- Extensive `innerHTML` use with an `esc()` helper applied inconsistently (see M11).

---

## 6. Remediation order

1. **CRITICAL** — C1 role escalation, C2 global state exposure, C3 notification forgery,
   C4 e-mail verification, C5 OTP brute force + plaintext tokens, C6 debug default.
2. **HIGH** — H1 messaging authorization, H2 rating authorization, H3 session revocation,
   H4 CSRF exemption, H5 secret key, H6 password policy, H7 rate limiting, H8 server-side RBAC.
3. **MEDIUM** — M1 headers/CSP, M2 error handling, M3/M6 input validation, M4 DTO filtering,
   M5/M7 enumeration, M8 token hygiene, M9 owner squatting, M10 audit-log endpoint, M11 escaping.
4. **LOW** — L1–L5.
5. Automated security test suite covering every fix above.

*Post-fix results, the second audit pass and the remaining-risk register are recorded in
`SECURITY_REPORT.md`.*
