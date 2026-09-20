# -*- coding: utf-8 -*-
"""Directory and summary workflows — the replacement for bulk hydration.

Phase 2.4 fixed `/api/state`'s query count (10,603 → 16). It did not fix its
SIZE: 97% of that payload is the `reports` collection, because the browser was
shipped every report of every athlete so it could compute, in JavaScript, five
derived numbers per athlete (latest score, progress, weekly delta, points,
session count) plus a handful of platform counters.

This module computes those numbers where the data already is. The dashboards
render exactly what they rendered before; what changes is that ~1.4 MB of raw
rows stops crossing the network to do it.

Two rules this module does not bend:

*The privacy boundary is unchanged.* Identity fields go through
`serializers.user_public` (so contact details stay filtered by viewer), and
the per-metric breakdown of a report is gated by
`serializers.can_view_report_detail` — the same function `/api/state` uses.
An athlete sees other athletes' overall scores (the leaderboard already showed
those) and never their metric breakdown.

*The arithmetic is the frontend's, exactly.* `points`, `weekly`, `progress`
are folded here with the same formulas and the same rounding the browser used,
including `Math.round`'s round-half-up (Python's `round` is half-even), so no
displayed number moves.
"""

import datetime
import math

from athletix.errors import AuthorizationError, NotFoundError
from athletix.repositories import (messages, notifications, ratings, reports,
                                   users)
from athletix.schemas import responses as dto
from athletix.serializers import (can_view_report_detail, is_privileged,
                                  leaderboard_leaders)

WEEK_SECONDS = 7 * 86400
FEED_REPORTS = 12
FEED_MESSAGES = 8
HISTOGRAM_DAYS = 14


# --------------------------------------------------------------------------
# Folding one athlete's series into the numbers a dashboard shows
# --------------------------------------------------------------------------
def _round_half_up(value):
    """JavaScript's Math.round: ties go up, not to even.

    Python's round() is banker's rounding, so round(0.5) is 0 and round(1.5)
    is 2. Using it here would move numbers the UI has always shown.
    """
    return int(math.floor(value + 0.5))


def _fold(rows, week_cutoff):
    """Reduce one athlete's ordered reports to the summary the UI needs.

    Mirrors reportsOf()/pointsOf()/weeklyDelta()/progressOf()/latestOverall()
    in templates/index.html, statement for statement.
    """
    overalls = [r["overall"] or 0 for r in rows]
    points = 50
    weekly = 0
    for i in range(1, len(rows)):
        step = _round_half_up((overalls[i] - overalls[i - 1]) * 2)
        points += step
        if rows[i]["date"] >= week_cutoff:
            weekly += step
    latest_row = rows[-1]
    return {
        "reports": len(rows),
        "latest": overalls[-1],
        "best": max(overalls),
        "progress": round(overalls[-1] - overalls[0], 1),
        "points": max(0, points),
        "weekly": weekly,
        "lastDate": latest_row["date"],
        "_latest_row": latest_row,
    }


def _summaries_by_athlete():
    """{athlete_id: folded summary} for the whole cohort, in ONE query."""
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(seconds=WEEK_SECONDS)).isoformat(
                  timespec="seconds")
    grouped, current, current_id = {}, [], None
    for row in reports.performance_series():
        if row["athlete_id"] != current_id:
            if current:
                grouped[current_id] = _fold(current, cutoff)
            current_id, current = row["athlete_id"], []
        current.append(row)
    if current:
        grouped[current_id] = _fold(current, cutoff)
    return grouped


# --------------------------------------------------------------------------
# Directories
# --------------------------------------------------------------------------
def athlete_directory(viewer, limit, offset):
    """Athlete roster plus each athlete's performance summary.

    Returns (rows, total). This is what the leaderboard, the coach athlete
    table, the recruiter view, compare and analytics all read from now.
    """
    rows = users.list_by_role("athlete", limit=limit, offset=offset)
    summaries = _summaries_by_athlete()
    profiles = users.athlete_profiles_all()
    out = []
    for row in rows:
        summary = summaries.get(row["id"])
        out.append(dto.athlete_summary_dto(
            row, viewer, profile=profiles.get(row["id"]), summary=summary,
            detail=can_view_report_detail(viewer, row["id"])))
    return out, users.count_by_role("athlete")


