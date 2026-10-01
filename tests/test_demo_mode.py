# -*- coding: utf-8 -*-
"""DEMO_MODE: two opt-in demo logins that are safe to run in production.

These exercise `ensure_demo_accounts()` directly rather than re-importing the
application under a different environment, because the configuration is read
once at import. `bootstrap` binds the config names into its own namespace, so
monkeypatching them there is both accurate and isolated - the real
environment is never touched.
"""
import pytest

import athletix.config as cfg
from athletix import bootstrap
from athletix.config import PRIVILEGED_ROLES

import app as appmod

DEMO_ATHLETE = "demo.athlete@test.local"
DEMO_COACH = "demo.coach@test.local"
DEMO_PW = "DemoJudge!2026"
DEMO_PW_2 = "DemoJudge!2027-rotated"


def _enable(monkeypatch, password=DEMO_PW, mode=True,
            athlete=DEMO_ATHLETE, coach=DEMO_COACH):
    # Both namespaces: bootstrap bound the names at import (provisioning),
    # while the template flags and the demo_locked predicate read the config
    # module at call time.
    for mod in (bootstrap, cfg):
        monkeypatch.setattr(mod, "DEMO_MODE", mode)
        monkeypatch.setattr(mod, "DEMO_PASSWORD", password)
        monkeypatch.setattr(mod, "DEMO_ATHLETE_EMAIL", athlete)
        monkeypatch.setattr(mod, "DEMO_COACH_EMAIL", coach)


def _user(flask_app, email):
    with flask_app.app_context():
        return appmod.get_db().execute(
            "SELECT id, role, verified, name FROM users WHERE email = ?",
            (email,)).fetchone()


@pytest.fixture(autouse=True)
def _clean_demo_users(flask_app):
    """The shared clean_db fixture keeps `owner`; demo rows are ours to remove.
    The readiness set is process state, so it is reset too - otherwise a
    later test file would render a Demo Access panel it never asked for."""
    bootstrap._DEMO_READY.clear()
    yield
    bootstrap._DEMO_READY.clear()
    with flask_app.app_context():
        db = appmod.get_db()
        db.execute("DELETE FROM users WHERE email IN (?, ?)",
                   (DEMO_ATHLETE, DEMO_COACH))
        db.commit()


# ── creation ──────────────────────────────────────────────────────────────
def test_demo_accounts_created_when_enabled(flask_app, monkeypatch):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()

    a = _user(flask_app, DEMO_ATHLETE)
    c = _user(flask_app, DEMO_COACH)
    assert a is not None and c is not None
    assert a["role"] == "athlete"
    assert c["role"] == "coach"
    # both must be able to log in without an e-mail round trip
    assert a["verified"] == 1
    assert c["verified"] == 1


def test_demo_accounts_can_log_in(flask_app, monkeypatch, client):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()

    resp, data = client.login(DEMO_ATHLETE, DEMO_PW)
    assert resp.status_code == 200, data
    assert data["user"]["role"] == "athlete"
    assert client.get("/api/me").status_code == 200

    c2 = type(client)(flask_app.test_client())
    resp2, data2 = c2.login(DEMO_COACH, DEMO_PW)
    assert resp2.status_code == 200, data2
    assert data2["user"]["role"] == "coach"


def test_demo_athlete_gets_sample_data(flask_app, monkeypatch):
    """A judge should land on a populated dashboard, not an empty one."""
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    a = _user(flask_app, DEMO_ATHLETE)
    with flask_app.app_context():
        db = appmod.get_db()
        reports = db.execute(
            "SELECT COUNT(*) FROM reports WHERE athlete_id = ?",
            (a["id"],)).fetchone()[0]
        profile = db.execute(
            "SELECT sport FROM athlete_profiles WHERE user_id = ?",
            (a["id"],)).fetchone()
    assert reports > 0
    assert profile is not None


def test_sample_data_is_not_duplicated_on_restart(flask_app, monkeypatch):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    a = _user(flask_app, DEMO_ATHLETE)

    def count():
        with flask_app.app_context():
            return appmod.get_db().execute(
                "SELECT COUNT(*) FROM reports WHERE athlete_id = ?",
                (a["id"],)).fetchone()[0]

    first = count()
    bootstrap.ensure_demo_accounts()
    bootstrap.ensure_demo_accounts()
    assert count() == first


# ── works against a database that already has users ───────────────────────
def test_demo_accounts_created_on_non_empty_database(flask_app, monkeypatch,
                                                     athlete, coach):
    """This is the difference from SEED_DEMO, which only fills an empty DB."""
    with flask_app.app_context():
        existing = appmod.get_db().execute(
            "SELECT COUNT(*) FROM users").fetchone()[0]
    assert existing > 0

    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()

    assert _user(flask_app, DEMO_ATHLETE) is not None
    assert _user(flask_app, DEMO_COACH) is not None
    # the accounts that were already there are untouched
    with flask_app.app_context():
        assert appmod.get_db().execute(
            "SELECT COUNT(*) FROM users").fetchone()[0] == existing + 2


