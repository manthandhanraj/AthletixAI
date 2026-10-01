"""Shared fixtures for the AthletixAI security test suite.

Every test runs against a throwaway SQLite database in a temp directory, with
the environment pinned *before* app.py is imported so the module-level
configuration (secret key, e-mail mode, seeding) is deterministic and never
touches the developer's real .env values or athletixai.db.
"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="athletix-tests-")

# Pinned before `import app`. app.py calls os.environ.setdefault() when it
# reads .env, so anything set here wins over the developer's real file.
os.environ.update(
    DB_PATH=os.path.join(_TMPDIR, "test.db"),
    SECRET_KEY="test-secret-key-that-is-long-enough-for-the-policy-check",
    SMTP_HOST="",                 # e-mail dev mode -> OTPs returned in JSON
    SMTP_USER="", SMTP_PASS="",
    OWNER_EMAIL="owner@test.local",
    OWNER_PASSWORD="OwnerPass!2026",
    SEED_DEMO="0",                # no demo roster; tests build what they need
    # Pinned off so a developer's DEMO_MODE=1 in .env cannot provision demo
    # logins into the test database; test_demo_mode.py turns it on per test.
    DEMO_MODE="0",
    DEMO_PASSWORD="",
    APP_ENV="test",
    TRUST_PROXY="",
)
# Test-only bcrypt cost. At the production factor of 12 this suite takes
# ~18 minutes, which is how security suites end up never being run. app.py
# clamps this back to >= 12 whenever a production signal is set, so it cannot
# weaken a real deployment. setdefault (not update) so a run can be forced
# back to the real cost with BCRYPT_ROUNDS=12 to check timing-sensitive
# behaviour.
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.pop("FLASK_DEBUG", None)

import app as appmod            # noqa: E402  (import must follow the env setup)

ATHLETE_PW = "AthletePass!2026"
COACH_PW = "CoachPass!2026"


class Client:
    """Thin wrapper that carries the CSRF token like the real frontend does."""

    def __init__(self, flask_client):
        self.c = flask_client
        self.csrf = None
        self.user = None

    # ── plumbing ──────────────────────────────────────────────────────
    def refresh_csrf(self):
        self.csrf = self.get("/api/csrf").get_json()["token"]
        return self.csrf

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, csrf="auto", **kw):
        headers = dict(kw.pop("headers", {}))
        token = self.csrf if csrf == "auto" else csrf
        if token is not None:
            headers["X-CSRF-Token"] = token
        return self.c.post(path, json=(data if data is not None else {}),
                           headers=headers, **kw)

    def json(self, resp):
        return resp.get_json() or {}

    # ── flows ─────────────────────────────────────────────────────────
    def signup(self, email, password, role="athlete", name="Test User",
               verify=True, **extra):
        self.refresh_csrf()
        payload = {"role": role, "name": name, "email": email,
                   "password": password}
        payload.update(extra)
        resp = self.post("/api/auth/signup", payload)
        data = self.json(resp)
        if verify:
            # A caller that asked for a verified account must GET one. Failing
            # here names the real problem; letting it slide surfaces later as
            # a confusing 401 on an unrelated assertion and can mask a genuine
            # authorization regression.
            assert resp.status_code == 200, (
                "signup(%s) failed: %s %s" % (email, resp.status_code, data))
            assert data.get("dev_code"), (
                "signup(%s) returned no dev_code: %s" % (email, data))
            self.refresh_csrf()
            vr = self.post("/api/auth/verify",
                           {"email": email, "code": data["dev_code"]})
            assert vr.status_code == 200, (
                "verify(%s) failed: %s %s"
                % (email, vr.status_code, self.json(vr)))
        return resp, data

    def login(self, email, password, **extra):
        self.refresh_csrf()
        payload = {"email": email, "password": password}
        payload.update(extra)
        resp = self.post("/api/auth/login", payload)
        data = self.json(resp)
        if data.get("csrf"):
            self.csrf = data["csrf"]
            self.user = data.get("user")
        return resp, data

    def logout(self):
        self.refresh_csrf()
        r = self.post("/api/auth/logout")
        self.user = None
        return r

    def state(self):
        return self.json(self.get("/api/state")).get("state") or {}


@pytest.fixture
def flask_app():
    appmod.app.config["TESTING"] = True
    return appmod.app


@pytest.fixture(autouse=True)
def clean_db(flask_app):
    """Fresh data and fresh rate-limit counters for every single test."""
    appmod.limiter._store.clear()
    with flask_app.app_context():
        db = appmod.get_db()
        for table in ("activity_logs", "email_tokens", "notifications",
                      "ratings", "messages", "videos", "ai_results",
                      "reports", "profile_settings", "coach_profiles",
                      "athlete_profiles"):
            db.execute("DELETE FROM %s" % table)
        db.execute("DELETE FROM users WHERE role <> 'owner'")
        db.commit()
    yield
    appmod.limiter._store.clear()


def make_client(flask_app):
    return Client(flask_app.test_client())


@pytest.fixture
def client(flask_app):
    return make_client(flask_app)


def _logged_in(c, email, password, **extra):
    """Log in and prove it worked, so a broken fixture fails at the fixture."""
    resp, data = c.login(email, password, **extra)
    assert resp.status_code == 200, (
        "login(%s) failed: %s %s" % (email, resp.status_code, data))
    assert c.user, "login(%s) returned no user" % email
    assert c.get("/api/me").status_code == 200, (
        "session not usable after login(%s)" % email)
    return c


@pytest.fixture
def athlete(flask_app):
    """A verified, logged-in athlete."""
    c = make_client(flask_app)
    c.signup("athlete.a@test.local", ATHLETE_PW, role="athlete",
             name="Athlete A", sport="Cricket", age=17)
    return _logged_in(c, "athlete.a@test.local", ATHLETE_PW)


@pytest.fixture
def athlete_b(flask_app):
    c = make_client(flask_app)
    c.signup("athlete.b@test.local", ATHLETE_PW, role="athlete",
             name="Athlete B", sport="Football", age=18)
    return _logged_in(c, "athlete.b@test.local", ATHLETE_PW)


@pytest.fixture
def coach(flask_app):
    c = make_client(flask_app)
    c.signup("coach.a@test.local", COACH_PW, role="coach", name="Coach A")
    return _logged_in(c, "coach.a@test.local", COACH_PW)


@pytest.fixture
def owner(flask_app):
    c = make_client(flask_app)
    return _logged_in(c, "owner@test.local", "OwnerPass!2026")


@pytest.fixture
def admin(flask_app, owner):
    """An admin, created the only legitimate way: promoted by the owner."""
    c = make_client(flask_app)
    c.signup("admin.a@test.local", COACH_PW, role="coach", name="Admin A")
    with flask_app.app_context():
        db = appmod.get_db()
        uid = db.execute("SELECT id FROM users WHERE email=?",
                         ("admin.a@test.local",)).fetchone()["id"]
    pr = owner.post("/api/admin/users/%d/role" % uid, {"role": "admin"})
    assert pr.status_code == 200, "owner could not promote admin: %s" % pr.status_code
    return _logged_in(c, "admin.a@test.local", COACH_PW)


def user_id(flask_app, email):
    with flask_app.app_context():
        row = appmod.get_db().execute("SELECT id FROM users WHERE email=?",
                                      (email,)).fetchone()
        return row["id"] if row else None
