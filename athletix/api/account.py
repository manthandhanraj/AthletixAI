# -*- coding: utf-8 -*-
"""Account-scoped reads and settings: current user, scoped state, personal
collections, consent and the GDPR-style data export."""

import json
from collections import OrderedDict

from flask import Blueprint, jsonify, make_response, request

from athletix.audit import log_activity
from athletix.pagination import page_params
from athletix.repositories import messages, notifications, users
from athletix.schemas import requests as rq
from athletix.schemas import responses as dto
from athletix.security.ratelimit import too_many
from athletix.security.session import (current_user, login_required,
                                       reload_user, roles_required)
from athletix.serializers import scoped_state
from athletix.services import analytics as analytics_service
from athletix.services import profile as profile_service
from athletix.unit_of_work import unit_of_work
from athletix.validation import body

bp = Blueprint('account', __name__)


@bp.get("/me")
@login_required
def api_me():
    return jsonify(ok=True, user=dto.user_dto(reload_user()))


@bp.get("/state")
@login_required
def api_state():
    """Bulk hydration for the single-page frontend - scoped to the caller.

    Every collection in the payload is filtered server-side by identity and
    role (see scoped_state). The per-resource endpoints below are the
    preferred, narrower way to read the same data; this one exists because
    the existing UI renders from one in-memory store, and replacing that is
    a frontend change, not a security fix.
    """
    return jsonify(ok=True, state=scoped_state(current_user()))


@bp.get("/my/messages")
@login_required
def my_messages():
    u = current_user()
    limit, offset = page_params(request.args)
    rows = messages.list_for_user(u["id"], limit, offset)
    return jsonify(dto.paged("messages", dto.messages_dto(rows),
                             messages.count_for_user(u["id"]), limit, offset))


@bp.get("/my/notifications")
@login_required
def my_notifications():
    u = current_user()
    limit, offset = page_params(request.args, default=200)
    rows = notifications.list_for_user(u["id"], limit=limit, offset=offset)
    return jsonify(dto.paged("notifications", dto.notifications_dto(rows),
                             notifications.count_for_user(u["id"]),
                             limit, offset))


@bp.post("/consent")
@login_required
def set_consent():
    d = rq.ConsentRequest.load(body())
    profile_service.set_consent(current_user(), d["key"], d["value"])
    return jsonify(ok=True)


@bp.get("/export")
@login_required
def export_data():
    """Privacy: export everything we hold about the CALLER, and nothing else.

    Every query below is filtered by the session's user id. There is no
    parameter to point this at another account.
    """
    u = current_user()
    limited = too_many("export", str(u["id"]))
    if limited:
        return limited
    data = analytics_service.export_user_data(u)
    resp = make_response(json.dumps(data, indent=2))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = \
        "attachment; filename=athletixai_my_data.json"
    return resp


@bp.post("/coach/profile")
@roles_required("coach")
def coach_profile():
    """Edit the caller's own coach profile.

    The WHERE clause is the session's user id, and the SET clause is built
    from the schema's declared columns - never from client-supplied keys.
    `load_present` keeps the Phase 1 partial-update semantics: a field that
    was not sent is not written.
    """
    u = current_user()
    sent = rq.CoachProfileRequest.load_present(body())

    fields = OrderedDict()
    for column in ("specialty", "bio", "experience", "city"):
        if column in sent:
            fields[column] = sent[column]
    if "achievements" in sent:
        fields["achievements"] = json.dumps(sent["achievements"])
    if fields:
        # The repository re-checks every key against its own allowlist, so a
        # client key can never reach the SET clause even if this builder is
        # later changed.
        with unit_of_work():
            users.update_coach_profile(u["id"], fields)
    log_activity(u["id"], "coach_profile_update")
    return jsonify(ok=True, message="Profile saved.",
                   user=dto.user_dto(reload_user()))


@bp.get("/public/stats")
def public_stats():
    """Real platform numbers for the landing page (no login needed).

    These are read live from the database - nothing here is hard-coded,
    so the landing page can never show a number we cannot back up.
    """
    return jsonify(ok=True, **analytics_service.public_stats())
