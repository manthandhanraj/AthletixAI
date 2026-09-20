"""AthletixAI — automated security test suite (Phase 1).

Each test names the vulnerability it locks down. They are written as attacks:
every one of them PASSED against the pre-hardening code, which is why they
exist.
"""
import json

import pytest

from conftest import (ATHLETE_PW, COACH_PW, appmod, make_client, user_id)


# ==========================================================================
# RBAC / privilege escalation
# ==========================================================================
class TestPrivilegeEscalation:

    @pytest.mark.parametrize("role", ["admin", "owner", "ADMIN", "Owner",
                                      "superuser", "", None, 1, ["admin"],
                                      {"role": "admin"}])
    def test_public_signup_cannot_create_privileged_roles(self, client, role):
        resp, data = client.signup("escalate@test.local", ATHLETE_PW,
                                   role=role, verify=False)
        assert resp.status_code == 400, "role=%r was accepted" % (role,)
        assert data["ok"] is False
        assert user_id(client.c.application, "escalate@test.local") is None

    def test_public_signup_allows_athlete(self, client):
        resp, data = client.signup("newathlete@test.local", ATHLETE_PW,
                                   role="athlete", verify=False)
        assert resp.status_code == 200 and data["ok"] is True

    def test_public_signup_allows_coach(self, client):
        resp, data = client.signup("newcoach@test.local", COACH_PW,
                                   role="coach", verify=False)
        assert resp.status_code == 200 and data["ok"] is True

    def test_signup_cannot_squat_the_owner_email(self, client):
        resp, _ = client.signup("owner@test.local", ATHLETE_PW,
                                role="athlete", verify=False)
        assert resp.status_code == 403

    def test_signup_cannot_self_verify_or_preset_fields(self, flask_app, client):
        client.signup("sneaky@test.local", ATHLETE_PW, role="athlete",
                      verify=False, verified=1, id=1, sess_epoch=99)
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT * FROM users WHERE email=?",
                ("sneaky@test.local",)).fetchone()
        assert row["verified"] == 0
        assert row["role"] == "athlete"

    def test_athlete_cannot_reach_admin_endpoints(self, athlete):
        for path in ("/api/admin/metrics", "/api/admin/users",
                     "/api/admin/activity"):
            assert athlete.get(path).status_code == 403, path

    def test_coach_cannot_reach_admin_endpoints(self, coach):
        assert coach.get("/api/admin/users").status_code == 403

    def test_athlete_cannot_promote_anyone(self, flask_app, athlete):
        target = user_id(flask_app, "athlete.a@test.local")
        r = athlete.post("/api/admin/users/%d/role" % target, {"role": "admin"})
        assert r.status_code == 403

    def test_admin_cannot_promote_users_only_owner_can(self, flask_app, admin,
                                                       athlete):
        target = user_id(flask_app, "athlete.a@test.local")
        assert admin.post("/api/admin/users/%d/role" % target,
                          {"role": "admin"}).status_code == 403

    def test_owner_cannot_mint_a_second_owner(self, flask_app, owner, athlete):
        target = user_id(flask_app, "athlete.a@test.local")
        r = owner.post("/api/admin/users/%d/role" % target, {"role": "owner"})
        assert r.status_code == 400

    def test_role_change_revokes_the_targets_sessions(self, flask_app, owner,
                                                      athlete):
        assert athlete.get("/api/me").status_code == 200
        target = user_id(flask_app, "athlete.a@test.local")
        assert owner.post("/api/admin/users/%d/role" % target,
                          {"role": "coach"}).status_code == 200
        assert athlete.get("/api/me").status_code == 401

    def test_unauthenticated_gets_401_not_data(self, client):
        for path in ("/api/state", "/api/me", "/api/my/reports",
                     "/api/my/messages", "/api/my/notifications",
                     "/api/export", "/api/coach/athletes"):
            assert client.get(path).status_code == 401, path


