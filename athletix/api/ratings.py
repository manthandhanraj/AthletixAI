# -*- coding: utf-8 -*-
"""Coach ratings. Athletes only, coach targets only, attribution from the
session - never from the request body."""

from flask import Blueprint, jsonify

from athletix.schemas import requests as rq
from athletix.security.ratelimit import too_many
from athletix.security.session import current_user, login_required
from athletix.services import messaging as messaging_service
from athletix.validation import body

bp = Blueprint('ratings', __name__)


@bp.post("/ratings")
@login_required
def rate_coach():
    """Rate a coach.

    Only athletes may rate, and only accounts whose role is actually 'coach'
    can be rated. The rating is always attributed to the session's user - the
    request schema has no field for who is rating - so nobody can rate on
    someone else's behalf, or edit or delete another user's rating (the
    composite primary key (coach_id, athlete_id) plus the session-derived
    athlete_id means an upsert can only ever touch the caller's own row).
    """
    u = current_user()
    limited = too_many("rating", str(u["id"]))
    if limited:
        return limited
    d = rq.RatingRequest.load(body())
    messaging_service.rate_coach(u, d["coachId"], d["stars"])
    return jsonify(ok=True, message="Rating submitted.")
