# AthletixAI — How to Run (VS Code)

## Setup (one time)
1. Open this folder in VS Code.
2. Terminal → New Terminal.
3. Install:
       pip install -r requirements.txt

## Run
       python app.py
Open:  http://127.0.0.1:5000
Stop with Ctrl + C.

## ✅ NOW FULLY CONNECTED (Phase 1 done)
The frontend now talks to the Flask backend + SQLite database:
- Accounts, reports, messages, ratings, coach profiles, consents → all saved on the SERVER (athletixai.db).
- Log in as a coach on one device and you can SEE an athlete's new reports/messages created on another device. Real multi-user.
- Signup sends a 6-digit email verification code (shown in the terminal + a
  prompt while email is in DEV mode). The account is created UNVERIFIED and
  CANNOT log in until that code is entered - this is enforced server-side.

## Logins (fresh database only)
Demo accounts are seeded ONLY into an empty database, and only outside
production. The seed password is `AthletixDemo!2026` (override with
SEED_PASSWORD). An existing athletixai.db keeps whatever passwords it has.
- Athlete : arjun@athletix.ai  / AthletixDemo!2026
- Coach   : coach@athletix.ai  / AthletixDemo!2026
- Owner   : the admin account is configured through environment variables
            (OWNER_EMAIL and OWNER_PASSWORD in your .env file), so no admin
            credentials live in this repository. Without them, no owner
            account is created at all.
- Or create a new account (verify with the code printed in the terminal).

## Offline safety
If the backend is NOT running (e.g. you open index.html directly), the app
automatically falls back to offline/localStorage demo mode so it never breaks.
When app.py IS running, it uses the real server automatically.

## Real email (optional, for launch)
Set these env vars before running to send real verification/reset emails:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM
Without them it stays in DEV mode (codes in terminal).

## Reset data
Delete athletixai.db and restart to reseed fresh demo data.

## Security tests
    pip install -r requirements-dev.txt
    pytest

183 tests cover authentication, RBAC, IDOR/BOLA, messaging/notification/
rating authorization, privacy scoping, CSRF, rate limiting, input validation,
upload validation and error handling. They run in a temporary database and
never touch athletixai.db or your .env.

Note: `test_email.py` in this folder is a MANUAL SMTP diagnostic, not a test -
importing it sends a real email. pytest.ini keeps it out of collection; run it
deliberately with `python test_email.py`.

## Before deploying
- Set SECRET_KEY (the app refuses to start in production without it).
- Set FLASK_DEBUG=0 (or APP_ENV=production).
- Configure SMTP_* — otherwise users cannot verify their email or reset a
  password in production.
- Set OWNER_EMAIL / OWNER_PASSWORD. The owner account can never be created
  through public signup.
See .env.example for the full annotated list.
