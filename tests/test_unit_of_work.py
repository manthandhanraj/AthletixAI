"""Phase 2.3A — transaction atomicity.

Two kinds of test here:

1. Unit tests of the `unit_of_work()` boundary itself (commit, rollback,
   nesting, re-entrancy).

2. FAILURE INJECTION against the three security-critical operations that
   Phase 2.1 flagged (finding B1). Each one deliberately breaks the second
   half of the operation and asserts that the FIRST half did not persist.

The happy path was already green before this phase — it is the failure path
that was broken, so that is what these tests are for.
"""
import sqlite3

import pytest

from conftest import ATHLETE_PW, COACH_PW, appmod, make_client, user_id

import athletix.api.admin as admin_mod
import athletix.services.auth as auth_mod
import athletix.services.auth as profile_mod
from athletix.unit_of_work import in_unit_of_work, unit_of_work


class Boom(Exception):
    """Injected failure."""


def _hash_of(flask_app, email):
    with flask_app.app_context():
        row = appmod.get_db().execute(
            "SELECT pass_hash, sess_epoch, role FROM users WHERE email=?",
            (email,)).fetchone()
        return (row["pass_hash"], row["sess_epoch"], row["role"])


# ==========================================================================
# The boundary itself
# ==========================================================================
class TestUnitOfWorkBoundary:

    def test_commits_on_success(self, flask_app):
        with flask_app.app_context():
            with unit_of_work() as db:
                db.execute("INSERT INTO activity_logs (action, at) "
                           "VALUES ('uow_commit_test', '2026-01-01')")
            n = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs "
                "WHERE action='uow_commit_test'").fetchone()[0]
        assert n == 1

    def test_rolls_back_on_exception(self, flask_app):
        with flask_app.app_context():
            with pytest.raises(Boom):
                with unit_of_work() as db:
                    db.execute("INSERT INTO activity_logs (action, at) "
                               "VALUES ('uow_rollback_test', '2026-01-01')")
                    raise Boom()
            n = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs "
                "WHERE action='uow_rollback_test'").fetchone()[0]
        assert n == 0, "rollback did not discard the write"

    def test_nested_block_does_not_commit_early(self, flask_app):
        """The whole point: an inner boundary must not end the outer one."""
        with flask_app.app_context():
            with pytest.raises(Boom):
                with unit_of_work() as outer:
                    outer.execute("INSERT INTO activity_logs (action, at) "
                                  "VALUES ('uow_outer', '2026-01-01')")
                    with unit_of_work() as inner:
                        inner.execute("INSERT INTO activity_logs (action, at) "
                                      "VALUES ('uow_inner', '2026-01-01')")
                    # inner exited cleanly; if it had committed, the outer
                    # rollback below could not undo either row.
                    raise Boom()
            rows = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs "
                "WHERE action IN ('uow_outer','uow_inner')").fetchone()[0]
        assert rows == 0, "a nested block committed early"

    def test_depth_tracking_is_balanced(self, flask_app):
        with flask_app.app_context():
            assert in_unit_of_work() is False
            with unit_of_work():
                assert in_unit_of_work() is True
                with unit_of_work():
                    assert in_unit_of_work() is True
                assert in_unit_of_work() is True
            assert in_unit_of_work() is False

    def test_depth_is_restored_after_a_failure(self, flask_app):
        with flask_app.app_context():
            with pytest.raises(Boom):
                with unit_of_work():
                    raise Boom()
            assert in_unit_of_work() is False, "depth leaked after rollback"
            # the connection is still usable afterwards
            with unit_of_work() as db:
                db.execute("INSERT INTO activity_logs (action, at) "
                           "VALUES ('uow_after_fail', '2026-01-01')")
            n = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs "
                "WHERE action='uow_after_fail'").fetchone()[0]
            assert n == 1

    def test_constraint_violation_rolls_the_whole_block_back(self, flask_app):
        with flask_app.app_context():
            with pytest.raises(sqlite3.IntegrityError):
                with unit_of_work() as db:
                    db.execute("INSERT INTO activity_logs (action, at) "
                               "VALUES ('uow_before_constraint', '2026-01-01')")
                    # role has a CHECK constraint; this must fail.
                    db.execute("INSERT INTO users (role,name,email,pass_hash,"
                               "created_at) VALUES ('wizard','X','x@x.io','h','2026')")
            n = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs "
                "WHERE action='uow_before_constraint'").fetchone()[0]
        assert n == 0

    def test_audit_log_joins_an_open_transaction(self, flask_app):
        """log_activity used to commit independently, which would end a
        caller's transaction halfway (Phase 2.1 finding B2)."""
        with flask_app.app_context():
            with pytest.raises(Boom):
                with unit_of_work() as db:
                    db.execute("INSERT INTO activity_logs (action, at) "
                               "VALUES ('uow_marker', '2026-01-01')")
                    appmod.log_activity(None, "uow_nested_audit", "x")
                    raise Boom()
            n = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs WHERE action IN "
                "('uow_marker','uow_nested_audit')").fetchone()[0]
        assert n == 0, "log_activity committed inside somebody else's transaction"

    def test_audit_log_still_commits_when_standalone(self, flask_app):
        with flask_app.app_context():
            appmod.log_activity(None, "uow_standalone_audit", "x")
            n = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs "
                "WHERE action='uow_standalone_audit'").fetchone()[0]
        assert n == 1


