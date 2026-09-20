"""Phase 2.3B/C — repository layer.

These assert the contract the data layer must keep, not its implementation:

  * repositories never control transactions (a write inside a rolled-back
    unit of work must vanish);
  * repositories never widen the privacy boundary — they return raw rows, and
    it stays the serializers' job to filter them;
  * column allowlists reject anything not explicitly permitted.
"""
import sqlite3

import pytest

from conftest import ATHLETE_PW, appmod, make_client, user_id

from athletix.repositories import (activity, messages, notifications, ratings,
                                   reports, users)
from athletix.unit_of_work import unit_of_work


class Boom(Exception):
    pass


# ==========================================================================
# The contract: repositories do not own transactions
# ==========================================================================
class TestRepositoriesDoNotCommit:

    def test_a_repository_write_is_undone_by_a_rollback(self, flask_app):
        with flask_app.app_context():
            with pytest.raises(Boom):
                with unit_of_work():
                    users.create("athlete", "Ghost", "ghost@repo.test", "",
                                 "hash", verified=0, created_at="2026-01-01")
                    raise Boom()
            assert users.find_by_email("ghost@repo.test") is None, \
                "repository committed on its own"

    def test_a_repository_write_persists_when_the_boundary_commits(self, flask_app):
        with flask_app.app_context():
            with unit_of_work():
                uid = users.create("athlete", "Kept", "kept@repo.test", "",
                                   "hash", verified=0, created_at="2026-01-01")
            row = users.find_by_email("kept@repo.test")
            assert row is not None and row["id"] == uid

    def test_multi_repository_writes_share_one_transaction(self, flask_app):
        """The whole point of the unit of work: writes across different
        repositories succeed or fail together."""
        with flask_app.app_context():
            with unit_of_work():
                uid = users.create("athlete", "Multi", "multi@repo.test", "",
                                   "hash", verified=1, created_at="2026-01-01")
            with pytest.raises(Boom):
                with unit_of_work():
                    reports.create(uid, "2026-01-02", "Cricket",
                                   {k: 50 for k in appmod.METRICS}, 50.0, 0)
                    notifications.create(uid, "should vanish", "2026-01-02")
                    raise Boom()
            assert reports.list_for_athlete(uid) == []
            assert notifications.list_for_user(uid) == []

    def test_no_repository_module_calls_commit_or_rollback(self):
        import glob
        import io
        offenders = []
        for f in glob.glob("athletix/repositories/*.py"):
            src = io.open(f, encoding="utf-8").read()
            for needle in (".commit()", ".rollback()"):
                if needle in src:
                    offenders.append((f, needle))
        assert not offenders, "data layer controls transactions: %r" % offenders


# ==========================================================================
# Column allowlists
# ==========================================================================
class TestRepositoryAllowlists:

    @pytest.mark.parametrize("field", ["role", "pass_hash", "sess_epoch",
                                       "verified", "id", "sport; DROP TABLE users"])
    def test_athlete_profile_field_allowlist(self, flask_app, field):
        with flask_app.app_context():
            with pytest.raises(ValueError):
                users.update_athlete_profile_field(1, field, "x")

    @pytest.mark.parametrize("field", ["role", "user_id", "pass_hash",
                                       "specialty=1; DROP TABLE users"])
    def test_coach_profile_field_allowlist(self, flask_app, field):
        with flask_app.app_context():
            with pytest.raises(ValueError):
                users.update_coach_profile(1, {field: "x"})

    def test_allowlisted_coach_fields_are_accepted(self, flask_app, coach):
        uid = user_id(flask_app, "coach.a@test.local")
        with flask_app.app_context():
            with unit_of_work():
                users.update_coach_profile(uid, {"specialty": "Sprints",
                                                 "city": "Pune"})
            row = users.coach_profile(uid)
        assert row["specialty"] == "Sprints" and row["city"] == "Pune"


# ==========================================================================
# Repositories return internal rows; privacy stays with the serializers
# ==========================================================================
class TestRepositoriesReturnInternalRows:

    def test_user_rows_contain_internal_columns(self, flask_app, athlete):
        """Repositories deliberately return everything - which is exactly why
        routes must never return a row directly."""
        with flask_app.app_context():
            row = users.find_by_email("athlete.a@test.local")
        assert "pass_hash" in row.keys()
        assert "sess_epoch" in row.keys()

    def test_the_serializer_still_strips_them(self, flask_app, athlete):
        with flask_app.app_context():
            row = users.find_by_email("athlete.a@test.local")
            out = appmod.user_public(row, row)
        assert "pass_hash" not in out and "sess_epoch" not in out

    def test_no_endpoint_leaks_internal_columns(self, flask_app, athlete,
                                                 coach, owner):
        """Behavioural guard, stronger than grepping for `dict(row)`: walk the
        readable API surface as each role and assert no internal column ever
        appears in a response body.

        `dict(row)` is legitimate where the query already projects safe
        columns - the operator audit feed does exactly that - so the check has
        to look at what is actually returned, not at how it is built.
        """
        banned = ("pass_hash", "sess_epoch", "code_hash", "token_hash",
                  "$2b$", "$2a$")
        surface = [
            (athlete, ["/api/me", "/api/state", "/api/my/reports",
                       "/api/my/messages", "/api/my/notifications",
                       "/api/export", "/api/public/stats"]),
            (coach, ["/api/me", "/api/state", "/api/coach/athletes"]),
            (owner, ["/api/me", "/api/state", "/api/admin/users",
                     "/api/admin/metrics", "/api/admin/activity"]),
        ]
        for client, paths in surface:
            for path in paths:
                r = client.get(path)
                assert r.status_code == 200, (path, r.status_code)
                text = r.get_data(as_text=True)
                for needle in banned:
                    assert needle not in text, (path, needle)


