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
- Signup now sends a 6-digit email verification code (shown in the terminal + a prompt, since email is in DEV mode).

## Logins
- Athlete : arjun@athletix.ai  / 1234
- Coach   : coach@athletix.ai  / 1234
- Owner   : owner@athletix.ai  / archita.1905   (secret, full platform data)
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