# ==========================================================================
# Authentication
# ==========================================================================
class TestAuthentication:

    def test_wrong_password_is_rejected(self, client):
        client.signup("auth1@test.local", ATHLETE_PW)
        resp, _ = client.login("auth1@test.local", "WrongPassword!1")
        assert resp.status_code == 401

    def test_unknown_account_is_rejected_with_the_same_message(self, client):
        client.signup("auth2@test.local", ATHLETE_PW)
        _, wrong_pw = client.login("auth2@test.local", "WrongPassword!1")
        _, no_user = client.login("nobody@test.local", "WrongPassword!1")
        assert wrong_pw["error"] == no_user["error"]

    def test_unverified_account_cannot_log_in(self, client):
        client.signup("unver@test.local", ATHLETE_PW, verify=False)
        resp, data = client.login("unver@test.local", ATHLETE_PW)
        assert resp.status_code == 403
        assert data.get("need_verify") is True
        assert client.get("/api/me").status_code == 401

    def test_verification_actually_flips_the_flag(self, flask_app, client):
        _, data = client.signup("ver@test.local", ATHLETE_PW, verify=False)
        with flask_app.app_context():
            db = appmod.get_db()
            assert db.execute("SELECT verified FROM users WHERE email=?",
                              ("ver@test.local",)).fetchone()[0] == 0
        client.refresh_csrf()
        assert client.post("/api/auth/verify",
                           {"email": "ver@test.local",
                            "code": data["dev_code"]}).status_code == 200
        with flask_app.app_context():
            assert appmod.get_db().execute(
                "SELECT verified FROM users WHERE email=?",
                ("ver@test.local",)).fetchone()[0] == 1
        assert client.login("ver@test.local", ATHLETE_PW)[0].status_code == 200

    def test_wrong_verification_code_fails(self, client):
        client.signup("badcode@test.local", ATHLETE_PW, verify=False)
        client.refresh_csrf()
        r = client.post("/api/auth/verify",
                        {"email": "badcode@test.local", "code": "000000"})
        assert r.status_code == 400

    def test_verification_code_is_single_use(self, client):
        _, data = client.signup("once@test.local", ATHLETE_PW, verify=False)
        code = data["dev_code"]
        client.refresh_csrf()
        assert client.post("/api/auth/verify",
                           {"email": "once@test.local",
                            "code": code}).status_code == 200
        client.refresh_csrf()
        assert client.post("/api/auth/verify",
                           {"email": "once@test.local",
                            "code": code}).status_code == 400

    def test_expired_verification_code_fails(self, flask_app, client):
        _, data = client.signup("exp@test.local", ATHLETE_PW, verify=False)
        with flask_app.app_context():
            db = appmod.get_db()
            db.execute("UPDATE email_tokens SET expires_at='2000-01-01T00:00:00'")
            db.commit()
        client.refresh_csrf()
        assert client.post("/api/auth/verify",
                           {"email": "exp@test.local",
                            "code": data["dev_code"]}).status_code == 400

    def test_otp_dies_after_repeated_wrong_guesses(self, client):
        _, data = client.signup("brute@test.local", ATHLETE_PW, verify=False)
        real = data["dev_code"]
        for _ in range(appmod.TOKEN_MAX_ATTEMPTS):
            client.refresh_csrf()
            client.post("/api/auth/verify",
                        {"email": "brute@test.local", "code": "999999"})
        client.refresh_csrf()
        # The real code no longer works: the token was burned by the guessing.
        assert client.post("/api/auth/verify",
                           {"email": "brute@test.local",
                            "code": real}).status_code == 400

    def test_otps_are_not_stored_in_plaintext(self, flask_app, client):
        _, data = client.signup("hashed@test.local", ATHLETE_PW, verify=False)
        with flask_app.app_context():
            rows = appmod.get_db().execute(
                "SELECT * FROM email_tokens").fetchall()
        blob = json.dumps([dict(r) for r in rows])
        assert data["dev_code"] not in blob
        assert appmod.sha256(data["dev_code"]) in blob

    def test_password_hash_is_never_returned(self, athlete):
        payloads = [athlete.get("/api/me").get_data(as_text=True),
                    athlete.get("/api/session").get_data(as_text=True),
                    athlete.get("/api/state").get_data(as_text=True),
                    athlete.get("/api/export").get_data(as_text=True)]
        for text in payloads:
            low = text.lower()
            assert "pass_hash" not in low
            assert "$2b$" not in text and "$2a$" not in text
            assert ATHLETE_PW not in text

    def test_login_is_rate_limited(self, client):
        client.signup("rl@test.local", ATHLETE_PW)
        limit = appmod.RATE_RULES["login"][0]
        codes = []
        for _ in range(limit + 3):
            codes.append(client.login("rl@test.local", "Nope!12345")[0].status_code)
        assert 429 in codes

    def test_signup_is_rate_limited(self, client):
        """Each signup uses a fresh e-mail, so only the per-IP tier can
        stop mass registration."""
        limit = appmod.RATE_RULES["signup"][1]
        codes = []
        for i in range(limit + 3):
            codes.append(client.signup("bulk%d@test.local" % i, ATHLETE_PW,
                                       verify=False)[0].status_code)
        assert 429 in codes


class TestPasswordPolicy:

    @pytest.mark.parametrize("pw", ["short", "1234567", "", "aaaaaaaa",
                                    "password", "12345678", "a" * 200])
    def test_weak_passwords_rejected_at_signup(self, client, pw):
        resp, _ = client.signup("weak@test.local", pw, verify=False)
        assert resp.status_code == 400, "accepted %r" % pw

    def test_reasonable_password_accepted(self, client):
        resp, _ = client.signup("ok@test.local", "correct horse battery",
                                verify=False)
        assert resp.status_code == 200

    def test_weak_password_rejected_at_reset(self, client):
        client.signup("wr@test.local", ATHLETE_PW)
        client.refresh_csrf()
        code = client.json(client.post("/api/auth/forgot",
                                       {"email": "wr@test.local"}))["dev_code"]
        client.refresh_csrf()
        r = client.post("/api/auth/reset", {"email": "wr@test.local",
                                            "code": code, "password": "abc"})
        assert r.status_code == 400


