# -*- coding: utf-8 -*-
"""Request guards and response hardening: body size, CSRF, security headers.

The CSP is built from the sources this application actually loads, verified
against templates/index.html. 'unsafe-inline' is required in script-src
because the UI uses inline event handlers plus one large inline <script>; a
nonce does not cover event-handler attributes. The policy still blocks
arbitrary script hosts, restricts where data can be sent, forbids framing,
and locks down base-uri/form-action.
"""

import os
import secrets

from flask import jsonify, request, session

from athletix.config import CSP_REPORT_ONLY, IS_PRODUCTION, MAX_JSON_BYTES
from athletix.errors import ErrorCode


# The CSP is built from the sources this application actually loads, verified
# against templates/index.html:
#   cdnjs.cloudflare.com  - Chart.js, jsPDF
#   cdn.jsdelivr.net      - MediaPipe tasks-vision (ES module + WASM)
#   storage.googleapis.com- MediaPipe pose model asset (fetched at runtime)
#   fonts.googleapis.com / fonts.gstatic.com - Google Fonts
#   api.qrserver.com      - QR code image on the athlete card
# 'unsafe-inline' is required in script-src because the UI uses ~64 inline
# onclick handlers plus one large inline <script>; a nonce does not cover
# event-handler attributes, and removing them would be a full UI rewrite
# (explicitly out of scope for this phase). The policy still blocks
# arbitrary external script hosts, restricts where data can be sent
# (connect-src), forbids framing, and locks down base-uri/form-action.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob: "
    "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    "img-src 'self' data: blob: https://api.qrserver.com",
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "connect-src 'self' blob: https://cdn.jsdelivr.net "
    "https://storage.googleapis.com",
    "frame-src 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "upgrade-insecure-requests" if IS_PRODUCTION else "",
])


CSP = "; ".join(p for p in CSP.split("; ") if p)


CSP_HEADER = ("Content-Security-Policy-Report-Only"
              if CSP_REPORT_ONLY else "Content-Security-Policy")


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = (
        "camera=(self), microphone=(), geolocation=(), payment=(), "
        "usb=(), magnetometer=(), gyroscope=(), interest-cohort=()")
    resp.headers.setdefault(CSP_HEADER, CSP)
    if IS_PRODUCTION:
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    # Personal data must never sit in a shared or browser cache.
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, private"
        resp.headers["Pragma"] = "no-cache"
    return resp


# The profile photo endpoint legitimately carries a base64 image, so it keeps
# the global MAX_CONTENT_LENGTH ceiling instead of the tighter JSON cap. Both
# API mounts are listed: the exemption must be identical in each namespace, or
# the same request would be accepted on one path and refused on the other.
LARGE_BODY_PATHS = ("/api/profile/photo", "/api/v1/profile/photo")


def limit_body_size():
    """Reject oversized JSON before it is parsed."""
    if request.method in SAFE_METHODS:
        return None
    if request.path in LARGE_BODY_PATHS:
        return None
    if (request.content_length or 0) > MAX_JSON_BYTES:
        return jsonify(ok=False, error="Request body is too large.",
                       code=ErrorCode.PAYLOAD_TOO_LARGE), 413
    return None


def csrf_protect():
    """Double-submit CSRF token on every state-changing request.

    There is no route-prefix exemption any more. The previous
    `/api/auth/*` bypass left login and logout CSRF-able (and silently
    exempted every future auth endpoint); the frontend already sends the
    token on those calls, so enforcing it costs nothing. The token is
    minted per session, rotated on login, and compared in constant time.
    Origin/Referer is checked as a second, independent layer.
    """
    if request.method in SAFE_METHODS:
        return None
    if not request.path.startswith("/api/"):
        return None

    sent = request.headers.get("X-CSRF-Token", "")
    have = session.get("csrf", "")
    if not have or not sent or not secrets.compare_digest(str(sent), str(have)):
        return jsonify(ok=False, error="Invalid CSRF token",
                       code=ErrorCode.FORBIDDEN), 403

    # Defence in depth: a same-origin request always carries one of these,
    # and a cross-site form post cannot forge them.
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin:
        from urllib.parse import urlsplit
        sent_host = urlsplit(origin).netloc.lower()
        if sent_host and sent_host != (request.host or "").lower():
            return jsonify(ok=False, error="Cross-origin request blocked.",
                           code=ErrorCode.FORBIDDEN), 403
    return None


def ensure_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]
