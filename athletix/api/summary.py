# -*- coding: utf-8 -*-
"""Directory and summary endpoints — what the dashboards hydrate from.

These replace the frontend's dependence on `/api/state`, which shipped every
report of every athlete so the browser could derive five numbers per athlete.
`/api/state` itself is unchanged and still mounted: it is a compatibility
endpoint, not a dead one.

Every handler here is `@login_required`, delegates to
`services/directory.py`, and returns viewer-filtered DTOs. No SQL, no
authorization logic and no privacy decision lives in this file.
"""

from flask import Blueprint, jsonify, request

from athletix.pagination import page_params
from athletix.schemas import responses as dto
from athletix.security.session import current_user, login_required
from athletix.services import directory as directory_service

bp = Blueprint('summary', __name__)

# One row per athlete or coach, not one per report, so a default that covers a
# whole academy is still a small payload. Still bounded: `page_params` clamps
# to MAX_LIMIT and the response carries `page` metadata for anything larger.
DIRECTORY_DEFAULT_LIMIT = 200


@bp.get("/directory/athletes")
@login_required
def athlete_directory():
    """Athlete roster + performance summary, one row per athlete.

    Replaces the `users` + `reports` blocks of `/api/state` for every screen
    that only needed derived numbers: leaderboard, athlete tables, recruiter
    hub, compare, analytics.
    """
    u = current_user()
    limit, offset = page_params(request.args, default=DIRECTORY_DEFAULT_LIMIT)
    rows, total = directory_service.athlete_directory(u, limit, offset)
    return jsonify(dto.paged("athletes", rows, total, limit, offset))


@bp.get("/directory/coaches")
@login_required
def coach_directory():
    """Coach roster + aggregate rating. Replaces shipping the whole ratings
    table so the browser could average it."""
    u = current_user()
    limit, offset = page_params(request.args, default=DIRECTORY_DEFAULT_LIMIT)
    rows, total = directory_service.coach_directory(u, limit, offset)
    return jsonify(dto.paged("coaches", rows, total, limit, offset))


@bp.get("/summary/platform")
@login_required
def platform_summary():
    """Dashboard counters, the 14-day activity curve, and — for operators
    only — the activity feed and platform-wide totals.

    Cost is independent of how many reports exist: these are SQL aggregates,
    not a client-side scan of every row.
    """
    return jsonify(ok=True,
                   summary=directory_service.platform_summary(current_user()))
