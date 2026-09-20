# -*- coding: utf-8 -*-
"""Phase 3.1 - directory/summary endpoints and the end of bulk hydration.

These endpoints exist to stop shipping every report on the platform to every
browser. That makes them a privacy question as much as a performance one: the
numbers they carry are derived from other people's data, so each one has to be
checked against the boundary `/api/state` already drew.

What is pinned here:

  * authentication on every new endpoint, in both namespaces;
  * an athlete gets other athletes' derived scores (the leaderboard always
    showed those) and NEVER their metric breakdown or contact details;
  * rating attribution stays operator-only;
  * the arithmetic matches the formulas the browser used, exactly;
  * the collection endpoints cost a bounded number of queries - no N+1.
"""

import pytest

import app as appmod
from conftest import ATHLETE_PW, make_client, user_id

V1 = "/api/v1"
METRICS = appmod.METRICS


@pytest.fixture
def query_counter(monkeypatch):
    """Counts SQL statements issued while serving a request.

    The trace callback is installed after `_tune_connection` has run its
    PRAGMAs, so this counts data queries only - which is what an N+1 claim is
    actually about.
    """
    counter = {"n": 0, "statements": []}
    import athletix.database as database
    real = database._tune_connection

    def tuned(conn):
        conn = real(conn)

        def trace(statement):
            counter["n"] += 1
            counter["statements"].append(statement)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(database, "_tune_connection", tuned)
    return counter


def add_report(client, **metrics):
    m = {k: metrics.get(k, 70) for k in METRICS}
    return client.post("/api/reports", {"m": m})


def add_reports(client, overalls):
    """Create reports whose overall lands on each requested value.

    `overall` is the mean of the five metrics, so setting them all equal
    makes it exact.
    """
    for value in overalls:
        assert add_report(client, **{k: value for k in METRICS}
                          ).status_code == 200


# ==========================================================================
# Access control
# ==========================================================================
class TestAccessControl:

    @pytest.mark.parametrize("path", [
        "/directory/athletes", "/directory/coaches", "/summary/platform",
    ])
    @pytest.mark.parametrize("prefix", ["/api", V1])
    def test_authentication_is_required(self, client, prefix, path):
        assert client.get(prefix + path).status_code == 401

    @pytest.mark.parametrize("path", [
        "/directory/athletes", "/directory/coaches", "/summary/platform",
    ])
    def test_both_mounts_return_the_same_payload(self, athlete, path):
        assert (athlete.get("/api" + path).get_json()
                == athlete.get(V1 + path).get_json())

    def test_coach_rating_attribution_is_operator_only(self, flask_app,
                                                       athlete, coach, owner,
                                                       admin):
        cid = user_id(flask_app, "coach.a@test.local")
        assert athlete.post("/api/ratings",
                            {"coachId": cid, "stars": 4}).status_code == 200
        path = "/api/admin/coaches/%d/ratings" % cid
        assert athlete.get(path).status_code == 403
        assert coach.get(path).status_code == 403
        for operator in (owner, admin):
            r = operator.get(path)
            assert r.status_code == 200
            rows = r.get_json()["ratings"]
            assert rows and rows[0]["athlete"] == "Athlete A"
            assert rows[0]["stars"] == 4

    def test_unknown_coach_is_not_found(self, owner):
        assert owner.get("/api/admin/coaches/999999/ratings").status_code == 404

    def test_an_athlete_id_is_not_a_coach(self, flask_app, athlete, owner):
        aid = user_id(flask_app, "athlete.a@test.local")
        assert owner.get("/api/admin/coaches/%d/ratings" % aid).status_code == 404


