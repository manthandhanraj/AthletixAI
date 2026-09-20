# -*- coding: utf-8 -*-
"""Response DTOs — the declared shape of everything that leaves the process.

The privacy boundary is NOT here. `athletix/serializers.py` decides who may
see an e-mail address, a phone number, a metric breakdown or an AI verdict,
and it stays the single place that decision is made. This module:

  * gives the viewer-aware serializers a named, importable DTO entry point
    (`user_dto`, `report_dto`) so routes stop reaching into the serializer
    module directly, and
  * owns the shapes that carry no privacy decision at all - a message, a
    notification, a rating, the pagination envelope - which were previously
    rebuilt inline in three different modules and could drift apart.

Every function takes a row that the *query* already scoped to the caller, or
delegates the scoping decision to `serializers`. Nothing here reads the
database, and no raw row is ever returned.
"""

from athletix.pagination import page_meta

__all__ = ["user_dto", "users_dto", "report_dto", "message_dto",
           "messages_dto", "notification_dto", "notifications_dto",
           "rating_dto", "page_dto", "ok", "paged", "athlete_summary_dto",
           "coach_directory_dto", "feed_dto", "scoring_dto"]

# "Not supplied", distinct from a genuine None. A caller that already knows a
# report has no AI row passes ai=None and must NOT trigger a lookup for it.
_UNSET = object()


def ok(**payload):
    """The Phase 1 success envelope: {"ok": true, ...}."""
    body = {"ok": True}
    body.update(payload)
    return body


def page_dto(total, limit, offset, returned):
    """The pagination envelope (see athletix/pagination.py)."""
    return page_meta(total, limit, offset, returned)


def paged(key, items, total, limit, offset):
    """A collection response: the data under its own key, plus `page`."""
    return ok(**{key: items, "page": page_dto(total, limit, offset,
                                              len(items))})


# --------------------------------------------------------------------------
# Viewer-aware DTOs — these delegate to the privacy boundary
# --------------------------------------------------------------------------
def user_dto(row, viewer=None, profile=_UNSET):
    """One user, filtered for `viewer` by `serializers.user_public`."""
    from athletix import serializers
    if profile is _UNSET:
        return serializers.user_public(row, viewer)
    return serializers.user_public(row, viewer, profile=profile)


def users_dto(rows, viewer):
    """Many users, batch-loaded, each still filtered by `user_public`."""
    from athletix import serializers
    return serializers.serialize_users(rows, viewer)


def report_dto(row, detail=True, ai=_UNSET):
    """One report. `detail=False` is the score-only public shape."""
    from athletix import serializers
    if ai is _UNSET:
        return serializers.report_public(row, detail=detail)
    return serializers.report_public(row, detail=detail, ai=ai)


def reports_dto(rows, detail=True, ai_by_report=None):
    """Serialize a collection of reports.

    `ai_by_report` is a preloaded {report_id: ai_row} map. Without it each
    report costs one `ai_results` lookup - the N+1 that Phase 2.4 removed from
    `/api/state` but left in the per-resource collections. Pass it and the
    whole page costs one query.
    """
    if ai_by_report is None:
        return [report_dto(r, detail=detail) for r in rows]
    return [report_dto(r, detail=detail, ai=ai_by_report.get(r["id"]))
            for r in rows]


# --------------------------------------------------------------------------
# Shapes with no privacy decision in them
# --------------------------------------------------------------------------
def message_dto(row):
    """A direct message.

    No filtering happens here: every query that produces these rows is
    already restricted to conversations the caller is a party to (or to an
    operator). The shape is Phase 1's, key for key.
    """
    return {"id": row["id"], "toId": row["to_id"], "fromId": row["from_id"],
            "from": row["from_nm"], "text": row["body"], "date": row["date"]}


def messages_dto(rows):
    return [message_dto(r) for r in rows]


def notification_dto(row):
    """A notification. Rows are already scoped to their owner by the query."""
    return {"id": row["id"], "toId": row["user_id"], "text": row["text"],
            "date": row["date"], "read": bool(row["read"])}


def notifications_dto(rows):
    return [notification_dto(r) for r in rows]


