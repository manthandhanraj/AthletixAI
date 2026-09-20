# -*- coding: utf-8 -*-
"""Phase 2.6 - /api/v1 namespace and the API contract.

Two things are proved here:

1. `/api/v1/...` and `/api/...` are the SAME endpoint, not two
   implementations. Same handler object, same schema, same authorization,
   same rate-limit bucket, same response body.
2. Every privacy and authorization guarantee the Phase 1 paths carry is
   carried by the versioned paths too. A namespace that quietly dropped a
   check would be a privilege-escalation route.
"""

import pytest

from conftest import ATHLETE_PW, COACH_PW, make_client, user_id

V1 = "/api/v1"


def _both(path):
    """The same logical endpoint under both mounts."""
    return ["/api" + path, V1 + path]


# ==========================================================================
# Structural parity
# ==========================================================================
class TestNamespaceParity:

    @pytest.mark.parametrize("path", _both("/public/stats"))
    def test_public_endpoint_answers_on_both_mounts(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_public_payloads_are_identical(self, client):
        a = client.get("/api/public/stats").get_json()
        b = client.get(V1 + "/public/stats").get_json()
        assert a == b

    def test_state_payload_is_identical_across_mounts(self, athlete):
        a = athlete.get("/api/state").get_json()
        b = athlete.get(V1 + "/state").get_json()
        assert a == b

    def test_me_payload_is_identical_across_mounts(self, coach):
        assert (coach.get("/api/me").get_json()
                == coach.get(V1 + "/me").get_json())

    def test_session_from_v1_login_works_on_legacy_path(self, flask_app):
        c = make_client(flask_app)
        c.signup("v1.login@test.local", ATHLETE_PW, name="V1 Login")
        c.refresh_csrf()
        r = c.post(V1 + "/auth/login",
                   {"email": "v1.login@test.local", "password": ATHLETE_PW})
        assert r.status_code == 200
        c.csrf = r.get_json()["csrf"]
        assert c.get("/api/me").status_code == 200
        assert c.get(V1 + "/me").status_code == 200

    def test_full_signup_verify_login_flow_over_v1(self, flask_app):
        c = make_client(flask_app)
        c.refresh_csrf()
        r = c.post(V1 + "/auth/signup",
                   {"role": "athlete", "name": "V1 Flow",
                    "email": "v1.flow@test.local", "password": ATHLETE_PW})
        assert r.status_code == 200
        code = r.get_json()["dev_code"]
        c.refresh_csrf()
        v = c.post(V1 + "/auth/verify",
                   {"email": "v1.flow@test.local", "code": code})
        assert v.status_code == 200
        c.refresh_csrf()
        li = c.post(V1 + "/auth/login",
                    {"email": "v1.flow@test.local", "password": ATHLETE_PW})
        assert li.status_code == 200 and li.get_json()["ok"] is True

    def test_v1_errors_carry_the_same_shape_and_status(self, client):
        legacy = client.get("/api/me")
        versioned = client.get(V1 + "/me")
        assert legacy.status_code == versioned.status_code == 401
        assert legacy.get_json() == versioned.get_json()


# ==========================================================================
# Security controls are not bypassable by choosing a namespace
# ==========================================================================
class TestVersionedSecurityControls:

    @pytest.mark.parametrize("path", [
        "/me", "/state", "/export", "/my/reports", "/my/messages",
        "/my/notifications", "/admin/users", "/admin/metrics",
        "/admin/activity", "/coach/athletes",
    ])
    def test_v1_reads_require_authentication(self, client, path):
        assert client.get(V1 + path).status_code == 401

    @pytest.mark.parametrize("path", [
        "/reports", "/messages", "/notifications", "/ratings",
        "/profile/update", "/profile/photo", "/profile/password",
        "/profile/delete", "/consent", "/coach/profile",
        "/auth/logout-all",
    ])
    def test_v1_writes_require_authentication(self, client, path):
        client.refresh_csrf()
        assert client.post(V1 + path, {}).status_code == 401

    @pytest.mark.parametrize("path", ["/auth/login", "/reports", "/messages"])
    def test_v1_is_not_csrf_exempt(self, client, path):
        r = client.post(V1 + path, {"email": "x@test.local"}, csrf=None)
        assert r.status_code == 403
        assert "CSRF" in (r.get_json() or {}).get("error", "")

    def test_v1_admin_endpoints_reject_an_athlete(self, athlete):
        for path in ("/admin/users", "/admin/metrics", "/admin/activity"):
            assert athlete.get(V1 + path).status_code == 403

    def test_role_change_is_owner_only_on_v1(self, flask_app, coach, admin):
        target = user_id(flask_app, "coach.a@test.local")
        r = admin.post(V1 + "/admin/users/%d/role" % target, {"role": "admin"})
        assert r.status_code == 403

    def test_rate_limit_counters_are_shared_across_mounts(self, flask_app):
        """The per-subject login budget must not double just because the
        caller alternates between /api and /api/v1."""
        c = make_client(flask_app)
        c.signup("shared.limit@test.local", ATHLETE_PW, name="Shared Limit")
        statuses = []
        for i in range(12):
            base = "/api" if i % 2 == 0 else V1
            c.refresh_csrf()
            statuses.append(c.post(base + "/auth/login",
                                   {"email": "shared.limit@test.local",
                                    "password": "WrongPassword!1"}).status_code)
        assert 429 in statuses, statuses
        # Switching namespace after the limit bites must not reset it.
        c.refresh_csrf()
        assert c.post(V1 + "/auth/login",
                      {"email": "shared.limit@test.local",
                       "password": ATHLETE_PW}).status_code == 429

    def test_v1_responses_are_not_cacheable(self, athlete):
        r = athlete.get(V1 + "/me")
        assert r.headers.get("Cache-Control") == "no-store, private"

    def test_v1_body_size_cap_still_applies(self, athlete):
        big = {"text": "x" * (1024 * 1024 + 100), "toId": 1}
        r = athlete.post(V1 + "/messages", big)
        assert r.status_code == 413

    def test_v1_photo_endpoint_keeps_its_large_body_exemption(self, athlete):
        """The photo exemption must exist in BOTH namespaces, or the same
        upload succeeds on one path and 413s on the other."""
        payload = {"photo": "data:image/png;base64," + ("A" * 400000)}
        r = athlete.post(V1 + "/profile/photo", payload)
        # Rejected on content (not valid base64 image data), not on size.
        assert r.status_code == 400
        assert r.get_json()["error"] != "Request body is too large."


# ==========================================================================
# Privacy, proved on the versioned namespace as well
# ==========================================================================
class TestVersionedPrivacy:

    def _users_by_id(self, payload):
        return {u["id"]: u for u in payload["state"]["users"]}

    def test_athlete_sees_own_contact_details_but_not_another_athletes(
            self, flask_app, athlete, athlete_b):
        me = user_id(flask_app, "athlete.a@test.local")
        other = user_id(flask_app, "athlete.b@test.local")
        users = self._users_by_id(athlete.get(V1 + "/state").get_json())
        assert users[me]["email"] == "athlete.a@test.local"
        assert "email" not in users[other]
        assert "phone" not in users[other]
        assert "last_login" not in users[other]

    def test_coach_sees_athletes_without_contact_details(
            self, flask_app, athlete, coach):
        athlete_uid = user_id(flask_app, "athlete.a@test.local")
        r = coach.get(V1 + "/coach/athletes")
        assert r.status_code == 200
        rows = {a["id"]: a for a in r.get_json()["athletes"]}
        assert athlete_uid in rows
        assert "email" not in rows[athlete_uid]
        assert "phone" not in rows[athlete_uid]

    def test_owner_sees_contact_details(self, flask_app, athlete, owner):
        athlete_uid = user_id(flask_app, "athlete.a@test.local")
        rows = {u["id"]: u for u in
                owner.get(V1 + "/admin/users").get_json()["users"]}
        assert rows[athlete_uid]["email"] == "athlete.a@test.local"

    def test_private_metrics_are_withheld_from_another_athlete(
            self, flask_app, athlete, athlete_b):
        athlete.post(V1 + "/reports", {"m": {"speed": 70, "agility": 70,
                                             "strength": 70, "stamina": 70,
                                             "technique": 70}})
        state = athlete_b.get(V1 + "/state").get_json()["state"]
        mine = user_id(flask_app, "athlete.a@test.local")
        foreign = [r for r in state["reports"] if r["athleteId"] == mine]
        assert foreign, "the leaderboard row should still be visible"
        for report in foreign:
            assert report["m"] is None
            assert report["ai"] is None

    def test_report_detail_is_ownership_scoped_on_v1(self, athlete, athlete_b):
        created = athlete.post(V1 + "/reports",
                               {"m": {"speed": 80, "agility": 80,
                                      "strength": 80, "stamina": 80,
                                      "technique": 80}}).get_json()
        rid = created["report"]["id"]
        assert athlete.get(V1 + "/reports/%d" % rid).status_code == 200
        # 404, not 403: a 403 would confirm the report exists.
        assert athlete_b.get(V1 + "/reports/%d" % rid).status_code == 404

    def test_export_only_ever_returns_the_caller(self, flask_app, athlete,
                                                 athlete_b):
        payload = athlete.get(V1 + "/export").get_json()
        assert payload["account"]["email"] == "athlete.a@test.local"
        assert "athlete.b@test.local" not in str(payload)

    def test_rating_attribution_is_hidden_from_other_athletes(
            self, flask_app, athlete, athlete_b, coach):
        coach_uid = user_id(flask_app, "coach.a@test.local")
        assert athlete.post(V1 + "/ratings",
                            {"coachId": coach_uid, "stars": 5}).status_code == 200
        rows = athlete_b.get(V1 + "/state").get_json()["state"]["ratings"]
        assert rows and all(r["byId"] is None for r in rows)
        own = athlete.get(V1 + "/state").get_json()["state"]["ratings"]
        assert own[0]["byId"] == user_id(flask_app, "athlete.a@test.local")

    def test_messages_are_only_visible_to_their_parties(
            self, flask_app, athlete, athlete_b, coach):
        athlete_uid = user_id(flask_app, "athlete.a@test.local")
        assert coach.post(V1 + "/messages",
                          {"toId": athlete_uid,
                           "text": "private scouting note"}).status_code == 200
        assert "private scouting note" in str(
            athlete.get(V1 + "/my/messages").get_json())
        assert "private scouting note" not in str(
            athlete_b.get(V1 + "/my/messages").get_json())
        assert "private scouting note" not in str(
            athlete_b.get(V1 + "/state").get_json())