class TestPasswordReset:

    def _request_code(self, client, email):
        client.refresh_csrf()
        return client.json(client.post("/api/auth/forgot",
                                       {"email": email}))["dev_code"]

    def test_reset_works_and_the_new_password_is_active(self, client):
        client.signup("pr1@test.local", ATHLETE_PW)
        code = self._request_code(client, "pr1@test.local")
        client.refresh_csrf()
        assert client.post("/api/auth/reset",
                           {"email": "pr1@test.local", "code": code,
                            "password": "BrandNew!2026"}).status_code == 200
        assert client.login("pr1@test.local", "BrandNew!2026")[0].status_code == 200
        assert client.login("pr1@test.local", ATHLETE_PW)[0].status_code == 401

    def test_reset_token_is_single_use(self, client):
        client.signup("pr2@test.local", ATHLETE_PW)
        code = self._request_code(client, "pr2@test.local")
        client.refresh_csrf()
        client.post("/api/auth/reset", {"email": "pr2@test.local",
                                        "code": code,
                                        "password": "BrandNew!2026"})
        client.refresh_csrf()
        assert client.post("/api/auth/reset",
                           {"email": "pr2@test.local", "code": code,
                            "password": "Another!2026"}).status_code == 400

    def test_expired_reset_token_fails(self, flask_app, client):
        client.signup("pr3@test.local", ATHLETE_PW)
        code = self._request_code(client, "pr3@test.local")
        with flask_app.app_context():
            db = appmod.get_db()
            db.execute("UPDATE email_tokens SET expires_at='2000-01-01T00:00:00'")
            db.commit()
        client.refresh_csrf()
        assert client.post("/api/auth/reset",
                           {"email": "pr3@test.local", "code": code,
                            "password": "BrandNew!2026"}).status_code == 400

    def test_reset_invalidates_existing_sessions(self, flask_app):
        """The attacker's stolen session must die when the victim resets."""
        victim = make_client(flask_app)
        victim.signup("pr4@test.local", ATHLETE_PW)
        attacker = make_client(flask_app)
        attacker.login("pr4@test.local", ATHLETE_PW)
        assert attacker.get("/api/me").status_code == 200

        resetter = make_client(flask_app)
        code = self._request_code(resetter, "pr4@test.local")
        resetter.refresh_csrf()
        assert resetter.post("/api/auth/reset",
                             {"email": "pr4@test.local", "code": code,
                              "password": "Recovered!2026"}).status_code == 200
        assert attacker.get("/api/me").status_code == 401
        assert attacker.get("/api/state").status_code == 401

    def test_forgot_does_not_reveal_whether_an_account_exists(self, client):
        client.signup("pr5@test.local", ATHLETE_PW)
        client.refresh_csrf()
        known = client.post("/api/auth/forgot", {"email": "pr5@test.local"})
        appmod.limiter._store.clear()
        client.refresh_csrf()
        unknown = client.post("/api/auth/forgot", {"email": "ghost@test.local"})
        assert known.status_code == unknown.status_code == 200
        assert (client.json(known)["message"] ==
                client.json(unknown)["message"])

    def test_reset_is_rate_limited(self, client):
        client.signup("pr6@test.local", ATHLETE_PW)
        limit = appmod.RATE_RULES["reset"][0]
        codes = []
        for _ in range(limit + 3):
            client.refresh_csrf()
            codes.append(client.post(
                "/api/auth/reset",
                {"email": "pr6@test.local", "code": "000000",
                 "password": "Guessing!2026"}).status_code)
        assert 429 in codes


class TestSessions:

    def test_logout_invalidates_the_session(self, athlete):
        assert athlete.get("/api/me").status_code == 200
        athlete.logout()
        assert athlete.get("/api/me").status_code == 401
        assert athlete.get("/api/state").status_code == 401

    def test_logout_all_kills_other_devices(self, flask_app):
        a = make_client(flask_app)
        a.signup("multi@test.local", ATHLETE_PW)
        a.login("multi@test.local", ATHLETE_PW)
        b = make_client(flask_app)
        b.login("multi@test.local", ATHLETE_PW)
        assert b.get("/api/me").status_code == 200
        a.post("/api/auth/logout-all")
        assert b.get("/api/me").status_code == 401

    def test_password_change_revokes_other_sessions(self, flask_app):
        a = make_client(flask_app)
        a.signup("pc@test.local", ATHLETE_PW)
        a.login("pc@test.local", ATHLETE_PW)
        b = make_client(flask_app)
        b.login("pc@test.local", ATHLETE_PW)
        r = a.post("/api/profile/password",
                   {"current": ATHLETE_PW, "password": "Changed!2026"})
        assert r.status_code == 200
        assert b.get("/api/me").status_code == 401
        # ...but the tab that made the change keeps working.
        a.csrf = r.get_json()["csrf"]
        assert a.get("/api/me").status_code == 200

    def test_session_cookie_flags(self, flask_app, client):
        client.signup("cookie@test.local", ATHLETE_PW)
        resp = client.login("cookie@test.local", ATHLETE_PW)[0]
        cookies = [h for k, h in resp.headers.items() if k == "Set-Cookie"]
        assert any("HttpOnly" in c and "SameSite=Lax" in c for c in cookies)
        assert flask_app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert flask_app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_forged_session_cookie_is_rejected(self, flask_app, client):
        client.c.set_cookie("athletix_session", "not-a-valid-signed-cookie")
        assert client.get("/api/me").status_code == 401

    def test_idle_session_expires(self, flask_app, athlete):
        import time as _t
        with athlete.c.session_transaction() as sess:
            sess["ts"] = int(_t.time()) - appmod.SESSION_IDLE_SECONDS - 10
        assert athlete.get("/api/me").status_code == 401