def coach_directory(viewer, limit, offset):
    """Coach roster plus the aggregate rating each coach holds.

    The browser used to receive every rating row on the platform to compute
    these averages. Now it receives an average and a count, and — for the
    caller only — the caller's own star value.
    """
    rows = users.list_by_role("coach", limit=limit, offset=offset)
    aggregates = ratings.aggregates()
    mine = ratings.by_athlete(viewer["id"]) if viewer["role"] == "athlete" else {}
    profiles = users.coach_profiles_all()
    # Operator-only: how many messages each coach has sent. The owner console
    # showed this by counting the whole messages table in the browser.
    sent = messages.sent_counts() if is_privileged(viewer) else None
    out = []
    for row in rows:
        entry = dto.coach_directory_dto(
            row, viewer, profile=profiles.get(row["id"]),
            rating=aggregates.get(row["id"]), my_stars=mine.get(row["id"]))
        if sent is not None:
            entry["messagesSent"] = sent.get(row["id"], 0)
        out.append(entry)
    return out, users.count_by_role("coach")


def coach_ratings(viewer, coach_id):
    """Who rated a coach, and how. OPERATORS ONLY.

    `scoped_state` gave admins and owners the athlete id behind every rating
    (`byId`); everyone else got null. This preserves exactly that boundary
    while resolving the id to a name server-side.
    """
    if not is_privileged(viewer):
        raise AuthorizationError("Not authorized.")
    row = users.find_by_id_and_role(coach_id, "coach")
    if not row:
        raise NotFoundError("Coach not found.")
    return [{"stars": r["stars"], "date": r["updated_at"],
             "athleteId": r["athlete_id"], "athlete": r["name"]}
            for r in ratings.for_coach_with_names(coach_id)]


# --------------------------------------------------------------------------
# Platform summary
# --------------------------------------------------------------------------
def platform_summary(viewer):
    """The counters the dashboards show, computed in SQL.

    Role-scoped: everyone authenticated gets the public shape of the platform
    (how many athletes, how many assessments, the 14-day activity curve).
    Operator-only fields — the message and notification totals, who was active
    today, and the activity feed with names in it — are added for admin and
    owner accounts only.
    """
    now = datetime.datetime.now()
    today = now.date().isoformat()
    week_cutoff = (now - datetime.timedelta(seconds=WEEK_SECONDS)).isoformat(
        timespec="seconds")
    since = (now - datetime.timedelta(days=HISTOGRAM_DAYS - 1)).date().isoformat()

    counters = reports.counters(today, week_cutoff)
    average = reports.average_latest_overall()

    counts = {row["day"]: row["n"] for row in reports.daily_counts(since)}
    histogram = []
    for offset in range(HISTOGRAM_DAYS - 1, -1, -1):
        day = (now - datetime.timedelta(days=offset)).date().isoformat()
        histogram.append({"date": day, "count": counts.get(day, 0)})

    summary = {
        # The three derived leaderboard badges, computed by the same
        # serializer the bulk payload used - so the board shows what it
        # always showed, without shipping the cohort's biomechanics.
        "leaders": leaderboard_leaders(),
        "users": {
            "total": users.count(),
            "athletes": users.count_by_role("athlete"),
            "coaches": users.count_by_role("coach"),
        },
        "reports": counters,
        "avgOverall": round(average, 1) if average is not None else None,
        "daily": histogram,
    }

    if not is_privileged(viewer):
        return summary

    active = reports.athletes_active_since(today)
    active |= messages.parties_active_since(today)
    summary.update(
        messages=messages.count(),
        notifications=notifications.count(),
        ratings=ratings.count(),
        activeToday=len(active),
        feed=dto.feed_dto(reports.recent_with_names(FEED_REPORTS),
                          messages.recent_with_names(FEED_MESSAGES)),
    )
    return summary
