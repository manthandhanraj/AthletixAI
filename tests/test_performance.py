"""Phase 2.4 — performance work must not change semantics."""
import pytest

from conftest import appmod, user_id
from athletix.repositories import reports as report_repo
from athletix.repositories import users as user_repo
from athletix.serializers import (report_public, serialize_users, user_public)


class TestBatchSerializationEquivalence:
    """The batched path must produce byte-identical output to the per-row
    path it replaced. This is the guard for the N+1 rewrite."""

    def _rows(self, flask_app):
        return appmod.get_db().execute("SELECT * FROM users ORDER BY id").fetchall()

    @pytest.mark.parametrize("viewer_role", ["athlete", "coach", "owner", "admin"])
    def test_serialize_users_matches_per_row(self, flask_app, athlete,
                                             athlete_b, coach, owner, admin,
                                             viewer_role):
        clients = {"athlete": athlete, "coach": coach, "owner": owner,
                   "admin": admin}
        with flask_app.app_context():
            rows = self._rows(flask_app)
            viewer = appmod.get_db().execute(
                "SELECT * FROM users WHERE id=?",
                (clients[viewer_role].user["id"],)).fetchone()
            batched = serialize_users(rows, viewer)
            per_row = [user_public(r, viewer) for r in rows]
        assert batched == per_row

    def test_serialize_users_with_no_viewer_filtering(self, flask_app, athlete):
        with flask_app.app_context():
            rows = self._rows(flask_app)
            assert serialize_users(rows, None) == [user_public(r, None)
                                                   for r in rows]

    def test_report_public_preloaded_ai_matches_lookup(self, flask_app, athlete):
        athlete.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS},
                                      "ai": {"potential": "High", "medal": 50,
                                             "risk": "Low", "best": "Cricket"}})
        athlete.post("/api/reports", {"m": {k: 60 for k in appmod.METRICS}})
        with flask_app.app_context():
            rows = report_repo.list_for_athlete(athlete.user["id"])
            preloaded = report_repo.ai_results_for_athlete(athlete.user["id"])
            for r in rows:
                assert (report_public(r, True, ai=preloaded.get(r["id"]))
                        == report_public(r, True))

    def test_missing_profile_row_still_omits_fields(self, flask_app, athlete):
        """Batch loader returns None for a user with no profile row; the
        serializer must behave as it did with a failed lookup."""
        uid = user_id(flask_app, "athlete.a@test.local")
        with flask_app.app_context():
            db = appmod.get_db()
            db.execute("DELETE FROM athlete_profiles WHERE user_id=?", (uid,))
            db.commit()
            rows = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchall()
            batched = serialize_users(rows, None)[0]
            per_row = user_public(rows[0], None)
        assert batched == per_row
        for f in ("sport", "age", "location"):
            assert f not in batched


class TestScopedStateStillScoped:
    """Re-assert the privacy guarantees specifically against the batched path."""

    def test_athlete_sees_no_foreign_ai_or_metrics(self, athlete, athlete_b):
        athlete_b.post("/api/reports", {"m": {k: 60 for k in appmod.METRICS},
                                        "ai": {"potential": "Elite",
                                               "medal": 90, "risk": "Low",
                                               "best": "Football"}})
        athlete.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS}})
        state = athlete.state()
        me = athlete.user["id"]
        for r in state["reports"]:
            if r["athleteId"] != me:
                assert r["m"] is None and r["ai"] is None
        assert "Elite" not in str(state["reports"])

    def test_coach_still_sees_metrics(self, coach, athlete):
        athlete.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS}})
        assert any(r["m"] for r in coach.state()["reports"])

    def test_batch_loading_does_not_leak_contact_details(self, athlete,
                                                         athlete_b, coach):
        for u in athlete.state()["users"]:
            if u["id"] != athlete.user["id"]:
                for f in ("email", "phone", "verified", "last_login"):
                    assert f not in u

    def test_leaders_block_unchanged_by_the_reuse(self, athlete, athlete_b):
        athlete.post("/api/reports", {"m": {k: 90 for k in appmod.METRICS}})
        athlete_b.post("/api/reports", {"m": {k: 50 for k in appmod.METRICS}})
        leaders = athlete.state()["leaders"]
        for key in ("mostImproved", "fastest", "strongest"):
            if leaders.get(key):
                assert set(leaders[key]) == {"id", "name", "value"}


