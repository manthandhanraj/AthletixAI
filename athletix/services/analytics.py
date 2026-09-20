# -*- coding: utf-8 -*-
"""Aggregation and export workflows."""

from athletix.audit import log_activity
from athletix.database import now_iso
from athletix.repositories import (activity, messages, notifications, ratings,
                                   reports, users)
from athletix.serializers import report_public, user_public


def platform_metrics():
    """Operator dashboard counters."""
    return {
        "users": users.count(),
        "athletes": users.count_by_role("athlete"),
        "coaches": users.count_by_role("coach"),
        "reports": reports.count(),
        "messages": messages.count(),
    }


def public_stats():
    """Landing-page numbers. Counts only - no personal data."""
    return {
        "athletes": users.count_by_role("athlete"),
        "coaches": users.count_by_role("coach"),
        "reports": reports.count(),
        "live_sessions": reports.count_live(),
        "sports": 35,
    }


def audit_trail(viewer, limit, offset):
    log_activity(viewer["id"], "admin_activity_viewed")
    return activity.recent(limit, offset), activity.count()


def export_user_data(user):
    """Everything held about the CALLER, and nothing else.

    Every query is filtered by the session's user id; there is no parameter
    to point this at another account.
    """
    # One `ai_results` query for the whole export instead of one per report
    # (Phase 3.1). The scope is unchanged - it is the caller's own reports,
    # which is what an export is.
    own_reports = reports.list_for_athlete(user["id"])
    ai_by_report = reports.ai_results_for_athlete(user["id"])
    data = {
        "account": user_public(user),
        "reports": [report_public(r, ai=ai_by_report.get(r["id"]))
                    for r in own_reports],
        "messages_received": [
            {"from_nm": r["from_nm"], "body": r["body"], "date": r["date"]}
            for r in messages.received_by(user["id"])],
        "messages_sent": [
            {"to_id": r["to_id"], "body": r["body"], "date": r["date"]}
            for r in messages.sent_by(user["id"])],
        "notifications": [
            {"text": r["text"], "date": r["date"], "read": r["read"]}
            for r in notifications.list_for_user(user["id"])],
        "ratings_given": [dict(r) for r in ratings.given_by(user["id"])],
        "consents": {r["key"]: r["value"] for r in users.consents(user["id"])},
        "exported_at": now_iso(),
    }
    log_activity(user["id"], "data_export")
    return data
