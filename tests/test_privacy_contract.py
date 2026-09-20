"""Characterization tests for the Phase 1 privacy boundary.

These pin the EXACT observable behaviour of the viewer-aware serializers
before Phase 2 moves them into a package. They are deliberately written
against behaviour, not implementation: they call the serializer through the
package's public name and assert on the dict it returns, so the same file
keeps working after the code is relocated.

If a refactor ever re-exposes an e-mail address, a phone number or another
athlete's metric breakdown, one of these fails.
"""
import pytest

from conftest import ATHLETE_PW, COACH_PW, appmod, make_client, user_id


CONTACT_FIELDS = ("email", "phone", "verified", "created_at", "last_login")
ATHLETE_PUBLIC = ("sport", "age", "location")
COACH_PUBLIC = ("specialty", "bio", "experience", "city", "achievements")
BASE_FIELDS = ("id", "role", "name", "photo")


def _row(flask_app, email):
    return appmod.get_db().execute(
        "SELECT * FROM users WHERE email=?", (email,)).fetchone()


@pytest.fixture
def cast(flask_app, athlete, athlete_b, coach, owner, admin):
    """One of every role, all verified and logged in."""
    return {
        "athlete": "athlete.a@test.local",
        "athlete_b": "athlete.b@test.local",
        "coach": "coach.a@test.local",
        "owner": "owner@test.local",
        "admin": "admin.a@test.local",
    }


class TestUserPublicContract:
    """user_public(row, viewer) — the single most security-sensitive function."""

    def test_self_view_includes_own_contact_details(self, flask_app, cast):
        with flask_app.app_context():
            me = _row(flask_app, cast["athlete"])
            out = appmod.user_public(me, me)
        for f in BASE_FIELDS + CONTACT_FIELDS:
            assert f in out, f
        assert out["email"] == cast["athlete"]

    def test_viewer_none_is_unfiltered(self, flask_app, cast):
        """viewer=None means 'no audience filtering' - used for the caller's
        own record. It must stay unfiltered, and must never be reachable
        with another user's row from a request path."""
        with flask_app.app_context():
            me = _row(flask_app, cast["athlete"])
            out = appmod.user_public(me)
        for f in CONTACT_FIELDS:
            assert f in out, f

    def test_athlete_viewing_another_athlete_gets_no_contact_details(
            self, flask_app, cast):
        with flask_app.app_context():
            viewer = _row(flask_app, cast["athlete"])
            target = _row(flask_app, cast["athlete_b"])
            out = appmod.user_public(target, viewer)
        for f in CONTACT_FIELDS:
            assert f not in out, "leaked %s" % f
        for f in BASE_FIELDS:
            assert f in out, f
        for f in ATHLETE_PUBLIC:
            assert f in out, "directory field %s should remain" % f

    def test_athlete_viewing_coach_gets_profile_but_no_contact(self, flask_app, cast):
        with flask_app.app_context():
            viewer = _row(flask_app, cast["athlete"])
            target = _row(flask_app, cast["coach"])
            out = appmod.user_public(target, viewer)
        for f in CONTACT_FIELDS:
            assert f not in out, "leaked %s" % f
        for f in COACH_PUBLIC:
            assert f in out, "coach directory field %s should remain" % f

    def test_coach_viewing_athlete_gets_performance_fields_not_contact(
            self, flask_app, cast):
        """The product decision from Phase 1: coaches scout performance,
        they do not get contact details."""
        with flask_app.app_context():
            viewer = _row(flask_app, cast["coach"])
            target = _row(flask_app, cast["athlete"])
            out = appmod.user_public(target, viewer)
        for f in CONTACT_FIELDS:
            assert f not in out, "leaked %s to a coach" % f
        for f in ATHLETE_PUBLIC:
            assert f in out, f

    @pytest.mark.parametrize("operator", ["owner", "admin"])
    def test_operators_do_see_contact_details(self, flask_app, cast, operator):
        with flask_app.app_context():
            viewer = _row(flask_app, cast[operator])
            target = _row(flask_app, cast["athlete"])
            out = appmod.user_public(target, viewer)
        for f in CONTACT_FIELDS:
            assert f in out, "operator lost %s" % f
        assert out["email"] == cast["athlete"]

    def test_never_serializes_credential_or_session_columns(self, flask_app, cast):
        """Whatever else changes, these columns must never appear."""
        with flask_app.app_context():
            viewer = _row(flask_app, cast["athlete"])
            for who in ("athlete", "athlete_b", "coach", "owner", "admin"):
                target = _row(flask_app, cast[who])
                for v in (None, viewer):
                    out = appmod.user_public(target, v)
                    for banned in ("pass_hash", "password", "sess_epoch"):
                        assert banned not in out, (who, banned)

    def test_missing_athlete_profile_row_does_not_crash_or_invent_fields(
            self, flask_app, cast):
        with flask_app.app_context():
            db = appmod.get_db()
            uid = user_id(flask_app, cast["athlete"])
            db.execute("DELETE FROM athlete_profiles WHERE user_id=?", (uid,))
            db.commit()
            out = appmod.user_public(_row(flask_app, cast["athlete"]))
        for f in ATHLETE_PUBLIC:
            assert f not in out, "invented %s with no profile row" % f
        assert out["id"] == uid

    def test_missing_coach_profile_row_does_not_crash_or_invent_fields(
            self, flask_app, cast):
        with flask_app.app_context():
            db = appmod.get_db()
            uid = user_id(flask_app, cast["coach"])
            db.execute("DELETE FROM coach_profiles WHERE user_id=?", (uid,))
            db.commit()
            out = appmod.user_public(_row(flask_app, cast["coach"]))
        for f in COACH_PUBLIC:
            assert f not in out, "invented %s with no profile row" % f

    def test_null_photo_and_phone_normalise_to_empty_string(self, flask_app, cast):
        with flask_app.app_context():
            db = appmod.get_db()
            uid = user_id(flask_app, cast["athlete"])
            db.execute("UPDATE users SET photo=NULL, phone=NULL WHERE id=?", (uid,))
            db.commit()
            out = appmod.user_public(_row(flask_app, cast["athlete"]))
        assert out["photo"] == ""
        assert out["phone"] == ""

    def test_is_privileged_matches_the_role_table(self, flask_app, cast):
        with flask_app.app_context():
            expected = {"athlete": False, "athlete_b": False, "coach": False,
                        "owner": True, "admin": True}
            for who, want in expected.items():
                assert appmod.is_privileged(_row(flask_app, cast[who])) is want, who
        assert appmod.is_privileged(None) is False


