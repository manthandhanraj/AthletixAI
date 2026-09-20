"""Phase 2.2 foundation tests.

Covers the architectural boundaries introduced in this sub-phase:
the application factory, configuration isolation, absence of import-time side
effects, the health endpoints, the error taxonomy, and route registration.

These are behaviour tests, not structure tests — they assert what the
application *does*, so a later reorganisation cannot silently break them.
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, appmod


# ==========================================================================
# Application factory
# ==========================================================================
class TestApplicationFactory:

    def test_create_app_is_the_construction_point(self):
        from athletix import create_app
        assert callable(create_app)

    def test_factory_builds_an_independent_app(self, tmp_path):
        """Two apps can coexist in one process - impossible before the
        factory, because construction was module import."""
        from athletix import create_app
        a = create_app(initialize=False)
        b = create_app({"TESTING": True}, initialize=False)
        assert a is not b
        assert b.config["TESTING"] is True
        assert a.config["TESTING"] is False

    def test_config_overrides_are_applied_after_defaults(self):
        from athletix import create_app
        app = create_app({"TESTING": True, "SESSION_COOKIE_NAME": "override_x"},
                         initialize=False)
        assert app.config["SESSION_COOKIE_NAME"] == "override_x"
        # untouched defaults survive
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True

    def test_initialize_false_does_not_touch_the_database(self, tmp_path):
        from athletix import create_app
        create_app(initialize=False)
        # Nothing to assert beyond "did not raise and did not need a DB";
        # the subprocess test below proves the filesystem claim.

    def test_security_defaults_survive_the_factory(self, flask_app):
        cfg = flask_app.config
        assert cfg["SESSION_COOKIE_HTTPONLY"] is True
        assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert cfg["SESSION_COOKIE_NAME"] == "athletix_session"
        assert cfg["MAX_CONTENT_LENGTH"] == 8 * 1024 * 1024
        assert cfg["TRAP_HTTP_EXCEPTIONS"] is False

    def test_request_hooks_and_teardown_are_registered(self, flask_app):
        names = {f.__name__ for fns in flask_app.before_request_funcs.values()
                 for f in fns}
        assert "limit_body_size" in names
        assert "csrf_protect" in names
        after = {f.__name__ for fns in flask_app.after_request_funcs.values()
                 for f in fns}
        assert "security_headers" in after
        teardown = {f.__name__ for f in flask_app.teardown_appcontext_funcs}
        assert "close_db" in teardown


# ==========================================================================
# Import-time side effects (Phase 2.1 finding D1)
# ==========================================================================
class TestNoImportSideEffects:

    def _run(self, code, db_path, extra_env=None):
        env = dict(os.environ)
        env.update(
            DB_PATH=str(db_path),
            SECRET_KEY="foundation-test-key-long-enough-0000000000",
            SMTP_HOST="", SMTP_USER="", SMTP_PASS="",
            OWNER_EMAIL="owner@foundation.test",
            OWNER_PASSWORD="FoundationOwner!2026",
            SEED_DEMO="1", APP_ENV="test", BCRYPT_ROUNDS="4",
            PYTHONPATH=ROOT,
        )
        env.pop("FLASK_DEBUG", None)
        env.update(extra_env or {})
        r = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-2000:]
        return r.stdout

    def test_importing_the_package_creates_no_database(self, tmp_path):
        db = tmp_path / "must_not_exist.db"
        out = self._run(
            "import os\n"
            "import athletix, athletix.serializers, athletix.validation\n"
            "import athletix.security.session, athletix.api.auth, athletix.mail\n"
            "print('EXISTS', os.path.exists(os.environ['DB_PATH']))\n", db)
        assert "EXISTS False" in out
        assert not db.exists()

    def test_factory_without_initialize_creates_no_database(self, tmp_path):
        db = tmp_path / "no_init.db"
        out = self._run(
            "import os\n"
            "from athletix import create_app\n"
            "create_app(initialize=False)\n"
            "print('EXISTS', os.path.exists(os.environ['DB_PATH']))\n", db)
        assert "EXISTS False" in out
        assert not db.exists()

    def test_explicit_initialize_does_create_the_database(self, tmp_path):
        db = tmp_path / "yes_init.db"
        out = self._run(
            "import os\n"
            "from athletix import create_app\n"
            "create_app()\n"
            "print('EXISTS', os.path.exists(os.environ['DB_PATH']))\n", db)
        assert "EXISTS True" in out

    def test_importing_does_not_seed_or_provision_an_owner(self, tmp_path):
        """Seeding and owner bootstrap are startup tasks, not import effects."""
        db = tmp_path / "no_seed.db"
        out = self._run(
            "import os, sqlite3\n"
            "import athletix.bootstrap\n"
            "print('EXISTS', os.path.exists(os.environ['DB_PATH']))\n", db)
        assert "EXISTS False" in out


# ==========================================================================
# Health endpoints
# ==========================================================================
class TestHealthEndpoints:

    def test_liveness_is_public_and_stable(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.get_json() == {"status": "ok"}

    def test_readiness_reports_database_reachability(self, client):
        r = client.get("/health/ready")
        assert r.status_code == 200
        assert r.get_json() == {"status": "ready"}

    def test_health_requires_no_authentication(self, client):
        """A load balancer cannot log in."""
        assert client.get("/health").status_code == 200
        assert client.get("/api/me").status_code == 401   # control

    def test_health_leaks_no_infrastructure_detail(self, client):
        for path in ("/health", "/health/ready"):
            body = client.get(path).get_data(as_text=True)
            low = body.lower()
            for needle in ("path", "sqlite", "version", "host", "secret",
                           "traceback", "athletixai.db", "c:\\", "/home/"):
                assert needle not in low, (path, needle)

    def test_health_does_not_mutate_state(self, flask_app, client):
        with flask_app.app_context():
            before = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs").fetchone()[0]
        client.get("/health")
        client.get("/health/ready")
        with flask_app.app_context():
            after = appmod.get_db().execute(
                "SELECT COUNT(*) FROM activity_logs").fetchone()[0]
        assert after == before

    def test_health_is_not_under_the_api_prefix(self, flask_app):
        rules = {str(r.rule) for r in flask_app.url_map.iter_rules()}
        assert "/health" in rules and "/health/ready" in rules


# ==========================================================================
# Error taxonomy
# ==========================================================================
class TestErrorTaxonomy:

    def test_exception_classes_map_to_the_right_status_and_code(self):
        from athletix.errors import (AuthenticationError, AuthorizationError,
                                     ConflictError, ErrorCode,
                                     NotFoundError, PayloadTooLargeError,
                                     RateLimitError, ValidationError)
        expected = [
            (ValidationError, 400, ErrorCode.VALIDATION_ERROR),
            (AuthenticationError, 401, ErrorCode.AUTHENTICATION_REQUIRED),
            (AuthorizationError, 403, ErrorCode.FORBIDDEN),
            (NotFoundError, 404, ErrorCode.NOT_FOUND),
            (ConflictError, 409, ErrorCode.CONFLICT),
            (PayloadTooLargeError, 413, ErrorCode.PAYLOAD_TOO_LARGE),
            (RateLimitError, 429, ErrorCode.RATE_LIMITED),
        ]
        for cls, status, code in expected:
            e = cls()
            assert e.status == status and e.code == code

    def test_app_error_renders_the_backward_compatible_shape(self, flask_app):
        """The frontend reads `error`; `code` is additive."""
        from athletix.errors import ValidationError
        with flask_app.test_request_context("/api/x"):
            resp, status = ValidationError("bad thing").to_response()
            body = resp.get_json()
        assert status == 400
        assert body["ok"] is False
        assert body["error"] == "bad thing"
        assert body["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("path,status", [
        ("/api/definitely-not-a-route", 404),
        ("/api/me", 401),
    ])
    def test_error_responses_carry_a_machine_readable_code(self, client, path, status):
        r = client.get(path)
        assert r.status_code == status
        body = r.get_json()
        assert body["ok"] is False
        assert isinstance(body.get("code"), str) and body["code"]

    def test_403_and_405_are_coded(self, athlete):
        r = athlete.get("/api/admin/users")
        assert r.status_code == 403 and r.get_json()["code"] == "FORBIDDEN"
        r2 = athlete.c.delete("/api/state",
                              headers={"X-CSRF-Token": athlete.csrf})
        assert r2.status_code == 405
        assert r2.get_json()["code"] == "METHOD_NOT_ALLOWED"

    def test_errors_never_leak_internals(self, athlete):
        for resp in (athlete.get("/api/reports/999999"),
                     athlete.get("/api/nope"),
                     athlete.post("/api/messages", {}),
                     athlete.get("/api/admin/users")):
            text = resp.get_data(as_text=True)
            for needle in ("Traceback", "sqlite3", "SELECT ", "athletix/",
                           "athletix\\", "C:\\", "/home/", "Werkzeug",
                           "File \""):
                assert needle not in text, (needle, resp.status_code)

    def test_html_404_still_returns_the_spa_shell(self, client):
        """Client-side routing depends on this; it must not become JSON."""
        r = client.get("/some/deep/link", headers={"Accept": "text/html"})
        assert r.status_code == 404
        assert b"<!DOCTYPE html" in r.get_data() or b"<html" in r.get_data()


# ==========================================================================
# Configuration boundary
# ==========================================================================
class TestConfigurationBoundary:

    def test_config_is_a_single_module(self):
        from athletix import config
        for name in ("SPORTS", "RATE_RULES", "PASSWORD_MIN", "OWNER_EMAIL",
                     "SESSION_IDLE_SECONDS", "BCRYPT_ROUNDS", "DEV_CODES"):
            assert hasattr(config, name), name

    def test_constants_match_the_pre_refactor_baseline(self):
        """Guards against silent policy drift during the move."""
        from athletix import config
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_baseline_constants.json")
        with open(path) as fh:
            snap = json.load(fh)
        # Environment-derived values, not policy: they legitimately differ
        # between the snapshot run and the test run.
        skip = {"DB_PATH", "SECRET_KEY", "BASE_DIR", "SEED_DEMO",
                "BCRYPT_ROUNDS", "OWNER_EMAIL", "OWNER_PASSWORD"}
        checked, drift = 0, []
        for name, old in snap.items():
            if name.startswith("_") or name in skip:
                continue
            if not hasattr(config, name):
                continue
            new = getattr(config, name)
            if hasattr(new, "pattern"):
                new = "re:" + new.pattern
            elif isinstance(new, (list, tuple)):
                new = [x.decode("latin-1") if isinstance(x, bytes) else x
                       for x in new]
            elif isinstance(new, dict):
                new = {str(k): (list(v) if isinstance(v, tuple) else v)
                       for k, v in new.items()}
            checked += 1
            if new != old:
                drift.append((name, old, new))
        assert checked >= 25, "baseline too small to be meaningful"
        assert not drift, "configuration drifted: %r" % (drift,)

    def test_flask_settings_are_derived_from_config(self):
        from athletix import config
        s = config.flask_settings()
        assert s["SESSION_COOKIE_SECURE"] == config.COOKIE_SECURE
        assert s["SECRET_KEY"] == config.SECRET_KEY

    def test_bcrypt_floor_still_holds_after_the_move(self, monkeypatch):
        from athletix import config
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        assert config._bcrypt_rounds() >= 12


# ==========================================================================
# Route registration
# ==========================================================================
class TestRouteRegistration:

    def test_all_routes_are_registered_through_blueprints(self, flask_app):
        endpoints = [r.endpoint for r in flask_app.url_map.iter_rules()
                     if r.endpoint != "static"]
        # Every endpoint is namespaced by its blueprint, i.e. contains a dot.
        unqualified = [e for e in endpoints if "." not in e]
        assert not unqualified, "routes bypassing blueprints: %r" % unqualified

    def test_expected_blueprints_are_mounted(self, flask_app):
        assert {"health", "pages", "auth", "profile", "assessments",
                "messaging", "notifications", "ratings", "account",
                "admin"} <= set(flask_app.blueprints)

    def test_public_api_contract_is_unchanged(self, flask_app):
        """Every Phase 1 route still exists with the same method and path."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_baseline_constants.json")
        with open(path) as fh:
            expected = set(json.load(fh)["_routes"])
        actual = set(
            "%s %s" % (sorted(r.methods - {"HEAD", "OPTIONS"})[0], str(r.rule))
            for r in flask_app.url_map.iter_rules() if r.endpoint != "static")
        missing = expected - actual
        assert not missing, "API contract broken, missing: %r" % sorted(missing)
        # The only additions are the health probes and the Phase 2.6 /api/v1
        # namespace. Nothing else may appear.
        added = actual - expected
        v1 = {r for r in added if r.split(" ", 1)[1].startswith("/api/v1/")}
        # Deliberate additions since the Phase 1 baseline. Health probes
        # (2.2) and the Phase 3.1 directory/summary endpoints that replaced
        # the frontend's bulk /api/state hydration. Nothing was removed:
        # `missing` above proves every Phase 1 route still exists.
        intentional = {
            "GET /health", "GET /health/ready",
            "GET /api/directory/athletes", "GET /api/directory/coaches",
            "GET /api/summary/platform",
            "GET /api/admin/coaches/<int:cid>/ratings",
        }
        assert added - v1 == intentional,             "unexpected new routes: %r" % sorted(added - v1 - intentional)

    def test_every_api_route_has_a_v1_twin(self, flask_app):
        """/api/v1 is a mount of the same blueprints, not a second API.

        Both directions are checked: an endpoint that exists only under
        /api would be missing from the versioned contract, and one that
        exists only under /api/v1 would be a route the compatibility alias
        silently dropped.
        """
        rules = set(
            "%s %s" % (sorted(r.methods - {"HEAD", "OPTIONS"})[0], str(r.rule))
            for r in flask_app.url_map.iter_rules() if r.endpoint != "static")
        legacy = {r for r in rules
                  if r.split(" ", 1)[1].startswith("/api/")
                  and not r.split(" ", 1)[1].startswith("/api/v1/")}
        versioned = {r for r in rules
                     if r.split(" ", 1)[1].startswith("/api/v1/")}
        expected_v1 = {r.replace("/api/", "/api/v1/", 1) for r in legacy}
        assert versioned == expected_v1
        assert len(legacy) == 42

    def test_both_mounts_run_the_same_handler(self, flask_app):
        """No duplicated business logic: the view function object behind
        /api/x and /api/v1/x is literally the same object."""
        views = flask_app.view_functions
        pairs = 0
        for endpoint, fn in list(views.items()):
            if not endpoint.startswith("v1_"):
                continue
            assert views[endpoint[3:]] is fn, endpoint
            pairs += 1
        assert pairs == 42

    def test_static_rooted_assets_are_served(self, client):
        """Regression: inside a package, send_from_directory("static", ...)
        resolves against athletix/ instead of the project root, so the PWA
        manifest and service worker 404ed after the move."""
        r = client.get("/manifest.json")
        assert r.status_code == 200
        assert b"{" in r.get_data()
        sw = client.get("/sw.js")
        assert sw.status_code == 200
        assert b"serviceWorker" in sw.get_data() or b"self.addEventListener" in sw.get_data()
        assert sw.headers.get("Service-Worker-Allowed") == "/"

    def test_templates_render_from_the_project_root(self, client):
        for path in ("/", "/privacy", "/terms"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert b"<html" in r.get_data().lower() or b"<!doctype" in r.get_data().lower()

    def test_no_duplicate_route_rules(self, flask_app):
        seen = {}
        for r in flask_app.url_map.iter_rules():
            if r.endpoint == "static":
                continue
            for m in r.methods - {"HEAD", "OPTIONS"}:
                key = (m, str(r.rule))
                assert key not in seen, "duplicate route %r" % (key,)
                seen[key] = r.endpoint