# ==========================================================================
# Privacy — the reason this is not just a performance change
# ==========================================================================
class TestDirectoryPrivacy:

    def _rows(self, client):
        return {a["id"]: a for a in
                client.get("/api/directory/athletes").get_json()["athletes"]}

    def test_athlete_sees_no_foreign_contact_details(self, flask_app, athlete,
                                                     athlete_b):
        rows = self._rows(athlete)
        other = user_id(flask_app, "athlete.b@test.local")
        assert "email" not in rows[other]
        assert "phone" not in rows[other]
        assert "last_login" not in rows[other]

    def test_athlete_sees_own_contact_details(self, flask_app, athlete):
        me = user_id(flask_app, "athlete.a@test.local")
        assert self._rows(athlete)[me]["email"] == "athlete.a@test.local"

    def test_athlete_never_sees_another_athletes_metric_breakdown(
            self, flask_app, athlete, athlete_b):
        add_reports(athlete_b, [70])
        add_reports(athlete, [80])
        rows = self._rows(athlete)
        mine = user_id(flask_app, "athlete.a@test.local")
        other = user_id(flask_app, "athlete.b@test.local")
        assert rows[other]["summary"]["latestReport"]["m"] is None
        assert rows[other]["summary"]["latest"] == 70          # public score
        assert rows[mine]["summary"]["latestReport"]["m"] == {k: 80 for k in METRICS}

    def test_coach_sees_metric_breakdowns_but_no_contact_details(
            self, flask_app, athlete, coach):
        add_reports(athlete, [66])
        rows = self._rows(coach)
        aid = user_id(flask_app, "athlete.a@test.local")
        assert rows[aid]["summary"]["latestReport"]["m"] == {k: 66 for k in METRICS}
        assert "email" not in rows[aid]
        assert "phone" not in rows[aid]

    def test_operator_sees_contact_details(self, flask_app, athlete, owner):
        aid = user_id(flask_app, "athlete.a@test.local")
        assert self._rows(owner)[aid]["email"] == "athlete.a@test.local"

    def test_directory_never_leaks_internal_columns(self, athlete, coach,
                                                    owner):
        for client in (athlete, coach, owner):
            for path in ("/api/directory/athletes", "/api/directory/coaches",
                         "/api/summary/platform"):
                text = client.get(path).get_data(as_text=True)
                for banned in ("pass_hash", "sess_epoch", "$2b$", "code_hash"):
                    assert banned not in text, (path, banned)

    def test_coach_directory_shows_only_the_callers_own_stars(
            self, flask_app, athlete, athlete_b, coach):
        cid = user_id(flask_app, "coach.a@test.local")
        assert athlete.post("/api/ratings",
                            {"coachId": cid, "stars": 5}).status_code == 200
        assert athlete_b.post("/api/ratings",
                              {"coachId": cid, "stars": 3}).status_code == 200

        mine = {c["id"]: c for c in
                athlete.get("/api/directory/coaches").get_json()["coaches"]}
        theirs = {c["id"]: c for c in
                  athlete_b.get("/api/directory/coaches").get_json()["coaches"]}
        assert mine[cid]["myStars"] == 5
        assert theirs[cid]["myStars"] == 3
        # The aggregate is public; who gave what is not.
        assert mine[cid]["rating"] == {"avg": 4.0, "count": 2}
        assert "athleteId" not in str(mine[cid])

    def test_message_counts_are_operator_only(self, flask_app, athlete, coach,
                                              owner):
        aid = user_id(flask_app, "athlete.a@test.local")
        coach.post("/api/messages", {"toId": aid, "text": "hello"})
        for client, expected in ((athlete, False), (coach, False),
                                 (owner, True)):
            rows = client.get("/api/directory/coaches").get_json()["coaches"]
            assert all(("messagesSent" in c) is expected for c in rows)


class TestPlatformSummaryScoping:

    def test_athlete_gets_no_operator_fields(self, athlete):
        summary = athlete.get("/api/summary/platform").get_json()["summary"]
        for field in ("feed", "messages", "notifications", "ratings",
                      "activeToday"):
            assert field not in summary, field
        assert set(summary) == {"leaders", "users", "reports", "avgOverall",
                                "daily"}

    def test_coach_gets_no_operator_fields(self, coach):
        summary = coach.get("/api/summary/platform").get_json()["summary"]
        assert "feed" not in summary and "activeToday" not in summary

    def test_operator_gets_the_feed(self, flask_app, athlete, coach, owner):
        add_reports(athlete, [72])
        aid = user_id(flask_app, "athlete.a@test.local")
        coach.post("/api/messages", {"toId": aid, "text": "scouting"})
        summary = owner.get("/api/summary/platform").get_json()["summary"]
        assert summary["activeToday"] >= 1
        kinds = {e["kind"] for e in summary["feed"]}
        assert {"report", "message"} <= kinds
        report_events = [e for e in summary["feed"] if e["kind"] == "report"]
        assert report_events[0]["name"] == "Athlete A"

    def test_the_feed_is_not_reachable_by_an_athlete(self, athlete, coach,
                                                     flask_app):
        aid = user_id(flask_app, "athlete.a@test.local")
        coach.post("/api/messages", {"toId": aid, "text": "private note"})
        body = athlete.get("/api/summary/platform").get_data(as_text=True)
        assert "private note" not in body