# ── password rotation ─────────────────────────────────────────────────────
def test_password_updates_when_demo_password_changes(flask_app, monkeypatch,
                                                     client):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    assert client.login(DEMO_ATHLETE, DEMO_PW)[0].status_code == 200

    _enable(monkeypatch, password=DEMO_PW_2)
    bootstrap.ensure_demo_accounts()

    fresh = type(client)(flask_app.test_client())
    assert fresh.login(DEMO_ATHLETE, DEMO_PW)[0].status_code != 200
    fresh2 = type(client)(flask_app.test_client())
    assert fresh2.login(DEMO_ATHLETE, DEMO_PW_2)[0].status_code == 200


def test_rotating_the_password_revokes_existing_sessions(flask_app,
                                                         monkeypatch, client):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    client.login(DEMO_ATHLETE, DEMO_PW)
    assert client.get("/api/me").status_code == 200

    _enable(monkeypatch, password=DEMO_PW_2)
    bootstrap.ensure_demo_accounts()

    assert client.get("/api/me").status_code == 401


# ── privileges ────────────────────────────────────────────────────────────
def test_demo_accounts_are_never_privileged(flask_app, monkeypatch):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    for email in (DEMO_ATHLETE, DEMO_COACH):
        assert _user(flask_app, email)["role"] not in PRIVILEGED_ROLES


@pytest.mark.parametrize("path", ["/api/admin/metrics", "/api/admin/users",
                                  "/api/admin/activity"])
def test_demo_accounts_are_refused_admin_endpoints(flask_app, monkeypatch,
                                                   client, path):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    client.login(DEMO_COACH, DEMO_PW)
    assert client.get(path).status_code == 403


def test_existing_privileged_account_is_not_turned_into_a_demo_login(
        flask_app, monkeypatch):
    """An address that already carries admin rights must not be handed the
    shared demo password, nor quietly demoted."""
    with flask_app.app_context():
        db = appmod.get_db()
        db.execute(
            "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
            "created_at) VALUES ('admin','Real Admin',?,'','x',1,?)",
            (DEMO_COACH, appmod.now_iso()))
        db.commit()

    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()

    row = _user(flask_app, DEMO_COACH)
    assert row["role"] == "admin"          # untouched
    assert row["name"] == "Real Admin"


def test_demo_email_matching_owner_email_is_refused(flask_app, monkeypatch):
    _enable(monkeypatch, athlete=bootstrap.OWNER_EMAIL)
    bootstrap.ensure_demo_accounts()
    with flask_app.app_context():
        row = appmod.get_db().execute(
            "SELECT role FROM users WHERE email = ?",
            (bootstrap.OWNER_EMAIL,)).fetchone()
    assert row["role"] == "owner"


# ── off by default ────────────────────────────────────────────────────────
def test_no_demo_accounts_when_mode_is_off(flask_app, monkeypatch):
    _enable(monkeypatch, mode=False)
    bootstrap.ensure_demo_accounts()
    assert _user(flask_app, DEMO_ATHLETE) is None
    assert _user(flask_app, DEMO_COACH) is None


def test_demo_mode_defaults_to_off():
    """An unconfigured deployment must not grow demo logins.

    conftest pins the environment without DEMO_MODE, so this asserts the
    real default the way a fresh deployment would see it.
    """
    import athletix.config as cfg
    assert cfg.DEMO_MODE is False
    assert cfg.DEMO_PASSWORD == ""


def test_no_demo_accounts_without_a_password(flask_app, monkeypatch):
    _enable(monkeypatch, password="")
    bootstrap.ensure_demo_accounts()
    assert _user(flask_app, DEMO_ATHLETE) is None
    assert _user(flask_app, DEMO_COACH) is None


def test_short_published_password_is_accepted(flask_app, monkeypatch,
                                              client):
    """The password is shown on the login page, so a strength rule would
    protect nothing - "12345" is exactly what the owner configured."""
    _enable(monkeypatch, password="12345")
    bootstrap.ensure_demo_accounts()
    assert _user(flask_app, DEMO_ATHLETE) is not None
    assert client.login(DEMO_ATHLETE, "12345")[0].status_code == 200


def test_overlong_demo_password_is_refused(flask_app, monkeypatch):
    """bcrypt ignores everything past 72 bytes; refusing beats truncating."""
    _enable(monkeypatch, password="x" * 73)
    bootstrap.ensure_demo_accounts()
    assert _user(flask_app, DEMO_ATHLETE) is None


def test_identical_demo_emails_are_refused(flask_app, monkeypatch):
    _enable(monkeypatch, coach=DEMO_ATHLETE)
    bootstrap.ensure_demo_accounts()
    assert _user(flask_app, DEMO_ATHLETE) is None


# ── the password never leaks ──────────────────────────────────────────────
def test_password_is_shown_inside_the_panel_only(flask_app, monkeypatch,
                                                client):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    page = client.get("/").get_data(as_text=True)
    panel = _panel(page)
    assert panel is not None and DEMO_PW in panel
    assert page.count(DEMO_PW) == 1


