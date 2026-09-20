# -*- coding: utf-8 -*-
"""Operator endpoints. Every route is behind roles_required(); role changes
are owner-only and revoke the target's sessions."""

from flask import Blueprint, jsonify, request

from athletix.audit import log_activity
from athletix.pagination import page_params
from athletix.repositories import users
from athletix.schemas import requests as rq
from athletix.schemas import responses as dto
from athletix.security.session import bump_epoch, current_user, roles_required
from athletix.services import analytics as analytics_service
from athletix.services import directory as directory_service
from athletix.unit_of_work import unit_of_work
from athletix.validation import body

bp = Blueprint('admin', __name__)


@bp.get("/admin/metrics")
@roles_required("admin", "owner")
def admin_metrics():
    return jsonify(ok=True, metrics=analytics_service.platform_metrics())


@bp.get("/admin/users")
@roles_required("admin", "owner")
def admin_users():
    u = current_user()
    limit, offset = page_params(request.args)
    rows = users.list_all(limit, offset)
    log_activity(u["id"], "admin_users_listed")
    return jsonify(dto.paged("users", dto.users_dto(rows, u), users.count(),
                             limit, offset))


@bp.get("/admin/activity")
@roles_required("admin", "owner")
def admin_activity():
    """The real audit trail, readable only by operators.

    The owner screen used to reconstruct a pretend activity feed in the
    browser from the global state dump; this returns the actual
    security-relevant log the server writes.
    """
    u = current_user()
    limit, offset = page_params(request.args, default=200)
    rows, total = analytics_service.audit_trail(u, limit, offset)
    return jsonify(dto.paged("activity", [dict(r) for r in rows], total,
                             limit, offset))


@bp.get("/admin/coaches/<int:cid>/ratings")
@roles_required("admin", "owner")
def admin_coach_ratings(cid):
    """Who rated a coach. Operator-only, which is the boundary the bulk
    payload already drew: `byId` was null for everyone else."""
    rows = directory_service.coach_ratings(current_user(), cid)
    return jsonify(ok=True, ratings=rows)


@bp.post("/admin/users/<int:uid>/role")
@roles_required("owner")
def admin_set_role(uid):
    """Change a user's role. Owner only - this is the privilege-granting
    operation, so it is the most tightly held endpoint in the app.

    Nobody, including the owner, can create a second owner here (the schema's
    vocabulary has no such value), and the owner cannot demote themselves into
    locking the platform out.
    """
    u = current_user()
    new_role = rq.RoleChangeRequest.load(body())["role"]
    target = users.find_by_id(uid)
    if not target:
        return jsonify(ok=False, error="User not found."), 404
    if target["role"] == "owner":
        return jsonify(ok=False,
                       error="The owner role cannot be changed here."), 403
    # ATOMIC (Phase 2.3): the role change and the revocation of sessions
    # minted under the old permissions commit together. Split across two
    # transactions, a failure could leave a user elevated while still
    # holding cookies issued under their previous role.
    with unit_of_work():
        users.update_role(uid, new_role)
        bump_epoch(uid)
    log_activity(u["id"], "role_change",
                 "user=%s %s->%s" % (uid, target["role"], new_role))
    return jsonify(ok=True, message="Role updated.")