# ==========================================================================
# The numbers themselves
# ==========================================================================
class TestSummaryArithmetic:

    def test_derived_values_match_the_previous_client_side_formulas(
            self, flask_app, athlete):
        """Overalls 60 → 62 → 61 give, by the formulas the browser used:

            latest   = 61
            best     = 62
            progress = 61 - 60          = 1.0
            points   = 50 + round(2*2) + round(2*-1) = 50 + 4 - 2 = 52
        """
        add_reports(athlete, [60, 62, 61])
        me = user_id(flask_app, "athlete.a@test.local")
        rows = {a["id"]: a for a in
                athlete.get("/api/directory/athletes").get_json()["athletes"]}
        summary = rows[me]["summary"]
        assert summary["reports"] == 3
        assert summary["latest"] == 61
        assert summary["best"] == 62
        assert summary["progress"] == 1.0
        assert summary["points"] == 52
        # Everything was filed just now, so the weekly delta is the full run.
        assert summary["weekly"] == 2

    def test_an_athlete_with_no_reports_gets_a_zeroed_summary(self, athlete):
        rows = athlete.get("/api/directory/athletes").get_json()["athletes"]
        blank = [a for a in rows if a["summary"]["reports"] == 0]
        assert blank, "the fixture athlete should have no reports yet"
        assert blank[0]["summary"] == {
            "reports": 0, "latest": 0, "best": 0, "progress": 0,
            "points": 50, "weekly": 0, "lastDate": None, "latestReport": None}

    def test_platform_counters_match_the_data(self, athlete, athlete_b):
        add_reports(athlete, [70, 72])
        add_reports(athlete_b, [64])
        summary = athlete.get("/api/summary/platform").get_json()["summary"]
        assert summary["reports"]["total"] == 3
        assert summary["reports"]["today"] == 3
        assert summary["reports"]["week"] == 3
        assert summary["reports"]["live"] == 0
        assert summary["users"]["athletes"] == 2
        assert len(summary["daily"]) == 14
        assert sum(d["count"] for d in summary["daily"]) == 3
        # Mean of each athlete's LATEST score: (72 + 64) / 2.
        assert summary["avgOverall"] == 68.0

    def test_live_sessions_are_counted(self, athlete):
        athlete.post("/api/reports", {"m": {k: 70 for k in METRICS},
                                      "live": True})
        summary = athlete.get("/api/summary/platform").get_json()["summary"]
        assert summary["reports"]["live"] == 1

    def test_leaders_travel_with_the_summary(self, athlete, athlete_b):
        add_reports(athlete, [90])
        add_reports(athlete_b, [50])
        leaders = athlete.get("/api/summary/platform").get_json()["summary"]["leaders"]
        assert leaders["fastest"]["name"] == "Athlete A"


class TestPagination:

    def test_page_metadata_and_clamping(self, flask_app, athlete, athlete_b):
        body = athlete.get("/api/directory/athletes?limit=1").get_json()
        assert len(body["athletes"]) == 1
        assert body["page"]["limit"] == 1 and body["page"]["total"] == 2
        assert body["page"]["has_more"] is True
        assert body["page"]["next_offset"] == 1
        big = athlete.get("/api/directory/athletes?limit=99999").get_json()
        assert big["page"]["limit"] <= 500

    def test_paging_is_stable_and_covers_everyone(self, athlete, athlete_b):
        first = athlete.get("/api/directory/athletes?limit=1&offset=0").get_json()
        second = athlete.get("/api/directory/athletes?limit=1&offset=1").get_json()
        ids = [first["athletes"][0]["id"], second["athletes"][0]["id"]]
        assert len(set(ids)) == 2
        # Repeating a request returns the same row.
        again = athlete.get("/api/directory/athletes?limit=1&offset=0").get_json()
        assert again["athletes"][0]["id"] == ids[0]

    def test_hostile_paging_input_is_survivable(self, athlete):
        for qs in ("?limit=abc", "?limit=-5", "?offset=-1", "?limit=0",
                   "?offset=nonsense"):
            assert athlete.get("/api/directory/athletes" + qs).status_code == 200