# ==========================================================================
# CSRF
# ==========================================================================
class TestCSRF:

    def test_state_change_without_a_token_fails(self, athlete):
        r = athlete.post("/api/messages", {"toId": 1, "text": "hi"}, csrf=None)
        assert r.status_code == 403

    def test_state_change_with_a_wrong_token_fails(self, athlete):
        r = athlete.post("/api/messages", {"toId": 1, "text": "hi"},
                         csrf="totally-wrong-token")
        assert r.status_code == 403

    @pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/signup",
                                      "/api/auth/logout", "/api/auth/forgot",
                                      "/api/auth/reset", "/api/auth/verify"])
    def test_auth_endpoints_are_not_csrf_exempt(self, client, path):
        """The old code skipped CSRF for every /api/auth/* path."""
        assert client.post(path, {}, csrf=None).status_code == 403

    def test_cross_origin_request_is_blocked(self, athlete):
        r = athlete.post("/api/notifications", {"text": "x"},
                         headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_get_requests_do_not_need_a_token(self, athlete):
        assert athlete.get("/api/state").status_code == 200

    def test_csrf_token_rotates_on_login(self, client):
        client.signup("rot@test.local", ATHLETE_PW)
        before = client.refresh_csrf()
        _, data = client.login("rot@test.local", ATHLETE_PW)
        assert data["csrf"] != before


# ==========================================================================
# IDOR / BOLA
# ==========================================================================
class TestIDOR:

    def _make_report(self, c, **kw):
        payload = {"m": {"speed": 70, "agility": 71, "strength": 72,
                         "stamina": 73, "technique": 74}}
        payload.update(kw)
        return c.json(c.post("/api/reports", payload))["report"]

    def test_athlete_cannot_read_another_athletes_report(self, athlete,
                                                         athlete_b):
        rep = self._make_report(athlete_b)
        r = athlete.get("/api/reports/%d" % rep["id"])
        assert r.status_code == 404

    def test_athlete_can_read_their_own_report(self, athlete):
        rep = self._make_report(athlete)
        r = athlete.get("/api/reports/%d" % rep["id"])
        assert r.status_code == 200
        assert r.get_json()["report"]["m"]["speed"] == 70

    def test_report_id_enumeration_finds_nothing(self, athlete, athlete_b):
        self._make_report(athlete_b)
        self._make_report(athlete_b)
        found = [rid for rid in range(1, 30)
                 if athlete.get("/api/reports/%d" % rid).status_code == 200]
        mine = {r["id"] for r in
                athlete.json(athlete.get("/api/my/reports"))["reports"]}
        assert set(found) <= mine

    def test_state_hides_other_athletes_metric_detail(self, athlete,
                                                      athlete_b):
        self._make_report(athlete_b)
        self._make_report(athlete)
        me = athlete.user["id"]
        for rep in athlete.state()["reports"]:
            if rep["athleteId"] == me:
                assert rep["m"] is not None
            else:
                assert rep["m"] is None, "leaked another athlete's metrics"
                assert rep["ai"] is None

    def test_report_is_always_filed_against_the_session_user(self, athlete,
                                                             athlete_b):
        rep = self._make_report(athlete, athleteId=athlete_b.user["id"],
                                athlete_id=athlete_b.user["id"])
        assert rep["athleteId"] == athlete.user["id"]

    def test_coach_cannot_create_reports(self, coach):
        r = coach.post("/api/reports", {"m": {k: 50 for k in appmod.METRICS}})
        assert r.status_code == 403

    def test_athlete_cannot_read_another_athletes_messages(self, flask_app,
                                                           athlete, athlete_b,
                                                           coach):
        coach.post("/api/messages", {"toId": athlete_b.user["id"],
                                     "text": "private scouting note"})
        blob = athlete.get("/api/state").get_data(as_text=True)
        assert "private scouting note" not in blob
        convo = athlete.json(athlete.get("/api/messages/%d"
                                         % coach.user["id"]))
        assert convo["messages"] == []

    def test_conversation_endpoint_only_returns_own_threads(self, athlete,
                                                            athlete_b, coach):
        coach.post("/api/messages", {"toId": athlete_b.user["id"],
                                     "text": "for B only"})
        got = athlete.json(athlete.get("/api/messages/%d" % athlete_b.user["id"]))
        assert got["messages"] == []

    def test_user_cannot_see_another_users_notifications(self, athlete,
                                                         athlete_b, coach):
        coach.post("/api/messages", {"toId": athlete_b.user["id"],
                                     "text": "ping"})
        notifs = athlete.json(
            athlete.get("/api/my/notifications"))["notifications"]
        assert all(n["toId"] == athlete.user["id"] for n in notifs)
        state_notifs = athlete.state()["notifs"]
        assert all(n["toId"] == athlete.user["id"] for n in state_notifs)

    def test_marking_notifications_read_touches_only_own_rows(self, flask_app,
                                                              athlete,
                                                              athlete_b,
                                                              coach):
        coach.post("/api/messages", {"toId": athlete_b.user["id"],
                                     "text": "ping"})
        athlete.post("/api/notifications/read")
        with flask_app.app_context():
            unread = appmod.get_db().execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0",
                (athlete_b.user["id"],)).fetchone()[0]
        assert unread == 1

    def test_profile_update_cannot_target_another_user(self, flask_app,
                                                       athlete, athlete_b):
        athlete.post("/api/profile/update",
                     {"id": athlete_b.user["id"],
                      "user_id": athlete_b.user["id"],
                      "name": "HACKED", "role": "admin"})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT name, role FROM users WHERE id=?",
                (athlete_b.user["id"],)).fetchone()
        assert row["name"] != "HACKED"
        assert row["role"] == "athlete"

    def test_export_only_ever_returns_the_callers_data(self, athlete,
                                                       athlete_b, coach):
        self._make_report(athlete_b)
        coach.post("/api/messages", {"toId": athlete_b.user["id"],
                                     "text": "b only note"})
        data = json.loads(athlete.get("/api/export").get_data(as_text=True))
        assert data["account"]["id"] == athlete.user["id"]
        assert data["reports"] == []
        assert "b only note" not in json.dumps(data)

    def test_coach_athlete_detail_rejects_non_athlete_ids(self, coach, owner):
        r = coach.get("/api/coach/athletes/%d" % owner.user["id"])
        assert r.status_code == 404


