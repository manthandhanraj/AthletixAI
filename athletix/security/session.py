# -*- coding: utf-8 -*-
"""Session lifecycle and role-based access control.

The session epoch is the mechanism that makes revocation real: every request
re-checks the cookie's epoch against the database, so a password reset, role
change or "log out everywhere" invalidates live cookies immediately.
"""

import secrets
import time
from functools import wraps

from flask import g, request, session

from athletix.config import PRIVILEGED_ROLES, SESSION_IDLE_SECONDS
from athletix.repositories import users as user_repo
from athletix.errors import (AuthenticationError,
                             AuthorizationError)

# Imported lazily inside roles_required to avoid a package-level cycle:
# audit -> ratelimit -> config, and session -> audit.
from athletix.audit import log_activity


def bump_epoch(user_id):
    """Invalidate every existing session for this user.

    Called on password reset, password change, e-mail change, role change and
    account deletion. Because the epoch lives in the database and every
    request re-checks it, a cookie minted before the bump stops working
    immediately - including one held by an attacker.

    Runs inside `unit_of_work()`, so when it is called as part of a larger
    operation (a password reset, say) it JOINS that transaction rather than
    committing on its own. That is what makes the credential change and the
    revocation atomic - the Phase 2.1 B1 defect.
    """
    from athletix.unit_of_work import unit_of_work
    with unit_of_work():
        user_repo.bump_session_epoch(user_id)


def reload_user():
    """Drop the per-request user cache and re-read the row.

    current_user() memoizes on `g` so a single request does not re-query the
    users table for every authorization check. Any handler that *writes* to
    the caller's own row must call this before serializing the user back, or
    it will echo the pre-update values.
    """
    g._user_checked = False
    g._user = None
    return current_user()


def start_session(row, remember=False):
    """Mint a fresh, fully-rotated session for a successfully authenticated
    user. Always clears first, so a pre-auth session cannot be fixated."""
    session.clear()
    session["uid"] = row["id"]
    session["ep"] = row["sess_epoch"]
    session["ts"] = int(time.time())
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = bool(remember)
    return session["csrf"]


def current_user():
    """The authenticated user, or None.

    Every request re-validates the session against the database: the account
    must still exist and the cookie's epoch must match the account's current
    epoch, and the session must not have gone idle past
    SESSION_IDLE_SECONDS. A stale or revoked cookie is cleared, not honoured.
    """
    if getattr(g, "_user_checked", False):
        return g._user
    g._user_checked = True
    g._user = None

    uid = session.get("uid")
    if not uid:
        return None

    last = session.get("ts")
    if not isinstance(last, int) or (time.time() - last) > SESSION_IDLE_SECONDS:
        session.clear()
        return None

    row = user_repo.find_by_id(uid)
    if row is None or session.get("ep") != row["sess_epoch"]:
        session.clear()
        return None

    # Slide the idle window, but only once a minute so we are not rewriting
    # the cookie on every single request.
    if time.time() - last > 60:
        session["ts"] = int(time.time())

    g._user = row
    return row


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            # Typed error so the response carries a machine-readable code;
            # the message and 401 status are unchanged from Phase 1.
            raise AuthenticationError("Authentication required")
        return fn(*a, **kw)
    return wrapper


def roles_required(*roles):
    """Server-side RBAC. The frontend's role checks are for menus only -
    this is the one that actually decides."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                raise AuthenticationError("Authentication required")
            if u["role"] not in roles:
                log_activity(u["id"], "authz_denied", request.path)
                raise AuthorizationError("Not authorized.")
            return fn(*a, **kw)
        return wrapper
    return deco