# ==========================================================================
# Query cost — the N+1 claims, measured
# ==========================================================================
class TestQueryBudgets:

    def _count(self, counter, client, path):
        client.get(path)                 # warm the connection
        counter["n"] = 0
        assert client.get(path).status_code == 200
        return counter["n"]

    def test_my_reports_does_not_scale_with_report_count(self, athlete,
                                                         query_counter):
        add_reports(athlete, [60, 61])
        small = self._count(query_counter, athlete, "/api/my/reports")
        add_reports(athlete, [62, 63, 64, 65, 66, 67, 68, 69])
        large = self._count(query_counter, athlete, "/api/my/reports")
        assert large == small, (
            "one query per report is back: %d -> %d" % (small, large))

    def test_export_does_not_scale_with_report_count(self, athlete,
                                                     query_counter):
        add_reports(athlete, [60, 61])
        small = self._count(query_counter, athlete, "/api/export")
        add_reports(athlete, [62, 63, 64, 65, 66, 67, 68, 69])
        large = self._count(query_counter, athlete, "/api/export")
        assert large == small, (
            "export N+1 is back: %d -> %d" % (small, large))

    def test_athlete_directory_does_not_scale_with_athlete_count(
            self, flask_app, athlete, query_counter):
        small = self._count(query_counter, athlete, "/api/directory/athletes")
        for i in range(5):
            extra = make_client(flask_app)
            extra.signup("bulk%d@test.local" % i, ATHLETE_PW,
                         name="Bulk %d" % i, sport="Cricket", age=17)
        large = self._count(query_counter, athlete, "/api/directory/athletes")
        assert large == small, (
            "the directory grew a per-athlete query: %d -> %d"
            % (small, large))

    def test_coach_athlete_detail_does_not_scale_with_report_count(
            self, flask_app, athlete, coach, query_counter):
        aid = user_id(flask_app, "athlete.a@test.local")
        path = "/api/coach/athletes/%d" % aid
        add_reports(athlete, [60, 61])
        small = self._count(query_counter, coach, path)
        add_reports(athlete, [62, 63, 64, 65, 66])
        large = self._count(query_counter, coach, path)
        assert large == small

    def test_platform_summary_does_not_scale_with_data(self, athlete,
                                                       athlete_b,
                                                       query_counter):
        add_reports(athlete, [60])
        small = self._count(query_counter, athlete, "/api/summary/platform")
        add_reports(athlete, [61, 62, 63, 64])
        add_reports(athlete_b, [65, 66, 67])
        large = self._count(query_counter, athlete, "/api/summary/platform")
        assert large == small


class TestCompatibility:

    def test_api_state_still_works_unchanged(self, athlete, coach, owner):
        """/api/state is a compatibility endpoint, not a removed one."""
        for client in (athlete, coach, owner):
            body = client.get("/api/state").get_json()
            assert body["ok"] is True
            assert set(body["state"]) == {"users", "reports", "messages",
                                          "ratings", "notifs", "leaders"}

    def test_state_and_directory_agree_on_the_derived_scores(
            self, flask_app, athlete, coach):
        """The summary must equal what a client would have computed from the
        bulk payload - that is the whole compatibility claim."""
        add_reports(athlete, [60, 64, 62])
        aid = user_id(flask_app, "athlete.a@test.local")

        state = coach.get("/api/state").get_json()["state"]
        series = sorted([r for r in state["reports"] if r["athleteId"] == aid],
                        key=lambda r: (r["date"], r["id"]))
        expected_latest = series[-1]["overall"]
        expected_progress = round(series[-1]["overall"] - series[0]["overall"], 1)

        rows = {a["id"]: a for a in
                coach.get("/api/directory/athletes").get_json()["athletes"]}
        summary = rows[aid]["summary"]
        assert summary["latest"] == expected_latest
        assert summary["progress"] == expected_progress
        assert summary["reports"] == len(series)
        assert summary["best"] == max(r["overall"] for r in series)