class TestReportPrivacyContract:
    """report_public(row, detail) and can_view_report_detail(viewer, athlete_id)."""

    def _make_report(self, c):
        return c.json(c.post("/api/reports", {"m": {k: 70 for k in appmod.METRICS}}))["report"]

    def test_detail_false_strips_metrics_and_ai(self, flask_app, athlete):
        rep = self._make_report(athlete)
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT * FROM reports WHERE id=?", (rep["id"],)).fetchone()
            lean = appmod.report_public(row, detail=False)
            full = appmod.report_public(row, detail=True)
        assert lean["m"] is None and lean["ai"] is None
        for f in ("id", "athleteId", "date", "sport", "overall", "live"):
            assert f in lean, f
        assert full["m"] is not None

    def test_can_view_report_detail_matrix(self, flask_app, cast):
        with flask_app.app_context():
            aid = user_id(flask_app, cast["athlete"])
            allowed = {"athlete": True, "athlete_b": False, "coach": True,
                       "owner": True, "admin": True}
            for who, want in allowed.items():
                v = _row(flask_app, cast[who])
                assert appmod.can_view_report_detail(v, aid) is want, who
            assert appmod.can_view_report_detail(None, aid) is False


class TestScopedStateContract:
    """scoped_state(user) - the payload the whole dashboard renders from."""

    def test_response_shape_is_stable(self, athlete):
        state = athlete.state()
        assert set(state.keys()) == {"users", "reports", "messages",
                                     "ratings", "notifs", "leaders"}

    def test_athlete_sees_no_foreign_contact_or_metrics(self, athlete, athlete_b, coach):
        athlete_b.post("/api/reports", {"m": {k: 60 for k in appmod.METRICS}})
        state = athlete.state()
        me = athlete.user["id"]
        for u in state["users"]:
            if u["id"] != me:
                for f in CONTACT_FIELDS:
                    assert f not in u, "leaked %s about user %s" % (f, u["id"])
        for r in state["reports"]:
            if r["athleteId"] != me:
                assert r["m"] is None and r["ai"] is None

    def test_coach_sees_metrics_but_no_contact(self, coach, athlete):
        athlete.post("/api/reports", {"m": {k: 60 for k in appmod.METRICS}})
        state = coach.state()
        for u in state["users"]:
            if u["id"] != coach.user["id"]:
                for f in CONTACT_FIELDS:
                    assert f not in u, "leaked %s to coach" % f
        assert any(r["m"] for r in state["reports"]), "coach lost metric access"

    def test_operator_sees_contact_details(self, owner, athlete):
        state = owner.state()
        others = [u for u in state["users"] if u["id"] != owner.user["id"]]
        assert others and all("email" in u for u in others)

    def test_messages_and_notifications_are_scoped(self, athlete, athlete_b, coach):
        coach.post("/api/messages", {"toId": athlete_b.user["id"], "text": "for b"})
        state = athlete.state()
        me = athlete.user["id"]
        assert all(m["toId"] == me or m["fromId"] == me for m in state["messages"])
        assert all(n["toId"] == me for n in state["notifs"])

    def test_rating_authorship_is_hidden_from_peers(self, athlete, athlete_b, coach):
        athlete_b.post("/api/ratings", {"coachId": coach.user["id"], "stars": 3})
        state = athlete.state()
        foreign = [r for r in state["ratings"] if r["stars"] == 3]
        assert foreign and all(r["byId"] is None for r in foreign)

    def test_leaders_block_exposes_only_name_id_value(self, athlete):
        leaders = athlete.state()["leaders"]
        for key in ("mostImproved", "fastest", "strongest"):
            if leaders.get(key):
                assert set(leaders[key].keys()) == {"id", "name", "value"}