class TestPagination:
    """2.4C: bounded collections, stable ordering, additive contract."""

    PAGED = [
        ("/api/my/reports", "reports"),
        ("/api/my/messages", "messages"),
        ("/api/my/notifications", "notifications"),
    ]

    def test_page_metadata_shape(self, athlete):
        for path, key in self.PAGED:
            body = athlete.json(athlete.get(path))
            assert key in body, path
            p = body["page"]
            assert set(p) == {"limit", "offset", "total", "returned",
                              "has_more", "next_offset"}, path

    def test_data_key_and_shape_unchanged(self, athlete):
        """Backward compatibility: the collection keeps its original key."""
        athlete.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS}})
        body = athlete.json(athlete.get("/api/my/reports"))
        assert isinstance(body["reports"], list)
        assert {"id", "athleteId", "date", "overall"} <= set(body["reports"][0])

    def test_limit_is_honoured_and_clamped(self, athlete):
        for _ in range(5):
            athlete.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS}})
        body = athlete.json(athlete.get("/api/my/reports?limit=2"))
        assert len(body["reports"]) == 2 and body["page"]["limit"] == 2
        big = athlete.json(athlete.get("/api/my/reports?limit=99999"))
        assert big["page"]["limit"] <= 500

    @pytest.mark.parametrize("qs", ["limit=abc", "limit=-5", "limit=0",
                                    "offset=-1", "offset=abc",
                                    "limit=1;DROP TABLE users"])
    def test_hostile_paging_input_falls_back_safely(self, athlete, qs):
        r = athlete.get("/api/my/reports?" + qs)
        assert r.status_code == 200
        p = r.get_json()["page"]
        assert 1 <= p["limit"] <= 500 and p["offset"] >= 0

    def test_paging_is_deterministic_and_covers_everything(self, athlete):
        for _ in range(7):
            athlete.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS}})
        seen, offset = [], 0
        while True:
            body = athlete.json(
                athlete.get("/api/my/reports?limit=3&offset=%d" % offset))
            seen += [r["id"] for r in body["reports"]]
            if not body["page"]["has_more"]:
                break
            offset = body["page"]["next_offset"]
        assert len(seen) == len(set(seen)), "duplicate rows across pages"
        assert len(seen) == body["page"]["total"], "rows lost between pages"

    def test_pagination_does_not_widen_scope(self, athlete, athlete_b, coach):
        """A paged read must still be scoped to the caller."""
        coach.post("/api/messages", {"toId": athlete_b.user["id"],
                                     "text": "for b only"})
        body = athlete.json(athlete.get("/api/my/messages?limit=500"))
        me = athlete.user["id"]
        assert all(m["toId"] == me or m["fromId"] == me
                   for m in body["messages"])
        assert "for b only" not in str(body)

    def test_admin_endpoints_paginate(self, owner):
        for path, key in (("/api/admin/users", "users"),
                          ("/api/admin/activity", "activity")):
            body = owner.json(owner.get(path + "?limit=2"))
            assert len(body[key]) <= 2 and body["page"]["limit"] == 2

    def test_coach_athletes_paginates(self, coach, athlete, athlete_b):
        body = coach.json(coach.get("/api/coach/athletes?limit=1"))
        assert len(body["athletes"]) == 1
        assert body["page"]["total"] >= 2

    def test_state_is_deliberately_not_paginated(self, athlete):
        """The dashboard hydrates from the complete scoped set; paginating
        /api/state would break it. Documented, not an oversight."""
        state = athlete.state()
        assert set(state) == {"users", "reports", "messages", "ratings",
                              "notifs", "leaders"}
