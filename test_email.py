"""
test_email.py  —  AthletixAI email diagnostic

Run this in the SAME folder as app.py, with your .env in place:

    python test_email.py

It sends ONE real test email to yourself and prints exactly what Gmail says.
This tells us whether the problem is Gmail accepting/dropping the mail, or
something else. Nothing about your app changes — this is just a probe.
"""
import os
import sys
import ssl
import smtplib
from email.mime.text import MIMEText

# ── load .env the same way app.py does ──
def load_env():
    path = ".env"
    if not os.path.exists(path):
        print("!! No .env file found in this folder. Run from the project folder.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()

HOST = os.environ.get("SMTP_HOST", "")
PORT = int(os.environ.get("SMTP_PORT", "465"))
USER = os.environ.get("SMTP_USER", "")
PASS = os.environ.get("SMTP_PASS", "")
FROM = os.environ.get("MAIL_FROM", USER)

print("=" * 55)
print(" AthletixAI email test")
print("=" * 55)
print(" SMTP_HOST :", HOST)
print(" SMTP_PORT :", PORT)
print(" SMTP_USER :", USER)
print(" SMTP_PASS : %d characters" % len(PASS),
      "(should be 16)" if len(PASS) != 16 else "(ok)")
print(" MAIL_FROM :", FROM)
print("-" * 55)

if not HOST or not USER or not PASS:
    print("!! Missing SMTP settings in .env. Fix those first.")
    sys.exit(1)

# send the test to YOURSELF (the SMTP_USER inbox) — most reliable check
to_addr = USER
msg = MIMEText("If you can read this, AthletixAI email delivery works. "
               "Your reset codes will arrive the same way.", "plain")
msg["Subject"] = "AthletixAI email test"
msg["From"] = FROM
msg["To"] = to_addr

print(" Sending a test email to:", to_addr)
print(" (checking Gmail actually accepts and delivers it)")
print("-" * 55)

try:
    if PORT == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(HOST, PORT, timeout=20, context=ctx) as s:
            s.set_debuglevel(1)          # <-- prints the full Gmail conversation
            s.login(USER, PASS)
            s.sendmail(USER, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(HOST, PORT, timeout=20) as s:
            s.set_debuglevel(1)
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(USER, PASS)
            s.sendmail(USER, [to_addr], msg.as_string())
    print("-" * 55)
    print(" RESULT: Gmail ACCEPTED the email with no error.")
    print(" Now check the inbox of:", USER)
    print(" (and its Spam / All Mail). If it arrives there, delivery works")
    print(" and the reset-code problem is only the OTHER address.")
except smtplib.SMTPAuthenticationError as e:
    print("-" * 55)
    print(" RESULT: LOGIN REJECTED (error 535).")
    print(" -> The app password is wrong, OR 2-Step Verification is OFF.")
    print(" -> Turn on 2-Step Verification, make a NEW app password,")
    print("    put it in .env with NO spaces, and try again.")
    print(" detail:", e)
except Exception as e:
    print("-" * 55)
    print(" RESULT: could not send.")
    print(" detail:", repr(e))
    print(" -> If this is a timeout / getaddrinfo error, your network is")
    print("    blocking Gmail (common on college/office WiFi).")
    print("    Try a phone hotspot and run this again.")