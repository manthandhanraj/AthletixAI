# Phase 3 — Turn ON Real Email (2 minutes)

Right now codes print in the terminal (DEV mode). To email them for real:

## Gmail (easiest, free)
1. Google Account → Security → turn on **2-Step Verification**.
2. Security → **App passwords** → create one → copy the 16-character password.
3. In this project, copy `.env.example` to a new file named **.env**
4. Fill it:
       SMTP_HOST=smtp.gmail.com
       SMTP_PORT=465
       SMTP_USER=youremail@gmail.com
       SMTP_PASS=that-16-char-app-password
       MAIL_FROM=AthletixAI <youremail@gmail.com>
5. Save, then run `python app.py` again.

Done — signup & password-reset codes now arrive in the user's inbox.
(If .env is missing or empty, the app safely stays in DEV mode.)

## Note
Never commit `.env` to GitHub. Add a line `.env` to your `.gitignore`.
