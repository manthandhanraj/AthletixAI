# -*- coding: utf-8 -*-
"""Centralized application errors and their HTTP mapping.

Two things live here:

1. An exception taxonomy (`AppError` and friends) that services and
   repositories can raise without knowing anything about HTTP. Phase 2.5
   services will use these instead of returning `jsonify(...), 4xx`.

2. The Flask error handlers, which guarantee that a client never receives a
   stack trace, a SQL statement, a filesystem path or a secret — while the
   full detail still reaches the server log with a correlation id.

BACKWARD COMPATIBILITY: the response body keeps the exact Phase 1 shape
(`{"ok": false, "error": "..."}`) that the frontend already parses. A machine
readable `code` is *added* alongside it. Nothing is renamed or removed.
"""

import secrets

from flask import current_app, jsonify, render_template, request


# --------------------------------------------------------------------------
# Error codes — the stable, machine-readable vocabulary
# --------------------------------------------------------------------------
class ErrorCode(object):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# --------------------------------------------------------------------------
# Exception taxonomy
# --------------------------------------------------------------------------
class AppError(Exception):
    """Base class for every expected, client-visible failure.

    `message` is safe to show a user. Anything sensitive belongs in the log,
    not in here.
    """
    status = 500
    code = ErrorCode.INTERNAL_ERROR
    message = "Something went wrong. Please try again."

    def __init__(self, message=None, code=None, status=None, extra=None):
        super(AppError, self).__init__(message or self.message)
        if message:
            self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.extra = extra or {}

    def to_response(self):
        payload = {"ok": False, "error": self.message, "code": self.code}
        payload.update(self.extra)
        return jsonify(payload), self.status


class ValidationError(AppError):
    status = 400
    code = ErrorCode.VALIDATION_ERROR
    message = "Bad request."


class AuthenticationError(AppError):
    status = 401
    code = ErrorCode.AUTHENTICATION_REQUIRED
    message = "Authentication required."


class AuthorizationError(AppError):
    status = 403
    code = ErrorCode.FORBIDDEN
    message = "Not authorized."


class NotFoundError(AppError):
    status = 404
    code = ErrorCode.NOT_FOUND
    message = "Not found."


class ConflictError(AppError):
    status = 409
    code = ErrorCode.CONFLICT
    message = "Conflict."


class RateLimitError(AppError):
    status = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many attempts. Please try again later."


class PayloadTooLargeError(AppError):
    status = 413
    code = ErrorCode.PAYLOAD_TOO_LARGE
    message = "Request body is too large."


# --------------------------------------------------------------------------
# HTTP handlers
# --------------------------------------------------------------------------
def _wants_json():
    return (request.path.startswith("/api/")
            or request.accept_mimetypes.best == "application/json")


def _err(message, code, status):
    return jsonify(ok=False, error=message, code=code), status


def register_error_handlers(app):
    """Attach the handlers. Response bodies match Phase 1 exactly, plus a
    `code` field."""

    @app.errorhandler(AppError)
    def _app_error(exc):
        return exc.to_response()

    @app.errorhandler(400)
    def _e400(_e):
        return _err("Bad request.", ErrorCode.VALIDATION_ERROR, 400)

    @app.errorhandler(401)
    def _e401(_e):
        return _err("Authentication required.",
                    ErrorCode.AUTHENTICATION_REQUIRED, 401)

    @app.errorhandler(403)
    def _e403(_e):
        return _err("Not authorized.", ErrorCode.FORBIDDEN, 403)

    @app.errorhandler(404)
    def _e404(_e):
        if _wants_json():
            return _err("Not found.", ErrorCode.NOT_FOUND, 404)
        # The SPA owns client-side routing, so an unknown page returns the
        # shell rather than a dead end.
        return render_template("index.html"), 404

    @app.errorhandler(405)
    def _e405(_e):
        return _err("Method not allowed.", ErrorCode.METHOD_NOT_ALLOWED, 405)

    @app.errorhandler(413)
    def _e413(_e):
        return _err("Request body is too large.",
                    ErrorCode.PAYLOAD_TOO_LARGE, 413)

    @app.errorhandler(429)
    def _e429(_e):
        return _err("Too many requests.", ErrorCode.RATE_LIMITED, 429)

    @app.errorhandler(Exception)
    def _e500(exc):
        """Never leak a stack trace, SQL statement or filesystem path.

        The full traceback goes to the server log where operators can see it;
        the caller gets an opaque message plus a correlation id to quote.
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, AppError):
            return exc.to_response()
        ref = secrets.token_hex(6)
        current_app.logger.exception("unhandled error ref=%s path=%s",
                                     ref, request.path)
        try:
            # Imported here, not at module level: audit sits above errors in
            # the dependency layering, and a top-level import would create a
            # cycle (errors -> audit -> ... -> errors).
            from flask import session
            from athletix.audit import log_activity
            log_activity(session.get("uid"), "server_error",
                         "%s %s ref=%s" % (request.method, request.path, ref))
        except Exception:
            pass
        return jsonify(ok=False,
                       error="Something went wrong. Please try again.",
                       code=ErrorCode.INTERNAL_ERROR, ref=ref), 500
