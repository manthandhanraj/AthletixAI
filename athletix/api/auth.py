# -*- coding: utf-8 -*-
"""Authentication endpoints.

HTTP only: load the request schema, rate-limit, delegate to `services.auth`,
serialize. Business rules live in the service; session cookies and rate
limiting stay here because both are transport concerns.

Route paths are relative. `athletix/api/__init__.py` mounts this blueprint
twice - at `/api` and at `/api/v1` - so both namespaces run this exact
handler. There is no second copy of anything.
"""

from flask import Blueprint, jsonify, session

from athletix.audit import log_activity
from athletix.config import DEV_CODES
from athletix.schemas import requests as rq
from athletix.schemas.responses import ok
from athletix.security.http import ensure_csrf
from athletix.security.ratelimit import client_ip, limiter, too_many
from athletix.security.session import current_user, login_required, start_session
from athletix.serializers import user_public
from athletix.services import auth as auth_service
from athletix.validation import body

bp = Blueprint('auth', __name__)


@bp.get("/csrf")
def api_csrf():
    return jsonify(ok=True, token=ensure_csrf())


@bp.post("/auth/signup")
def signup():
    d = rq.SignupRequest.load(body())
    limited = too_many("signup", d["email"])
    if limited:
        return limited

    uid, email, code = auth_service.register(d)
    resp = ok(need_verify=True, email=email,
              message="Account created. Enter the 6-digit code we emailed "
                      "you to activate your account.")
    if DEV_CODES:
        resp["dev_code"] = code      # never in production (see DEV_CODES)
    return jsonify(resp)


@bp.post("/auth/verify")
def verify_email():
    d = rq.VerifyRequest.load(body())
    limited = too_many("verify", d["email"])
    if limited:
        return limited
    auth_service.verify_email(d["email"], d["code"])
    return jsonify(ok=True,
                   message="Email verified successfully. You can now log in.")


@bp.post("/auth/resend")
def resend_verification():
    """Answers identically whether or not the address exists, so it cannot be
    used to enumerate accounts."""
    d = rq.ResendRequest.load(body())
    limited = too_many("resend", d["email"])
    if limited:
        return limited

    code = auth_service.resend_verification(d["email"])
    if code and DEV_CODES:
        return jsonify(ok=True, message="Verification email sent.",
                       dev_code=code)
    return jsonify(ok=True, message="If that account exists and still needs "
                                    "verifying, a code is on its way.")


@bp.post("/auth/login")
def login():
    # The schema rejects a malformed address before a rate-limit slot is
    # consumed, which is the Phase 1 ordering: a flood of garbage addresses
    # must not be able to lock a real user out.
    d = rq.LoginRequest.load(body())
    email = d["email"]
    limited = too_many("login", email)
    if limited:
        log_activity(None, "login_rate_limited", email)
        return limited

    try:
        row = auth_service.authenticate(email, d["password"], d["role"])
    except auth_service.NeedsVerification as nv:
        # 403 with a need_verify flag: a response shape the generic error
        # handler does not model, so it is built here.
        resp = {"ok": False, "need_verify": True, "email": nv.email,
                "error": "Please verify your email address first. "
                         "Enter the 6-digit code we sent you."}
        if nv.dev_code and DEV_CODES:
            resp["dev_code"] = nv.dev_code
        return jsonify(resp), 403

    csrf = start_session(row, d["remember"])
    auth_service.note_login(row["id"])
    limiter.reset("login", "%s|%s" % (client_ip(), email))
    return jsonify(ok=True, user=user_public(row), csrf=csrf)


@bp.post("/auth/logout")
def logout():
    u = current_user()
    if u:
        log_activity(u["id"], "logout")
    session.clear()
    return jsonify(ok=True)


@bp.post("/auth/logout-all")
@login_required
def logout_everywhere():
    """Revoke every session for this account, on every device."""
    auth_service.revoke_all_sessions(current_user()["id"])
    session.clear()
    return jsonify(ok=True, message="Signed out on all devices.")


@bp.get("/session")
def get_session():
    u = current_user()
    if not u:
        return jsonify(ok=True, user=None, csrf=ensure_csrf())
    return jsonify(ok=True, user=user_public(u), csrf=ensure_csrf())


@bp.post("/auth/forgot")
def forgot_password():
    d = rq.ForgotPasswordRequest.load(body())
    limited = too_many("forgot", d["email"])
    if limited:
        return limited

    generic = "If the email exists, a reset code has been sent."
    code, sent = auth_service.request_password_reset(d["email"])
    if code and DEV_CODES:
        return jsonify(ok=True, message=generic, dev_code=code)
    if code and not sent:
        # The mail job was refused outright (queue saturated, or an inline
        # send failed): surface a real error rather than a fake "sent", or
        # the user waits forever for a code that is not coming.
        return jsonify(ok=False, error="We couldn't send the email right now. "
                                       "Please try again in a minute."), 502
    return jsonify(ok=True, message=generic)


@bp.post("/auth/reset")
def reset_password():
    d = rq.ResetPasswordRequest.load(body())
    # Rate limited *before* the code is checked: without this a 6-digit OTP
    # is a million guesses from an account takeover.
    limited = too_many("reset", d["email"])
    if limited:
        return limited

    auth_service.reset_password(d["email"], d["code"], d["password"])
    session.clear()
    return jsonify(ok=True,
                   message="Password reset successfully. You can now log in.")
