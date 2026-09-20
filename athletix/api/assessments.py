# -*- coding: utf-8 -*-
"""Assessments (reports) and the coach's athlete views.

Report authorization is ownership-based: guessing an id returns 404, not
somebody else's performance data.
"""

from flask import Blueprint, jsonify, request

from athletix.pagination import page_params
from athletix.repositories import reports, users
from athletix.schemas import requests as rq
from athletix.schemas import responses as dto
from athletix.security.ratelimit import too_many
from athletix.security.session import (current_user, login_required,
                                       roles_required)
from athletix.services import assessment as assessment_service
from athletix.validation import body

bp = Blueprint('assessments', __name__)


@bp.post("/reports")
@login_required
def add_report():
    """File an assessment for the SESSION's athlete.

    The metrics in the body are computed by the browser (the CV pipeline runs
    client-side), so they are untrusted input: `services.assessment` hands
    them to the scoring boundary in `athletix/scoring/`, which decides what
    the server is willing to store and to vouch for.
    """
    u = current_user()
    limited = too_many("report", str(u["id"]))
    if limited:
        return limited
    row = assessment_service.create_assessment(
        u, rq.ReportCreateRequest.load(body()))
    return jsonify(ok=True, report=dto.report_dto(row))


@bp.get("/reports/<int:rid>")
@login_required
def get_report(rid):
    """Read one report - authorization by ownership, not by knowing the id.

    Guessing or incrementing a report id gets an athlete a 404, not somebody
    else's performance data.
    """
    row = assessment_service.get_report_for(current_user(), rid)
    return jsonify(ok=True, report=dto.report_dto(row))


@bp.get("/my/reports")
@login_required
def my_reports():
    u = current_user()
    limit, offset = page_params(request.args)
    rows = reports.list_for_athlete(u["id"], limit, offset)
    ai = reports.ai_results_for_reports([r["id"] for r in rows])
    return jsonify(dto.paged("reports",
                             dto.reports_dto(rows, ai_by_report=ai),
                             reports.count_for_athlete(u["id"]),
                             limit, offset))


@bp.get("/coach/athletes")
@roles_required("coach", "admin", "owner")
def coach_athletes():
    """Athlete directory for coaches - performance fields only.

    Scouting athlete performance is what a coach account is for, so coaches
    see athletes and their scores. They do NOT see athlete e-mail addresses
    or phone numbers: user_public() withholds those from every viewer that
    is not the athlete themselves or an operator.
    """
    u = current_user()
    limit, offset = page_params(request.args)
    rows = users.list_by_role("athlete", limit=limit, offset=offset)
    return jsonify(dto.paged("athletes", dto.users_dto(rows, u),
                             users.count_by_role("athlete"), limit, offset))


@bp.get("/coach/athletes/<int:aid>")
@roles_required("coach", "admin", "owner")
def coach_athlete_detail(aid):
    u = current_user()
    limit, offset = page_params(request.args)
    row, athlete_reports, total = assessment_service.athlete_detail(
        u, aid, limit, offset)
    ai = reports.ai_results_for_reports([r["id"] for r in athlete_reports])
    payload = dto.paged("reports",
                        dto.reports_dto(athlete_reports, ai_by_report=ai),
                        total, limit, offset)
    payload["athlete"] = dto.user_dto(row, u)
    return jsonify(payload)
