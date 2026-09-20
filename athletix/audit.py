# -*- coding: utf-8 -*-
"""Security audit log.

Append-only record of security-relevant events. Never raises (a failed audit
write must not break the request) and never stores credentials - `_safe_detail`
redacts anything that looks like one.
"""

import re

from flask import request

from athletix.database import now_iso
from athletix.request_info import client_ip


# Anything matching these never reaches the audit log, no matter which
# call site passes it in.
_SECRET_WORDS = ("password", "passwd", "pass_hash", "secret", "token",
                 "csrf", "otp", "code=", "authorization", "cookie")


def _safe_detail(detail):
    """Strip anything that looks like a credential out of an audit line."""
    text = str(detail or "")[:300]
    low = text.lower()
    for word in _SECRET_WORDS:
        if word in low:
            return "[redacted]"
    # Never let a bare 6-digit OTP land in the log either.
    return re.sub(r"\b\d{6}\b", "[redacted]", text)


def log_activity(user_id, action, detail=""):
    """Append a security-relevant event. Never raises, never logs secrets."""
    try:
        ua = ""
        ip = ""
        try:
            ip = client_ip()
            ua = (request.headers.get("User-Agent") or "")[:180]
        except Exception:
            pass
        # Uses the unit of work so an audit write can never commit somebody
        # else's half-finished transaction (Phase 2.1 finding B2). Called
        # standalone it commits its own row; called inside a business
        # operation it joins that transaction and shares its fate.
        from athletix.repositories import activity
        from athletix.unit_of_work import unit_of_work
        with unit_of_work():
            activity.append(user_id, str(action)[:60], _safe_detail(detail),
                            ip, now_iso(), ua)
    except Exception:
        pass