# ==========================================================================
# Privacy / data minimisation
# ==========================================================================
class TestPrivacy:

    def test_state_does_not_leak_other_users_contact_details(self, athlete,
                                                             athlete_b, coach):
        state = athlete.state()
        for u in state["users"]:
            if u["id"] == athlete.user["id"]:
                assert "email" in u          # own record keeps its own data
            else:
                assert "email" not in u, "leaked %s" % u
                assert "phone" not in u
                assert "last_login" not in u
                assert "verified" not in u

    def test_coach_sees_athlete_performance_but_not_contact_details(
            self, coach, athlete):
        athletes = coach.json(coach.get("/api/coach/athletes"))["athletes"]
        target = [a for a in athletes if a["id"] == athlete.user["id"]][0]
        assert target["sport"] == "Cricket"       # scouting data: yes
        assert "email" not in target              # contact data: no
        assert "phone" not in target

    def test_operator_can_see_contact_details(self, owner, athlete):
        users = owner.json(owner.get("/api/admin/users"))["users"]
        target = [u for u in users if u["id"] == athlete.user["id"]][0]
        assert target["email"] == "athlete.a@test.local"

    def test_ratings_do_not_reveal_who_rated_whom(self, athlete, athlete_b,
                                                  coach):
        athlete_b.post("/api/ratings",
                       {"coachId": coach.user["id"], "stars": 2})
        athlete.post("/api/ratings",
                     {"coachId": coach.user["id"], "stars": 5})
        ratings = athlete.state()["ratings"]
        assert len(ratings) == 2
        mine = [r for r in ratings if r["byId"] == athlete.user["id"]]
        assert len(mine) == 1 and mine[0]["stars"] == 5
        others = [r for r in ratings if r["stars"] == 2]
        assert others[0]["byId"] is None

    def test_no_password_material_anywhere_in_the_state_payload(self, athlete,
                                                               coach,
                                                               athlete_b):
        blob = athlete.get("/api/state").get_data(as_text=True)
        for needle in ("pass_hash", "$2b$", "password", ATHLETE_PW, COACH_PW):
            assert needle not in blob

    def test_api_responses_are_not_cacheable(self, athlete):
        resp = athlete.get("/api/state")
        assert "no-store" in resp.headers.get("Cache-Control", "")


# ==========================================================================
# Messaging authorization
# ==========================================================================
class TestMessaging:

    def test_athlete_cannot_message_another_athlete(self, athlete, athlete_b):
        r = athlete.post("/api/messages",
                         {"toId": athlete_b.user["id"], "text": "hello"})
        assert r.status_code == 404

    def test_athlete_cannot_message_the_owner(self, athlete, owner):
        r = athlete.post("/api/messages",
                         {"toId": owner.user["id"], "text": "hello"})
        assert r.status_code == 404

    def test_athlete_can_message_a_coach(self, athlete, coach):
        r = athlete.post("/api/messages",
                         {"toId": coach.user["id"], "text": "hello coach"})
        assert r.status_code == 200

    def test_coach_can_message_an_athlete(self, coach, athlete):
        r = coach.post("/api/messages",
                       {"toId": athlete.user["id"], "text": "trials sunday"})
        assert r.status_code == 200

    def test_coach_cannot_message_another_coach_unprompted(self, flask_app,
                                                           coach):
        other = make_client(flask_app)
        other.signup("coach.b@test.local", COACH_PW, role="coach",
                     name="Coach B")
        other.login("coach.b@test.local", COACH_PW)
        r = coach.post("/api/messages",
                       {"toId": other.user["id"], "text": "hi"})
        assert r.status_code == 404

    def test_a_reply_to_an_existing_thread_is_allowed(self, owner, athlete):
        assert owner.post("/api/messages",
                          {"toId": athlete.user["id"],
                           "text": "operator note"}).status_code == 200
        assert athlete.post("/api/messages",
                            {"toId": owner.user["id"],
                             "text": "reply"}).status_code == 200

    def test_sender_identity_cannot_be_forged(self, flask_app, athlete, coach):
        athlete.post("/api/messages",
                     {"toId": coach.user["id"], "text": "spoof attempt",
                      "fromId": coach.user["id"], "from": "Platform Owner"})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT from_id, from_nm FROM messages "
                "WHERE body='spoof attempt'").fetchone()
        assert row["from_id"] == athlete.user["id"]
        assert row["from_nm"] == "Athlete A"

    def test_message_to_a_nonexistent_user_fails(self, athlete):
        assert athlete.post("/api/messages",
                            {"toId": 999999, "text": "x"}).status_code == 404

    def test_messaging_is_rate_limited(self, athlete, coach):
        limit = appmod.RATE_RULES["message"][0]
        codes = [athlete.post("/api/messages",
                              {"toId": coach.user["id"], "text": "spam"}
                              ).status_code for _ in range(limit + 3)]
        assert 429 in codes


