# -*- coding: utf-8 -*-
"""Viewer-aware serialization — the Phase 1 privacy boundary.

CRITICAL: `user_public(row, viewer)` decides who may see contact details.
It is pinned by tests/test_privacy_contract.py and was moved here VERBATIM
from app.py; the policy is unchanged. Do not "tidy" this file without a
green privacy contract suite.

Phase 2.1 finding A4 (N+1): these functions issue one query per user and per
detailed report. That is a known performance defect scheduled for Phase 2.4;
it is deliberately NOT fixed here, because changing data access and moving
the privacy boundary in the same step is exactly how a leak gets introduced.
"""

import json

from athletix.config import METRICS, PRIVILEGED_ROLES
from athletix.repositories import (messages as message_repo,
                                   notifications as notification_repo,
                                   ratings as rating_repo,
                                   reports as report_repo, users as user_repo)
from athletix.schemas import responses as dto


# Sentinel distinguishing "not supplied" from a genuine None, so a caller
# can pass `profile=None` to mean "this user has no profile row" without the
# serializer falling back to a per-row query.
_UNSET = object()


def is_privileged(user):
    return bool(user) and user["role"] in PRIVILEGED_ROLES


def user_public(row, viewer=None, profile=_UNSET):
    """Serialize a user for a specific audience.

    This is the privacy boundary. Contact details (e-mail, phone), account
    state (verified) and behavioural metadata (created_at, last_login) are
    only ever included for the account's *own* owner or for an admin/owner
    operator. Everyone else gets the directory fields the product genuinely
    needs to render: name, role, avatar, and the public part of the athlete
    or coach profile.

    `viewer=None` means "no audience filtering" and is used only for the
    caller's own record.
    """
    own = viewer is None or viewer["id"] == row["id"]
    privileged = is_privileged(viewer) if viewer is not None else True

    data = {
        "id": row["id"], "role": row["role"], "name": row["name"],
        "photo": row["photo"] or "",
    }
    if own or privileged:
        data.update({
            "email": row["email"],
            "phone": row["phone"] or "",
            "verified": bool(row["verified"]),
            "created_at": row["created_at"],
            "last_login": row["last_login"],
        })

    # `profile` may be supplied by a batch loader. When it is not, fall back
    # to the original per-row lookup so every existing call site (and the
    # privacy-contract tests) behaves exactly as before.
    if row["role"] == "athlete":
        p = (profile if profile is not _UNSET
             else user_repo.athlete_profile(row["id"]))
        if p:
            data.update(sport=p["sport"], age=p["age"], location=p["location"])
    elif row["role"] == "coach":
        p = (profile if profile is not _UNSET
             else user_repo.coach_profile(row["id"]))
        if p:
            data.update(specialty=p["specialty"], bio=p["bio"],
                        experience=p["experience"], city=p["city"],
                        achievements=json.loads(p["achievements"] or "[]"))
    return data


def serialize_users(rows, viewer):
    """Batch-serialize users with two profile queries instead of one per row.

    Identical output to `[user_public(r, viewer) for r in rows]` - the
    privacy decision is still made per row by user_public.
    """
    roles = {r["role"] for r in rows}
    athlete_profiles = (user_repo.athlete_profiles_all()
                        if "athlete" in roles else {})
    coach_profiles = (user_repo.coach_profiles_all()
                      if "coach" in roles else {})
    out = []
    for r in rows:
        if r["role"] == "athlete":
            p = athlete_profiles.get(r["id"])
        elif r["role"] == "coach":
            p = coach_profiles.get(r["id"])
        else:
            p = None
        out.append(user_public(r, viewer, profile=p))
    return out


def report_public(row, detail=True, ai=_UNSET):
    """Serialize a report.

    `detail=False` drops the per-metric breakdown and the AI verdict and
    keeps only what a public leaderboard needs (date, sport, overall score).
    """
    base = {
        "id": row["id"], "athleteId": row["athlete_id"], "date": row["date"],
        "sport": row["sport"], "overall": row["overall"],
        "live": bool(row["live"]),
        # Provenance (Phase 2.8): which provider produced the score and
        # whether the server vouches for it. Not private - it is a statement
        # about the number, not about the athlete - so it travels with the
        # lean leaderboard shape too.
        "scoring": dto.scoring_dto(row),
    }
    if not detail:
        base.update(m=None, ai=None)
        return base
    if ai is _UNSET:
        ai = report_repo.ai_result(row["id"])
    base.update(
        m={k: row[k] for k in METRICS},
        ai=({"potential": ai["potential"], "medal": ai["medal_prob"],
             "risk": ai["risk"], "best": ai["best_fit"]} if ai else None))
    return base