def rating_dto(row, viewer_id, privileged):
    """A coach rating.

    The star value is public (it is what the directory renders); *who* gave it
    is not, so `byId` is present only for the viewer's own rating and for
    operators. That is a privacy decision, and it is the one Phase 1 made in
    `scoped_state`; it is expressed here so both callers cannot drift.
    """
    return {"coachId": row["coach_id"],
            "byId": (row["athlete_id"]
                     if privileged or row["athlete_id"] == viewer_id else None),
            "stars": row["stars"]}


def ratings_dto(rows, viewer_id, privileged):
    return [rating_dto(r, viewer_id, privileged) for r in rows]


def athlete_summary_dto(row, viewer, profile=_UNSET, summary=None,
                        detail=False):
    """An athlete directory entry plus the performance summary a dashboard
    needs, WITHOUT that athlete's report history.

    Privacy, unchanged from `/api/state`:

      * identity and contact fields come from `user_public`, so a viewer who
        may not see an e-mail address still does not see one here;
      * the derived scores (latest, best, progress, weekly, points) are the
        same information the leaderboard has always shown - they are folded
        from `overall`, which `report_public(detail=False)` already gives
        every authenticated viewer;
      * `latestReport.m`, the per-metric breakdown, is included ONLY when
        `detail` is True, which the caller derives from
        `serializers.can_view_report_detail` - the same gate `/api/state`
        applies.
    """
    data = user_dto(row, viewer, profile=profile)
    if not summary:
        data["summary"] = {"reports": 0, "latest": 0, "best": 0,
                           "progress": 0, "points": 50, "weekly": 0,
                           "lastDate": None, "latestReport": None}
        return data

    latest = summary.get("_latest_row")
    latest_dto = None
    if latest is not None:
        latest_dto = {"id": latest["id"], "date": latest["date"],
                      "sport": latest["sport"], "overall": latest["overall"],
                      "live": bool(latest["live"]), "m": None}
        if detail:
            from athletix.config import METRICS
            latest_dto["m"] = {k: latest[k] for k in METRICS}

    data["summary"] = {
        "reports": summary["reports"], "latest": summary["latest"],
        "best": summary["best"], "progress": summary["progress"],
        "points": summary["points"], "weekly": summary["weekly"],
        "lastDate": summary["lastDate"], "latestReport": latest_dto,
    }
    return data


def coach_directory_dto(row, viewer, profile=_UNSET, rating=None,
                        my_stars=None):
    """A coach directory entry with the aggregate rating they hold.

    `rating` is public (it is what the coach card renders). `myStars` is the
    VIEWER's own rating and nobody else's - who rated whom stays private,
    exactly as `rating_dto` decided for the bulk payload.
    """
    data = user_dto(row, viewer, profile=profile)
    data["rating"] = {"avg": (rating or {}).get("avg", 0),
                      "count": (rating or {}).get("count", 0)}
    data["myStars"] = my_stars
    return data


def feed_dto(report_rows, message_rows):
    """The operator activity feed: report and message events, newest first.

    Names are joined in SQL. This exists so the owner dashboard stops
    reconstructing a feed in the browser out of the entire reports and
    messages tables.
    """
    events = [{"kind": "report", "date": r["date"], "name": r["name"],
               "overall": r["overall"], "sport": r["sport"],
               "athleteId": r["athlete_id"]} for r in report_rows]
    events += [{"kind": "message", "date": m["date"], "from": m["from_nm"],
                "to": m["to_name"]} for m in message_rows]
    events.sort(key=lambda e: e["date"] or "", reverse=True)
    return events


def scoring_dto(row):
    """Provenance for a report's score.

    Says which provider produced the numbers, at which contract version, and
    - the part that matters - whether the server is willing to vouch for
    them. See `athletix/scoring/`.
    """
    keys = row.keys() if hasattr(row, "keys") else ()
    if "scoring_provider" not in keys:
        return None
    if not row["scoring_provider"]:
        return None
    return {
        "provider": row["scoring_provider"],
        "version": row["scoring_version"],
        "trusted": bool(row["scoring_trusted"]),
        "confidence": row["scoring_confidence"],
    }