# ==========================================================================
# Notification authorization
# ==========================================================================
class TestNotifications:

    def test_arbitrary_recipient_is_blocked(self, athlete, athlete_b):
        r = athlete.post("/api/notifications",
                         {"toId": athlete_b.user["id"],
                          "text": "Your account is suspended, click here"})
        assert r.status_code == 403

    def test_coach_cannot_push_notifications_to_athletes(self, coach, athlete):
        r = coach.post("/api/notifications",
                       {"toId": athlete.user["id"], "text": "phish"})
        assert r.status_code == 403

    def test_self_notification_is_allowed(self, athlete):
        assert athlete.post("/api/notifications",
                            {"text": "my reminder"}).status_code == 200

    def test_operator_may_notify_anyone(self, owner, athlete):
        assert owner.post("/api/notifications",
                          {"toId": athlete.user["id"],
                           "text": "maintenance window"}).status_code == 200

    def test_notification_text_cannot_carry_markup(self, flask_app, athlete):
        athlete.post("/api/notifications",
                     {"text": "<img src=x onerror=alert(1)>"})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT text FROM notifications ORDER BY id DESC "
                "LIMIT 1").fetchone()
        assert "<" not in row["text"] and ">" not in row["text"]


# ==========================================================================
# Ratings authorization
# ==========================================================================
class TestRatings:

    def test_coach_cannot_rate(self, coach, flask_app):
        other = make_client(flask_app)
        other.signup("coach.c@test.local", COACH_PW, role="coach",
                     name="Coach C")
        other.login("coach.c@test.local", COACH_PW)
        r = coach.post("/api/ratings",
                       {"coachId": other.user["id"], "stars": 1})
        assert r.status_code == 403

    def test_owner_cannot_rate(self, owner, coach):
        assert owner.post("/api/ratings",
                          {"coachId": coach.user["id"],
                           "stars": 5}).status_code == 403

    def test_athlete_can_rate_a_coach(self, athlete, coach):
        assert athlete.post("/api/ratings",
                            {"coachId": coach.user["id"],
                             "stars": 4}).status_code == 200

    def test_cannot_rate_a_non_coach(self, athlete, athlete_b):
        assert athlete.post("/api/ratings",
                            {"coachId": athlete_b.user["id"],
                             "stars": 5}).status_code == 404

    def test_rating_cannot_be_attributed_to_someone_else(self, flask_app,
                                                         athlete, athlete_b,
                                                         coach):
        athlete.post("/api/ratings",
                     {"coachId": coach.user["id"], "stars": 1,
                      "byId": athlete_b.user["id"],
                      "athleteId": athlete_b.user["id"]})
        with flask_app.app_context():
            rows = appmod.get_db().execute("SELECT * FROM ratings").fetchall()
        assert len(rows) == 1
        assert rows[0]["athlete_id"] == athlete.user["id"]

    def test_rating_cannot_overwrite_another_users_rating(self, flask_app,
                                                          athlete, athlete_b,
                                                          coach):
        athlete_b.post("/api/ratings",
                       {"coachId": coach.user["id"], "stars": 5})
        athlete.post("/api/ratings",
                     {"coachId": coach.user["id"], "stars": 1})
        with flask_app.app_context():
            rows = {r["athlete_id"]: r["stars"] for r in
                    appmod.get_db().execute("SELECT * FROM ratings")}
        assert rows[athlete_b.user["id"]] == 5
        assert rows[athlete.user["id"]] == 1

    @pytest.mark.parametrize("stars", [0, 6, -1, 99, "5; DROP TABLE users",
                                       None, 2.5])
    def test_invalid_star_values_rejected(self, athlete, coach, stars):
        r = athlete.post("/api/ratings",
                         {"coachId": coach.user["id"], "stars": stars})
        assert r.status_code == 400


