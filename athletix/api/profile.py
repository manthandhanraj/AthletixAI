# -*- coding: utf-8 -*-
"""Self-service profile management.

Every handler writes to the session's own user id - there is no user id in
any request schema, so none of these can be aimed at another account.
"""

from flask import Blueprint, jsonify, session

from athletix.config import DEV_CODES
from athletix.schemas import requests as rq
from athletix.security.ratelimit import too_many
from athletix.security.session import (current_user, demo_locked,
                                       login_required, reload_user,
                                       start_session)
from athletix.serializers import user_public
from athletix.services import auth as auth_service
from athletix.services import profile as profile_service
from athletix.validation import body

bp = Blueprint('profile', __name__)


@bp.post("/profile/update")
@login_required
def profile_update():
    """Update the caller's own profile.

    There is no user id in the request: the record being written is always
    the one behind the session cookie. A client cannot aim this at another
    account, and cannot include `role`, `verified` or `id` - those keys are
    not in the schema and are not in the UPDATE.
    """
    u = current_user()
    profile_service.update_profile(u, rq.ProfileUpdateRequest.load(body()))
    return jsonify(ok=True, message="Profile updated successfully.",
                   user=user_public(reload_user()))


@bp.post("/profile/photo")
@login_required
def profile_photo():
    u = current_user()
    limited = too_many("photo", str(u["id"]))
    if limited:
        return limited
    profile_service.update_photo(u, rq.PhotoRequest.load(body())["photo"])
    return jsonify(ok=True, message="Profile photo updated.")


@bp.post("/profile/password")
@login_required
@demo_locked
def change_password():
    u = current_user()
    limited = too_many("password", str(u["id"]))
    if limited:
        return limited
    d = rq.PasswordChangeRequest.load(body())
    fresh = auth_service.change_password(u, d["current"], d["password"])
    # Re-establish this tab's session so the user who just changed their
    # password is not logged out of the window they are sitting in.
    csrf = start_session(fresh, session.permanent)
    return jsonify(ok=True, message="Password changed successfully.",
                   csrf=csrf)


@bp.post("/profile/email/request")
@login_required
@demo_locked
def request_email_change():
    u = current_user()
    limited = too_many("email_change", str(u["id"]))
    if limited:
        return limited
    d = rq.EmailChangeRequest.load(body())
    code = profile_service.request_email_change(u, d["email"], d["password"])
    generic = "If that address is available, a verification code is on its " \
              "way to it."
    if code and DEV_CODES:
        return jsonify(ok=True, message=generic, dev_code=code)
    return jsonify(ok=True, message=generic)


@bp.post("/profile/email/confirm")
@login_required
@demo_locked
def confirm_email_change():
    u = current_user()
    limited = too_many("email_change", str(u["id"]))
    if limited:
        return limited
    d = rq.EmailConfirmRequest.load(body())
    fresh = profile_service.confirm_email_change(u, d["email"], d["code"])
    csrf = start_session(fresh, session.permanent)
    return jsonify(ok=True, message="Email updated successfully.",
                   user=user_public(fresh), csrf=csrf)


@bp.post("/profile/delete")
@login_required
@demo_locked
def delete_account():
    u = current_user()
    profile_service.delete_account(
        u, rq.DeleteAccountRequest.load(body())["password"])
    session.clear()
    return jsonify(ok=True, message="Your account has been deleted.")