# ==========================================================================
# Failure injection: password reset
# ==========================================================================
class TestResetPasswordAtomicity:

    def _request_code(self, client, email):
        client.refresh_csrf()
        return client.json(client.post("/api/auth/forgot",
                                       {"email": email}))["dev_code"]

    def test_happy_path_changes_password_and_revokes_sessions(self, flask_app):
        victim = make_client(flask_app)
        victim.signup("atomic1@test.local", ATHLETE_PW)
        live = make_client(flask_app)
        live.login("atomic1@test.local", ATHLETE_PW)
        assert live.get("/api/me").status_code == 200

        before = _hash_of(flask_app, "atomic1@test.local")
        r = make_client(flask_app)
        code = self._request_code(r, "atomic1@test.local")
        r.refresh_csrf()
        assert r.post("/api/auth/reset",
                      {"email": "atomic1@test.local", "code": code,
                       "password": "Recovered!2026"}).status_code == 200
        after = _hash_of(flask_app, "atomic1@test.local")
        assert after[0] != before[0], "password not changed"
        assert after[1] == before[1] + 1, "epoch not bumped"
        assert live.get("/api/me").status_code == 401, "old session survived"

    def test_failure_in_epoch_bump_rolls_back_the_password(self, flask_app, monkeypatch):
        """THE Phase 2.1 B1 scenario: the credential write succeeds, the
        revocation fails. Neither may persist."""
        victim = make_client(flask_app)
        victim.signup("atomic2@test.local", ATHLETE_PW)
        live = make_client(flask_app)
        live.login("atomic2@test.local", ATHLETE_PW)
        before = _hash_of(flask_app, "atomic2@test.local")

        r = make_client(flask_app)
        code = self._request_code(r, "atomic2@test.local")

        def exploding_bump(_uid):
            raise Boom("epoch update failed")
        monkeypatch.setattr(auth_mod, "bump_epoch", exploding_bump)

        r.refresh_csrf()
        resp = r.post("/api/auth/reset",
                      {"email": "atomic2@test.local", "code": code,
                       "password": "ShouldNotStick!2026"})
        assert resp.status_code == 500          # opaque error, no leak
        assert "Boom" not in resp.get_data(as_text=True)

        after = _hash_of(flask_app, "atomic2@test.local")
        assert after[0] == before[0], "PASSWORD PERSISTED despite failed revocation"
        assert after[1] == before[1], "epoch changed despite failure"

        monkeypatch.undo()
        # The old credential still works and the old session is still the
        # one the user had - the system is in its previous consistent state.
        fresh = make_client(flask_app)
        assert fresh.login("atomic2@test.local", ATHLETE_PW)[0].status_code == 200
        assert fresh.login("atomic2@test.local", "ShouldNotStick!2026")[0].status_code == 401

    def test_failure_after_both_writes_still_rolls_back(self, flask_app, monkeypatch):
        """An exception raised anywhere inside the boundary discards it all."""
        c = make_client(flask_app)
        c.signup("atomic3@test.local", ATHLETE_PW)
        before = _hash_of(flask_app, "atomic3@test.local")
        r = make_client(flask_app)
        code = self._request_code(r, "atomic3@test.local")

        real_bump = auth_mod.bump_epoch

        def bump_then_explode(uid):
            real_bump(uid)                 # both writes now staged
            raise Boom("failure after both mutations")
        monkeypatch.setattr(auth_mod, "bump_epoch", bump_then_explode)

        r.refresh_csrf()
        assert r.post("/api/auth/reset",
                      {"email": "atomic3@test.local", "code": code,
                       "password": "AlsoShouldNotStick!2026"}).status_code == 500
        after = _hash_of(flask_app, "atomic3@test.local")
        assert after[0] == before[0] and after[1] == before[1]