# ==========================================================================
# Input validation
# ==========================================================================
class TestInputValidation:

    def test_malformed_json_does_not_500(self, athlete):
        r = athlete.c.post("/api/messages", data="{not json",
                           content_type="application/json",
                           headers={"X-CSRF-Token": athlete.csrf})
        assert r.status_code in (400, 404), r.status_code

    def test_oversized_body_is_rejected(self, athlete):
        r = athlete.post("/api/messages",
                         {"toId": 1, "text": "A" * (appmod.MAX_JSON_BYTES + 10)})
        assert r.status_code == 413

    def test_oversized_fields_are_truncated_not_stored_whole(self, flask_app,
                                                             athlete, coach):
        athlete.post("/api/messages",
                     {"toId": coach.user["id"], "text": "B" * 5000})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        assert len(row["body"]) <= 2000

    @pytest.mark.parametrize("bad", ["abc", "1 OR 1=1", None, -1, 0,
                                     "../../etc/passwd", [1], {"a": 1},
                                     "1; DROP TABLE users"])
    def test_invalid_ids_are_rejected(self, athlete, bad):
        r = athlete.post("/api/messages", {"toId": bad, "text": "hi"})
        assert r.status_code in (400, 404), (bad, r.status_code)

    def test_sql_injection_in_login_does_not_authenticate(self, client):
        client.refresh_csrf()
        for payload in ("' OR '1'='1", "admin'--", "x@y.z' OR 1=1--"):
            r = client.post("/api/auth/login",
                            {"email": payload, "password": payload})
            assert r.status_code in (400, 401)
        assert client.get("/api/me").status_code == 401

    def test_sql_injection_does_not_drop_tables(self, flask_app, athlete,
                                                coach):
        athlete.post("/api/messages",
                     {"toId": coach.user["id"],
                      "text": "'); DROP TABLE users;--"})
        with flask_app.app_context():
            assert appmod.get_db().execute(
                "SELECT COUNT(*) FROM users").fetchone()[0] > 0

    def test_stored_markup_is_neutralised(self, flask_app, athlete, coach):
        athlete.post("/api/messages",
                     {"toId": coach.user["id"],
                      "text": "<script>alert(document.cookie)</script>"})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        assert "<script" not in row["body"]

    def test_control_characters_are_stripped(self, flask_app, athlete, coach):
        athlete.post("/api/messages",
                     {"toId": coach.user["id"], "text": "a\x00b\x07c"})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        assert "\x00" not in row["body"]

    @pytest.mark.parametrize("sport", ["Quidditch", "<script>", "", 1,
                                       "Cricket'; --"])
    def test_unknown_sport_rejected_on_profile_update(self, athlete, sport):
        assert athlete.post("/api/profile/update",
                            {"sport": sport}).status_code == 400

    def test_known_sport_accepted_on_profile_update(self, athlete):
        assert athlete.post("/api/profile/update",
                            {"sport": "Boxing"}).status_code == 200

    @pytest.mark.parametrize("age", ["abc", -5, 500, [1], 3.7e9, 2.5])
    def test_invalid_age_does_not_crash(self, athlete, age):
        r = athlete.post("/api/profile/update", {"age": age})
        assert r.status_code == 400

    def test_null_age_means_unchanged_not_invalid(self, athlete):
        assert athlete.post("/api/profile/update",
                            {"age": None}).status_code == 200

    @pytest.mark.parametrize("metrics", [
        {"speed": "abc"},
        {"speed": 101, "agility": 1, "strength": 1, "stamina": 1,
         "technique": 1},
        {"speed": -1, "agility": 1, "strength": 1, "stamina": 1,
         "technique": 1},
        "not a dict", None, [],
    ])
    def test_invalid_report_metrics_rejected(self, athlete, metrics):
        assert athlete.post("/api/reports", {"m": metrics}).status_code == 400

    def test_profile_update_response_reflects_the_write(self, athlete):
        """current_user() is memoized per request; a handler that writes to
        the user row must re-read it before serializing, or the API echoes
        stale values back to the client."""
        r = athlete.post("/api/profile/update", {"name": "Renamed Athlete"})
        assert r.status_code == 200
        assert r.get_json()["user"]["name"] == "Renamed Athlete"
        assert athlete.json(athlete.get("/api/me"))["user"]["name"] ==             "Renamed Athlete"

    def test_coach_profile_response_reflects_the_write(self, coach):
        r = coach.post("/api/coach/profile", {"specialty": "Sprint Mechanics",
                                              "city": "Pune"})
        assert r.status_code == 200
        user = r.get_json()["user"]
        assert user["specialty"] == "Sprint Mechanics"
        assert user["city"] == "Pune"

    def test_invalid_consent_key_rejected(self, athlete):
        assert athlete.post("/api/consent",
                            {"key": "arbitrary", "value": 1}).status_code == 400
        assert athlete.post("/api/consent",
                            {"key": "analytics", "value": 1}).status_code == 200