def test_password_is_never_returned_by_an_api(flask_app, monkeypatch, client):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    client.login(DEMO_ATHLETE, DEMO_PW)
    for path in ("/api/me", "/api/session", "/api/csrf"):
        assert DEMO_PW not in client.get(path).get_data(as_text=True)


PANEL = '<div class="demo-hint ax-demo-access">'
PANEL_END = "<!-- /demo-access -->"


def _panel(page):
    """The rendered Demo Access block, or None.

    Asserting on a slice matters here: the page also ships the translation
    dictionary, which legitimately contains the English source of the panel's
    labels whether or not the panel rendered.
    """
    start = page.find(PANEL)
    if start < 0:
        return None
    return page[start:page.find(PANEL_END, start)]


def test_demo_panel_is_hidden_unless_demo_mode(flask_app, monkeypatch, client):
    _enable(monkeypatch, mode=False)
    bootstrap.ensure_demo_accounts()
    page = client.get("/").get_data(as_text=True)
    assert _panel(page) is None
    assert DEMO_ATHLETE not in page
    assert DEMO_PW not in page


def test_demo_panel_is_hidden_when_provisioning_failed(flask_app,
                                                       monkeypatch, client):
    """DEMO_MODE on but nothing provisioned: advertising logins that do not
    exist is exactly the "box is there, login fails" bug."""
    _enable(monkeypatch, password="")
    bootstrap.ensure_demo_accounts()
    assert _panel(client.get("/").get_data(as_text=True)) is None


def test_demo_panel_shows_logins_password_and_try(flask_app, monkeypatch,
                                                  client):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    panel = _panel(client.get("/").get_data(as_text=True))
    assert panel is not None
    assert "Demo Access" in panel
    assert DEMO_ATHLETE in panel and DEMO_COACH in panel
    assert DEMO_PW in panel
    # one Try per login, carrying the address and the role the server checks
    assert 'data-demo-role="athlete"' in panel
    assert 'data-demo-email="%s"' % DEMO_ATHLETE in panel
    assert 'data-demo-role="coach"' in panel
    assert 'data-demo-email="%s"' % DEMO_COACH in panel
    assert panel.count('class="ax-demo-try"') == 2


def test_only_provisioned_logins_are_advertised(flask_app, monkeypatch,
                                                client):
    """If one address collides with a privileged account, only the other
    login may be offered."""
    with flask_app.app_context():
        db = appmod.get_db()
        db.execute(
            "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
            "created_at) VALUES ('admin','Real Admin',?,'','x',1,?)",
            (DEMO_COACH, appmod.now_iso()))
        db.commit()
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    panel = _panel(client.get("/").get_data(as_text=True))
    assert DEMO_ATHLETE in panel
    assert DEMO_COACH not in panel


# ── the published password cannot be used to break the demo ───────────────
LOCKED = [
    ("/api/profile/password", {"current": DEMO_PW, "password": "Hijack!2026x"}),
    ("/api/profile/email/request", {"email": "evil@test.local",
                                    "password": DEMO_PW}),
    ("/api/profile/email/confirm", {"email": "evil@test.local",
                                    "code": "123456"}),
    ("/api/profile/delete", {"password": DEMO_PW}),
    ("/api/auth/logout-all", {}),
]


@pytest.mark.parametrize("path,payload", LOCKED)
def test_demo_credentials_are_locked(flask_app, monkeypatch, client, path,
                                     payload):
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    client.login(DEMO_ATHLETE, DEMO_PW)
    client.refresh_csrf()
    assert client.post(path, payload).status_code == 403
    # and the next judge can still get in with the published password
    fresh = type(client)(flask_app.test_client())
    assert fresh.login(DEMO_ATHLETE, DEMO_PW)[0].status_code == 200
    assert _user(flask_app, DEMO_ATHLETE) is not None


def test_ordinary_users_are_not_locked(flask_app, monkeypatch, athlete):
    """The lock is scoped to the two demo addresses and nothing else."""
    from conftest import ATHLETE_PW
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    athlete.refresh_csrf()
    r = athlete.post("/api/profile/password",
                     {"current": ATHLETE_PW, "password": "BrandNew!2026x"})
    assert r.status_code == 200


def test_demo_accounts_can_still_use_ordinary_features(flask_app, monkeypatch,
                                                       client):
    """Locking credentials must not lock the product."""
    _enable(monkeypatch)
    bootstrap.ensure_demo_accounts()
    client.login(DEMO_ATHLETE, DEMO_PW)
    client.refresh_csrf()
    r = client.post("/api/profile/update", {"name": "Demo Athlete"})
    assert r.status_code == 200


def test_lock_lifts_when_demo_mode_is_off(monkeypatch):
    _enable(monkeypatch, mode=False)
    assert cfg.is_demo_email(DEMO_ATHLETE) is False
    _enable(monkeypatch)
    assert cfg.is_demo_email(DEMO_ATHLETE.upper()) is True