# ==========================================================================
# Failure injection: change password
# ==========================================================================
class TestChangePasswordAtomicity:

    def test_happy_path(self, flask_app, athlete):
        before = _hash_of(flask_app, "athlete.a@test.local")
        r = athlete.post("/api/profile/password",
                         {"current": ATHLETE_PW, "password": "Changed!2026"})
        assert r.status_code == 200
        after = _hash_of(flask_app, "athlete.a@test.local")
        assert after[0] != before[0] and after[1] == before[1] + 1

    def test_failure_in_epoch_bump_rolls_back_the_password(self, flask_app, athlete,
                                                           monkeypatch):
        before = _hash_of(flask_app, "athlete.a@test.local")

        def exploding_bump(_uid):
            raise Boom("epoch update failed")
        monkeypatch.setattr(profile_mod, "bump_epoch", exploding_bump)

        r = athlete.post("/api/profile/password",
                         {"current": ATHLETE_PW, "password": "NoStick!2026"})
        assert r.status_code == 500
        after = _hash_of(flask_app, "athlete.a@test.local")
        assert after[0] == before[0], "PASSWORD PERSISTED despite failed revocation"
        assert after[1] == before[1]

        monkeypatch.undo()
        fresh = make_client(flask_app)
        assert fresh.login("athlete.a@test.local", ATHLETE_PW)[0].status_code == 200


# ==========================================================================
# Failure injection: role change
# ==========================================================================
class TestAdminSetRoleAtomicity:

    def test_happy_path_changes_role_and_revokes(self, flask_app, owner, athlete):
        target = user_id(flask_app, "athlete.a@test.local")
        before = _hash_of(flask_app, "athlete.a@test.local")
        assert athlete.get("/api/me").status_code == 200
        assert owner.post("/api/admin/users/%d/role" % target,
                          {"role": "coach"}).status_code == 200
        after = _hash_of(flask_app, "athlete.a@test.local")
        assert after[2] == "coach" and after[1] == before[1] + 1
        assert athlete.get("/api/me").status_code == 401

    def test_failure_in_epoch_bump_rolls_back_the_role(self, flask_app, owner,
                                                       athlete, monkeypatch):
        """A half-applied role change is a privilege bug: new permissions in
        the database, old permissions still live in issued cookies."""
        target = user_id(flask_app, "athlete.a@test.local")
        before = _hash_of(flask_app, "athlete.a@test.local")

        def exploding_bump(_uid):
            raise Boom("epoch update failed")
        monkeypatch.setattr(admin_mod, "bump_epoch", exploding_bump)

        r = owner.post("/api/admin/users/%d/role" % target, {"role": "admin"})
        assert r.status_code == 500
        after = _hash_of(flask_app, "athlete.a@test.local")
        assert after[2] == before[2], "ROLE PERSISTED despite failed revocation"
        assert after[1] == before[1]
        # the target keeps their original, un-elevated session
        assert athlete.get("/api/me").status_code == 200
        assert athlete.get("/api/admin/users").status_code == 403


# ==========================================================================
# Session-revocation policy must be unchanged by this refactor
# ==========================================================================
class TestSessionRevocationPolicyUnchanged:

    def test_password_change_revokes_other_sessions_but_keeps_this_one(
            self, flask_app):
        a = make_client(flask_app)
        a.signup("policy1@test.local", ATHLETE_PW)
        a.login("policy1@test.local", ATHLETE_PW)
        b = make_client(flask_app)
        b.login("policy1@test.local", ATHLETE_PW)
        r = a.post("/api/profile/password",
                   {"current": ATHLETE_PW, "password": "Policy1!2026"})
        assert r.status_code == 200
        a.csrf = r.get_json()["csrf"]
        assert b.get("/api/me").status_code == 401, "other device survived"
        assert a.get("/api/me").status_code == 200, "acting tab was logged out"

    def test_reset_revokes_every_session(self, flask_app):
        a = make_client(flask_app)
        a.signup("policy2@test.local", ATHLETE_PW)
        a.login("policy2@test.local", ATHLETE_PW)
        r = make_client(flask_app)
        r.refresh_csrf()
        code = r.json(r.post("/api/auth/forgot",
                             {"email": "policy2@test.local"}))["dev_code"]
        r.refresh_csrf()
        r.post("/api/auth/reset", {"email": "policy2@test.local", "code": code,
                                   "password": "Policy2!2026"})
        assert a.get("/api/me").status_code == 401

    def test_role_change_revokes_the_targets_sessions(self, flask_app, owner,
                                                      athlete):
        target = user_id(flask_app, "athlete.a@test.local")
        owner.post("/api/admin/users/%d/role" % target, {"role": "coach"})
        assert athlete.get("/api/me").status_code == 401