# ==========================================================================
# Upload / media validation
# ==========================================================================
class TestUploads:

    PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    def test_valid_png_accepted(self, athlete):
        assert athlete.post("/api/profile/photo",
                            {"photo": self.PNG}).status_code == 200

    @pytest.mark.parametrize("photo", [
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "data:image/svg+xml;base64,PHN2Zz48c2NyaXB0PmFsZXJ0KDEpPC9zY3JpcHQ+PC9zdmc+",
        "javascript:alert(1)",
        "http://evil.example/x.png",
        "data:image/png;base64,!!!!not-base64!!!!",
        "data:image/png;base64,SGVsbG8gd29ybGQh",     # valid b64, not an image
        "<script>alert(1)</script>",
    ])
    def test_bad_photos_rejected(self, athlete, photo):
        assert athlete.post("/api/profile/photo",
                            {"photo": photo}).status_code == 400

    def test_oversized_photo_rejected(self, athlete):
        big = "data:image/png;base64," + ("A" * 600000)
        assert athlete.post("/api/profile/photo",
                            {"photo": big}).status_code in (400, 413)

    @pytest.mark.parametrize("name,expected", [
        ("../../../../etc/passwd", "video"),
        ("..\\..\\windows\\system32\\cmd.exe", "video"),
        ("clip.mp4", "clip.mp4"),
        ("evil.php", "video"),
        ("run.exe", "video"),
        ("shell.mp4.sh", "video"),
        ("a" * 400 + ".mp4", None),
    ])
    def test_video_filenames_are_sanitised(self, flask_app, athlete, name,
                                           expected):
        athlete.post("/api/reports",
                     {"m": {k: 50 for k in appmod.METRICS},
                      "video": {"name": name, "size": 5, "source": "upload"}})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT filename FROM videos ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert "/" not in row["filename"] and "\\" not in row["filename"]
        assert ".." not in row["filename"]
        if expected:
            assert row["filename"] == expected
        assert len(row["filename"]) <= 120

    def test_video_source_is_restricted(self, flask_app, athlete):
        athlete.post("/api/reports",
                     {"m": {k: 50 for k in appmod.METRICS},
                      "video": {"name": "a.mp4", "size": 99999999,
                                "source": "<script>"}})
        with flask_app.app_context():
            row = appmod.get_db().execute(
                "SELECT source, size_mb FROM videos ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["source"] in appmod.VIDEO_SOURCES
        assert row["size_mb"] <= 4096


# ==========================================================================
# Headers, error handling, secrets
# ==========================================================================
class TestHardening:

    def test_security_headers_present(self, client):
        h = client.get("/").headers
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "DENY"
        assert "strict-origin" in h["Referrer-Policy"]
        assert "Permissions-Policy" in h
        csp = h["Content-Security-Policy"]
        for directive in ("default-src 'self'", "object-src 'none'",
                          "frame-ancestors 'none'", "base-uri 'self'",
                          "form-action 'self'"):
            assert directive in csp

    def test_csp_allows_the_assets_the_app_actually_uses(self, client):
        csp = client.get("/").headers["Content-Security-Policy"]
        for host in ("https://cdnjs.cloudflare.com", "https://cdn.jsdelivr.net",
                     "https://fonts.googleapis.com", "https://fonts.gstatic.com",
                     "https://api.qrserver.com", "https://storage.googleapis.com"):
            assert host in csp, host

    def test_errors_do_not_leak_internals(self, athlete):
        for resp in (athlete.get("/api/reports/999999"),
                     athlete.get("/api/nope"),
                     athlete.post("/api/messages", {})):
            text = resp.get_data(as_text=True)
            for needle in ("Traceback", "sqlite3", "SELECT ", "app.py",
                           "C:\\", "/home/", "Werkzeug"):
                assert needle not in text, (needle, resp.status_code)

    def test_404_on_api_is_json_not_html(self, client):
        r = client.get("/api/definitely-not-a-route")
        assert r.status_code == 404
        assert r.get_json()["ok"] is False

    def test_debug_is_off_by_default(self):
        assert appmod.DEBUG_MODE is False
        assert appmod.app.debug is False

    def test_dev_codes_are_disabled_in_production_config(self):
        """DEV_CODES must be impossible to reach with APP_ENV=production."""
        assert appmod.DEV_CODES == (appmod.EMAIL_DEV_MODE
                                    and not appmod.IS_PRODUCTION)

    def test_audit_log_records_security_events(self, flask_app, client):
        client.signup("audit@test.local", ATHLETE_PW)
        client.login("audit@test.local", "WrongPass!2026")
        client.login("audit@test.local", ATHLETE_PW)
        with flask_app.app_context():
            actions = [r["action"] for r in appmod.get_db().execute(
                "SELECT action FROM activity_logs")]
        for expected in ("signup", "verify_email", "login_failed", "login"):
            assert expected in actions, expected

    def test_audit_log_never_stores_credentials(self, flask_app, client):
        client.signup("audit2@test.local", ATHLETE_PW)
        client.login("audit2@test.local", ATHLETE_PW)
        with flask_app.app_context():
            rows = appmod.get_db().execute(
                "SELECT detail FROM activity_logs").fetchall()
        blob = " ".join((r["detail"] or "") for r in rows)
        assert ATHLETE_PW not in blob
        assert "$2b$" not in blob

    def test_owner_password_policy_is_enforced_at_bootstrap(self):
        assert appmod.password_problem("1234") is not None
        assert appmod.password_problem("OwnerPass!2026") is None

    def test_bcrypt_cost_cannot_be_weakened_in_production(self, monkeypatch):
        """BCRYPT_ROUNDS exists so the suite is not 18 minutes long. It must
        be impossible to use it to weaken a real deployment."""
        for signal in ({"FLASK_DEBUG": "0"}, {"APP_ENV": "production"},
                       {"APP_ENV": "prod"}):
            for attempt in ("1", "4", "0", "-5"):
                monkeypatch.delenv("FLASK_DEBUG", raising=False)
                monkeypatch.delenv("APP_ENV", raising=False)
                for k, v in signal.items():
                    monkeypatch.setenv(k, v)
                monkeypatch.setenv("BCRYPT_ROUNDS", attempt)
                assert appmod._bcrypt_rounds() >= 12, (signal, attempt)

    def test_bcrypt_cost_defaults_to_12_without_the_override(self, monkeypatch):
        monkeypatch.delenv("BCRYPT_ROUNDS", raising=False)
        assert appmod._bcrypt_rounds() == 12

    def test_bcrypt_cost_override_is_bounded(self, monkeypatch):
        monkeypatch.delenv("FLASK_DEBUG", raising=False)
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("BCRYPT_ROUNDS", "99")
        assert appmod._bcrypt_rounds() <= 16
        monkeypatch.setenv("BCRYPT_ROUNDS", "not-a-number")
        assert appmod._bcrypt_rounds() == 12

    def test_passwords_are_bcrypt_hashed_not_stored(self, flask_app, client):
        client.signup("hash@test.local", ATHLETE_PW)
        with flask_app.app_context():
            h = appmod.get_db().execute(
                "SELECT pass_hash FROM users WHERE email=?",
                ("hash@test.local",)).fetchone()["pass_hash"]
        assert h.startswith("$2"), "not a bcrypt hash"
        assert ATHLETE_PW not in h
        assert appmod.check_password(ATHLETE_PW, h)
        assert not appmod.check_password("wrong-password", h)

    @pytest.mark.parametrize("method", ["put", "patch", "delete"])
    def test_no_unintended_write_methods_on_resources(self, athlete, method):
        """Reports and messages expose no PUT/PATCH/DELETE surface at all."""
        for path in ("/api/reports/1", "/api/messages/1", "/api/me",
                     "/api/state"):
            r = getattr(athlete.c, method)(
                path, headers={"X-CSRF-Token": athlete.csrf})
            assert r.status_code in (403, 404, 405), (method, path,
                                                      r.status_code)

    def test_public_stats_leak_no_personal_data(self, client):
        data = client.get("/api/public/stats").get_json()
        assert set(data) <= {"ok", "athletes", "coaches", "reports",
                             "live_sessions", "sports"}