def can_view_report_detail(viewer, athlete_id):
    """Who may see an athlete's full metric breakdown and AI verdict.

    * the athlete themselves
    * any coach - scouting athlete performance is the product; coaches are
      a vetted role and this is the data they exist to read. They still do
      NOT get athlete contact details (see user_public).
    * admin / owner operators
    """
    if not viewer:
        return False
    if viewer["id"] == athlete_id:
        return True
    return viewer["role"] in ("coach",) + PRIVILEGED_ROLES


def leaderboard_leaders(report_rows=None, athlete_names=None):
    """The three derived badges the public leaderboard shows.

    The board displays "Most Improved", "Fastest" and "Strongest" athlete.
    Rendering those in the browser previously required every athlete's
    per-metric history to be shipped to every user. Computing them here
    sends three names instead of the whole cohort's biomechanics - the
    feature is identical, the data exposure is not.
    """
    if athlete_names is None:
        athlete_names = user_repo.athlete_names()
    names = athlete_names
    if not names:
        return {}

    if report_rows is None:
        report_rows = report_repo.latest_and_earliest_per_athlete()
    latest, earliest = {}, {}
    for r in report_rows:
        aid = r["athlete_id"]
        if aid not in names:
            continue
        earliest.setdefault(aid, r)
        latest[aid] = r

    def pick(scorer):
        best, best_val = None, None
        for aid, row in latest.items():
            try:
                val = scorer(aid, row)
            except (TypeError, KeyError):
                continue
            if best_val is None or val > best_val:
                best, best_val = aid, val
        if best is None:
            return None
        return {"id": best, "name": names[best], "value": round(best_val, 1)}

    return {
        "mostImproved": pick(lambda aid, row: (row["overall"] or 0)
                             - (earliest[aid]["overall"] or 0)),
        "fastest": pick(lambda aid, row: row["speed"] or 0),
        "strongest": pick(lambda aid, row: row["strength"] or 0),
    }


def scoped_state(user):
    """The dataset this specific user is authorized to see.

    Replaces the previous full_state(), which handed every authenticated
    caller the whole database - every e-mail address, phone number and
    private message on the platform. The response keeps the same shape so
    the frontend keeps working, but each collection is now filtered
    server-side by the viewer's identity and role:

      users     - directory fields only; contact details for self + operators
      reports   - full detail for own reports (and for coaches/operators);
                  score-only rows for other athletes, which is all the
                  leaderboard needs
      messages  - only conversations the viewer is a party to
      notifs    - only the viewer's own notifications
      ratings   - aggregate stars always; who rated whom only for the
                  viewer's own rating (and for operators)
    """
    uid = user["id"]
    privileged = is_privileged(user)

    # ── users: 1 query + 2 batched profile queries (was 1 + one per user) ──
    user_rows = user_repo.list_all()
    users = serialize_users(user_rows, user)

    # ── reports: 1 query + 1 batched ai_results query (was 1 + one per
    #    detailed report). The AI batch is loaded at exactly the scope the
    #    viewer is authorized for, so this is not a widening: an athlete
    #    loads only their own verdicts.
    report_rows = report_repo.list_all()
    if privileged or user["role"] == "coach":
        ai_by_report = report_repo.ai_results_all()
    else:
        ai_by_report = report_repo.ai_results_for_athlete(uid)

    reports = []
    for r in report_rows:
        detail = can_view_report_detail(user, r["athlete_id"])
        if detail:
            reports.append(report_public(r, detail=True,
                                         ai=ai_by_report.get(r["id"])))
        else:
            reports.append(report_public(r, detail=False))

    if privileged:
        msg_rows = message_repo.list_all()
    else:
        msg_rows = message_repo.list_for_user(uid)
    # Shapes come from schemas.responses so the bulk payload and the
    # per-resource endpoints cannot drift apart. The *scoping* stays here:
    # which rows are fetched is the privacy decision, and it is made above.
    messages = dto.messages_dto(msg_rows)

    ratings = dto.ratings_dto(rating_repo.list_all(), uid, privileged)

    if privileged:
        notif_rows = notification_repo.list_all()
    else:
        notif_rows = notification_repo.list_for_user(uid)
    notifs = dto.notifications_dto(notif_rows)

    return {"users": users, "reports": reports, "messages": messages,
            "ratings": ratings, "notifs": notifs,
            "leaders": leaderboard_leaders(
                report_rows=report_rows,
                athlete_names={r["id"]: r["name"] for r in user_rows
                               if r["role"] == "athlete"})}
