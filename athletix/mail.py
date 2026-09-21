# -*- coding: utf-8 -*-
"""Transactional e-mail: templates, SMTP transport, and the job hand-off.

`send_email()` is the TRANSPORT. It is synchronous by nature - it opens a
socket and talks SMTP, with a 20 second timeout across two port attempts - and
it stays that way, because that is what sending an e-mail actually is.

`queue_email()` is what application code calls. It hands the send to the job
boundary in `athletix/jobs/`, so no request handler waits on a mail server
(Phase 2.1 finding A3: up to ~40 seconds of blocking on signup, on login of an
unverified account, and on password reset).

The return value means "accepted", never "delivered":

    inline dispatcher (no SMTP configured) - the send ran; True = it worked
    background dispatcher                  - True = queued for delivery

Call sites are written against the weaker guarantee. The only caller that
looks at it at all is /auth/forgot, which must not answer "a code has been
sent" when the job was refused outright.
"""

import smtplib
import json
import urllib.error
import urllib.request
from email.utils import parseaddr
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from athletix.config import (BREVO_API_KEY, EMAIL_DEV_MODE, EMAIL_PROVIDER,
                             MAIL_FROM, SMTP_HOST, SMTP_PASS, SMTP_PORT,
                             SMTP_USER, TOKEN_TTL_MINUTES)


def _email_shell(title, body_html):
    return """<!DOCTYPE html><html><body style="margin:0;background:#0b0b10;
font-family:Arial,Helvetica,sans-serif;padding:28px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0"
 style="background:#15151c;border:1px solid #2a2a34;border-radius:14px;">
<tr><td style="background:#d90429;border-radius:14px 14px 0 0;padding:18px 28px;">
<span style="color:#ffffff;font-size:20px;font-weight:bold;
letter-spacing:1px;">ATHLETIX<span style="color:#ffd7d7;">AI</span></span></td></tr>
<tr><td style="padding:28px;color:#e8e9ee;font-size:14px;line-height:1.7;">
<h2 style="margin:0 0 12px;color:#ffffff;font-size:19px;">""" + title + """</h2>
""" + body_html + """
</td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #2a2a34;color:#8a8f9b;
font-size:11px;">AthletixAI &middot; AI Sports Talent Assessment Platform<br>
Owned by Archita Tripathi &amp; Manthan Dhanraj &middot; This is an automated
message, please do not reply.</td></tr>
</table></td></tr></table></body></html>"""


def _otp_block(code):
    return ("""<div style="background:#0b0b10;border:1px dashed #d90429;
border-radius:10px;padding:16px;text-align:center;margin:18px 0;">
<span style="font-size:30px;letter-spacing:10px;color:#ff5757;
font-weight:bold;">""" + code + """</span></div>
<p style="color:#8a8f9b;font-size:12px;">This code expires in """
            + str(TOKEN_TTL_MINUTES) + " minutes.</p>")


def email_welcome(name):
    return ("Welcome to AthletixAI", _email_shell(
        "Welcome aboard, " + name + "!",
        "<p>Your AthletixAI account has been created successfully.</p>"
        "<p>Upload your first training video to receive an AI performance "
        "report with Speed, Agility, Strength, Stamina and Technique scores "
        "&mdash; then climb the leaderboard.</p>"
        "<p><b>Discover. Analyze. Elevate. Dominate.</b></p>"))


def email_verify(name, code):
    return ("Verify your AthletixAI email", _email_shell(
        "Verify your email address",
        "<p>Hi " + name + ", use the code below to verify your email and "
        "activate your account:</p>" + _otp_block(code)))


def email_reset(name, code):
    return ("Reset your AthletixAI password", _email_shell(
        "Password reset requested",
        "<p>Hi " + name + ", we received a request to reset your password. "
        "Enter this code to continue:</p>" + _otp_block(code) +
        "<p>If you did not request this, you can safely ignore this email "
        "&mdash; your password will not change.</p>"))


def email_password_changed(name):
    return ("Your AthletixAI password was changed", _email_shell(
        "Password changed",
        "<p>Hi " + name + ", this is a confirmation that your account "
        "password was changed just now.</p>"
        "<p>If this was not you, reset your password immediately from the "
        "login screen.</p>"))