# ==========================================================================
# Behaviour of the individual repositories
# ==========================================================================
class TestUserRepository:

    def test_find_by_email_is_case_insensitive(self, flask_app, athlete):
        with flask_app.app_context():
            assert users.find_by_email("ATHLETE.A@TEST.LOCAL") is not None

    def test_role_scoped_lookup(self, flask_app, athlete):
        uid = user_id(flask_app, "athlete.a@test.local")
        with flask_app.app_context():
            assert users.find_by_id_and_role(uid, "athlete") is not None
            assert users.find_by_id_and_role(uid, "coach") is None

    def test_counts(self, flask_app, athlete, coach):
        with flask_app.app_context():
            assert users.count() >= 2
            assert users.count_by_role("athlete") >= 1
            assert users.count_by_role("coach") >= 1

    def test_bump_session_epoch_increments(self, flask_app, athlete):
        uid = user_id(flask_app, "athlete.a@test.local")
        with flask_app.app_context():
            before = users.find_by_id(uid)["sess_epoch"]
            with unit_of_work():
                users.bump_session_epoch(uid)
            assert users.find_by_id(uid)["sess_epoch"] == before + 1

    def test_email_taken_by_other_ignores_self(self, flask_app, athlete):
        uid = user_id(flask_app, "athlete.a@test.local")
        with flask_app.app_context():
            assert users.email_taken_by_other("athlete.a@test.local", uid) is False
            assert users.email_taken_by_other("athlete.a@test.local", uid + 999) is True


class TestReportRepository:

    def test_create_and_scope_by_athlete(self, flask_app, athlete, athlete_b):
        a = athlete.user["id"]
        b = athlete_b.user["id"]
        with flask_app.app_context():
            with unit_of_work():
                reports.create(a, "2026-02-01", "Cricket",
                               {k: 60 for k in appmod.METRICS}, 60.0, 0)
            mine = reports.list_for_athlete(a)
            theirs = reports.list_for_athlete(b)
        assert all(r["athlete_id"] == a for r in mine)
        assert all(r["athlete_id"] == b for r in theirs)

    def test_counts_live_only_when_flagged(self, flask_app, athlete):
        uid = athlete.user["id"]
        with flask_app.app_context():
            before = reports.count_live()
            with unit_of_work():
                reports.create(uid, "2026-02-02", "Cricket",
                               {k: 60 for k in appmod.METRICS}, 60.0, 1)
            assert reports.count_live() == before + 1


class TestMessageRepository:

    def test_conversation_is_symmetric_and_scoped(self, flask_app, athlete, coach):
        a, c = athlete.user["id"], coach.user["id"]
        with flask_app.app_context():
            with unit_of_work():
                messages.create(a, c, "Coach A", "hello", "2026-03-01")
            convo = messages.conversation(a, c)
            assert len(convo) == 1
            assert messages.conversation(c, a) == convo or len(
                messages.conversation(c, a)) == 1

    def test_has_written_to_backs_the_reply_policy(self, flask_app, athlete, coach):
        a, c = athlete.user["id"], coach.user["id"]
        with flask_app.app_context():
            assert messages.has_written_to(c, a) is False
            with unit_of_work():
                messages.create(a, c, "Coach A", "first contact", "2026-03-02")
            assert messages.has_written_to(c, a) is True


class TestNotificationRepository:

    def test_mark_all_read_is_scoped_to_one_user(self, flask_app, athlete,
                                                 athlete_b):
        a, b = athlete.user["id"], athlete_b.user["id"]
        with flask_app.app_context():
            with unit_of_work():
                notifications.create(a, "mine", "2026-04-01")
                notifications.create(b, "theirs", "2026-04-01")
            with unit_of_work():
                notifications.mark_all_read(a)
            assert all(r["read"] for r in notifications.list_for_user(a))
            assert all(not r["read"] for r in notifications.list_for_user(b))

    def test_limit_is_honoured(self, flask_app, athlete):
        uid = athlete.user["id"]
        with flask_app.app_context():
            with unit_of_work():
                for i in range(5):
                    notifications.create(uid, "n%d" % i, "2026-04-0%d" % (i + 1))
            assert len(notifications.list_for_user(uid, limit=3)) == 3


class TestRatingRepository:

    def test_upsert_replaces_only_the_callers_row(self, flask_app, athlete,
                                                  athlete_b, coach):
        a, b, c = athlete.user["id"], athlete_b.user["id"], coach.user["id"]
        with flask_app.app_context():
            with unit_of_work():
                ratings.upsert(c, a, 5, "2026-05-01")
                ratings.upsert(c, b, 2, "2026-05-01")
                ratings.upsert(c, a, 1, "2026-05-02")     # a changes their mind
            rows = {r["athlete_id"]: r["stars"] for r in ratings.list_all()}
        assert rows[a] == 1 and rows[b] == 2


class TestActivityRepository:

    def test_append_and_recent(self, flask_app):
        with flask_app.app_context():
            with unit_of_work():
                activity.append(None, "repo_test_event", "detail", "127.0.0.1",
                                "2026-06-01", "agent")
            actions = [r["action"] for r in activity.recent(50)]
        assert "repo_test_event" in actions

    def test_recent_respects_the_limit(self, flask_app):
        with flask_app.app_context():
            with unit_of_work():
                for i in range(6):
                    activity.append(None, "bulk%d" % i, "", "", "2026-06-0%d" % (i + 1), "")
            assert len(activity.recent(3)) == 3
