# -*- coding: utf-8 -*-
"""Assessment (report) workflows."""

from athletix.audit import log_activity
from athletix.config import VIDEO_SOURCES
from athletix.database import now_iso
from athletix.errors import AuthorizationError, NotFoundError
from athletix.repositories import reports, users
from athletix.scoring import get_service as scoring_service
from athletix.scoring import parse_metrics  # noqa: F401  (public re-export)
from athletix.serializers import can_view_report_detail
from athletix.unit_of_work import unit_of_work
from athletix.validation import as_int, safe_filename


def create_assessment(user, data):
    """Record an assessment for the SESSION's athlete.

    There is no athleteId in the request body, so a report can never be filed
    against somebody else's account.

    The numbers arrive from the browser, where the pose pipeline runs, so they
    are untrusted input. They go through `athletix/scoring/`, which validates
    them, recomputes the overall server-side, and returns a result that
    records WHO produced the score and whether the server vouches for it.
    Report, AI result and video metadata are one assessment: partial
    persistence would leave a report whose provenance is missing.
    """
    if user["role"] != "athlete":
        raise AuthorizationError("Only athletes can create reports.")

    sport = users.athlete_sport(user["id"]) or "Athletics"
    result = scoring_service().score_request(user["id"], sport, data)

    with unit_of_work():
        rid = reports.create(user["id"], now_iso(), result.sport,
                             result.metrics, result.overall,
                             1 if data.get("live") else 0,
                             provider=result.provider, version=result.version,
                             trusted=1 if result.trusted else 0,
                             confidence=result.confidence)

        if result.ai:
            reports.add_ai_result(rid, result.ai["potential"],
                                  result.ai["medal"], result.ai["risk"],
                                  result.ai["best"])

        vinfo = data.get("video")
        if isinstance(vinfo, dict) and vinfo:
            source = vinfo.get("source")
            reports.add_video(rid, user["id"],
                              safe_filename(vinfo.get("name")),
                              as_int(vinfo.get("size"), 0, 4096, 0),
                              source if source in VIDEO_SOURCES else "upload",
                              now_iso())

    log_activity(user["id"], "report_created",
                 "overall=%s provider=%s trusted=%s"
                 % (result.overall, result.provider, result.trusted))
    return reports.find_by_id(rid)


def get_report_for(user, report_id):
    """Authorization is by ownership, not by knowing the id.

    Returns 404 rather than 403: a 403 would confirm the report exists.
    """
    row = reports.find_by_id(report_id)
    if not row or not can_view_report_detail(user, row["athlete_id"]):
        raise NotFoundError("Report not found.")
    log_activity(user["id"], "report_viewed", "report=%s" % report_id)
    return row


def athlete_detail(viewer, athlete_id, limit, offset):
    """An athlete's record for a coach/operator. Returns (row, reports, total)."""
    row = users.find_by_id_and_role(athlete_id, "athlete")
    if not row:
        raise NotFoundError("Athlete not found.")
    log_activity(viewer["id"], "athlete_viewed", "athlete=%s" % athlete_id)
    return (row, reports.list_for_athlete(athlete_id, limit, offset),
            reports.count_for_athlete(athlete_id))