def email_account_created(name, role):
    return ("Your AthletixAI account is ready", _email_shell(
        "Account created",
        "<p>Hi " + name + ", your <b>" + role.title() +
        "</b> account is verified and ready to use.</p>"
        "<p>Log in anytime to analyze sessions, track progress and connect "
        "with the AthletixAI network.</p>"))


def queue_email(to_addr, subject, html):
    """Hand a transactional e-mail to the job boundary.

    This is the function application code should call. It returns as soon as
    the work is accepted; see the module docstring for exactly what "accepted"
    promises. It never raises: a user's password change must not fail because
    the confirmation notice could not be scheduled.
    """
    from athletix import jobs
    return jobs.dispatch(send_email, to_addr, subject, html)


def send_email(to_addr, subject, html):
    """Send an email synchronously; in dev mode, print to console instead.

    This is the transport, and it BLOCKS. Do not call it from a request
    handler - call `queue_email()`, which is the same work behind the job
    boundary.
    """
    if EMAIL_DEV_MODE:
        print("\n[EMAIL DEV MODE] To: %s | Subject: %s" % (to_addr, subject))
        return True

    if EMAIL_PROVIDER == "brevo":
        if not BREVO_API_KEY:
            print("[EMAIL FAILED] BREVO_API_KEY is not configured.")
            return False
        display_name, from_addr = parseaddr(MAIL_FROM)
        if not from_addr:
            print("[EMAIL FAILED] MAIL_FROM is not a valid email address.")
            return False
        payload = {
            "sender": {"email": from_addr, "name": display_name or "AthletixAI"},
            "to": [{"email": to_addr}],
            "subject": subject,
            "htmlContent": html,
        }
        request = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=json.dumps(payload).encode("utf-8"),
            headers={"accept": "application/json", "api-key": BREVO_API_KEY,
                     "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
            print("[EMAIL] sent to %s via Brevo API" % to_addr)
            return True
        except urllib.error.HTTPError as exc:
            # Brevo's response describes a configuration error but never
            # contains the API key, so logging it is safe and actionable.
            detail = exc.read().decode("utf-8", "replace")
            print("[EMAIL ERROR] Brevo API (%d) -> %s" % (exc.code, detail))
        except Exception as exc:
            print("[EMAIL ERROR] Brevo API -> %s" % exc)
        return False

    if EMAIL_PROVIDER != "smtp":
        print("[EMAIL FAILED] Unsupported EMAIL_PROVIDER: %s" % EMAIL_PROVIDER)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    # The envelope sender must be the actual authenticated mailbox, not the
    # display-name form in MAIL_FROM (Gmail rejects a mismatched envelope).
    envelope_from = SMTP_USER or MAIL_FROM

    # Try the configured port first; if SSL/STARTTLS fails, fall back to the
    # other Gmail port automatically. Every failure is printed in full so the
    # real reason is visible, not swallowed.
    attempts = []
    if SMTP_PORT == 465:
        attempts = [("ssl", 465), ("starttls", 587)]
    else:
        attempts = [("starttls", SMTP_PORT or 587), ("ssl", 465)]

    last_err = None
    for mode, port in attempts:
        try:
            if mode == "ssl":
                with smtplib.SMTP_SSL(SMTP_HOST, port, timeout=20) as server:
                    if SMTP_USER:
                        server.login(SMTP_USER, SMTP_PASS)
                    server.sendmail(envelope_from, [to_addr], msg.as_string())
            else:
                with smtplib.SMTP(SMTP_HOST, port, timeout=20) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    if SMTP_USER:
                        server.login(SMTP_USER, SMTP_PASS)
                    server.sendmail(envelope_from, [to_addr], msg.as_string())
            print("[EMAIL] sent to %s via %s:%d" % (to_addr, mode, port))
            return True
        except Exception as exc:
            last_err = exc
            print("[EMAIL ERROR] %s:%d (%s) -> %s" % (SMTP_HOST, port, mode, exc))
    print("[EMAIL FAILED] could not send to %s. Last error: %s" % (to_addr, last_err))
    return False
