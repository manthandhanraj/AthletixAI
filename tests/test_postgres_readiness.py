# -*- coding: utf-8 -*-
"""Phase 2.9 - PostgreSQL readiness.

Nothing here migrates anything. These tests pin the two properties that make
a later engine change tractable, and that quietly rot otherwise:

  * engine-specific code stays inside `database.py` and `repositories/` -
    no SQL in a route, a service, a DTO or a serializer;
  * the configuration recognises a PostgreSQL URL, refuses it explicitly
    (this build does not implement it), and never prints a password.

Plus a regression guard on the one SQLite-only construct the audit found and
removed: the implicit `rowid`.
"""

import os
import re

import pytest

import app as appmod
from athletix import config as cfg
from athletix.repositories import tokens as token_repo

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "athletix")

# Where SQL is allowed to live.
DATA_LAYER = ("database.py", "repositories", "bootstrap.py")

# Case-sensitive on purpose: every SQL statement in this codebase is written
# in upper case, so matching case-insensitively turns ordinary prose ("please
# select the correct role") into a false positive.
SQL_RE = re.compile(r"\b(SELECT\s|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
                    r"DELETE\s+FROM|CREATE\s+(TABLE|INDEX)|PRAGMA\s|"
                    r"ALTER\s+TABLE)")


def _python_files():
    for folder, _dirs, files in os.walk(ROOT):
        if "__pycache__" in folder:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            with open(path, encoding="utf-8") as fh:
                yield rel, fh.read()


def _code_lines(text):
    """Source lines with comments and docstring blocks removed.

    Crude but sufficient: the point is to find executable SQL, and every
    module here documents its SQL in prose that would otherwise match.
    """
    out, in_doc = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"""', "'''")):
            # A one-line docstring opens and closes on the same line.
            if not (len(stripped) > 3 and stripped.endswith(('"""', "'''"))):
                in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#") or stripped.startswith("--"):
            continue
        out.append(line)
    return "\n".join(out)


class TestEngineSpecificCodeIsContained:

    def test_no_sql_outside_the_data_layer(self):
        offenders = []
        for rel, text in _python_files():
            if any(rel == allowed or rel.startswith(allowed + "/")
                   for allowed in DATA_LAYER):
                continue
            for line in _code_lines(text).splitlines():
                if SQL_RE.search(line):
                    offenders.append("%s: %s" % (rel, line.strip()[:70]))
        assert not offenders, offenders

    def test_no_module_imports_sqlite3_outside_the_data_layer(self):
        offenders = [rel for rel, text in _python_files()
                     if "import sqlite3" in _code_lines(text)
                     and rel not in ("database.py",)]
        assert not offenders, offenders

    def test_rowid_is_no_longer_used_anywhere(self):
        """SQLite's implicit rowid has no PostgreSQL equivalent. It was the
        only such dependency and it is gone."""
        offenders = [rel for rel, text in _python_files()
                     if re.search(r"\browid\b", _code_lines(text))]
        assert not offenders, offenders

    def test_pragmas_live_only_in_the_database_module(self):
        offenders = [rel for rel, text in _python_files()
                     if "PRAGMA" in _code_lines(text) and rel != "database.py"]
        assert not offenders, offenders

    def test_routes_and_services_never_open_a_connection(self):
        offenders = []
        for rel, text in _python_files():
            if not (rel.startswith("api/") or rel.startswith("services/")
                    or rel.startswith("schemas/")):
                continue
            code = _code_lines(text)
            for needle in ("get_db(", "sqlite3.connect", ".commit()",
                           ".rollback()"):
                if needle in code:
                    offenders.append((rel, needle))
        assert not offenders, offenders


class TestTokenTableSurrogateKey:

    def test_email_tokens_has_an_explicit_primary_key(self, flask_app):
        with flask_app.app_context():
            cols = {r[1] for r in appmod.get_db().execute(
                "PRAGMA table_info(email_tokens)")}
        assert "id" in cols

    def test_the_full_otp_lifecycle_still_works(self, flask_app, client):
        """Issue, wrong guess, correct guess - all addressed by `id`."""
        from athletix.security.tokens import consume_token, issue_token
        with flask_app.app_context():
            code, _ = issue_token("token.user@test.local", "verify")
            assert consume_token("token.user@test.local", "verify",
                                 "000000") is None
            row = consume_token("token.user@test.local", "verify", code)
            assert row is not None and row["id"]
            # One-time use: the row is gone.
            assert consume_token("token.user@test.local", "verify",
                                 code) is None

    def test_issuing_a_new_code_invalidates_the_previous_one(self, flask_app):
        from athletix.security.tokens import consume_token, issue_token
        with flask_app.app_context():
            first, _ = issue_token("token.two@test.local", "reset")
            second, _ = issue_token("token.two@test.local", "reset")
            assert consume_token("token.two@test.local", "reset",
                                 first) is None
            assert consume_token("token.two@test.local", "reset",
                                 second) is not None

    def test_attempt_limiting_still_burns_the_code(self, flask_app):
        from athletix.config import TOKEN_MAX_ATTEMPTS
        from athletix.security.tokens import consume_token, issue_token
        with flask_app.app_context():
            code, _ = issue_token("token.three@test.local", "verify")
            for _ in range(TOKEN_MAX_ATTEMPTS):
                consume_token("token.three@test.local", "verify", "000000")
            assert consume_token("token.three@test.local", "verify",
                                 code) is None
            assert token_repo.count() == 0

    def test_only_hashes_are_stored(self, flask_app):
        from athletix.security.tokens import issue_token
        with flask_app.app_context():
            code, _ = issue_token("token.four@test.local", "verify")
            row = token_repo.latest("token.four@test.local", "verify")
            assert code not in str(dict(row))
            assert len(row["code_hash"]) == 64


class TestBackendConfiguration:

    def test_the_default_backend_is_sqlite(self):
        assert cfg.DB_BACKEND == "sqlite"
        assert cfg.db_backend("") == "sqlite"

    @pytest.mark.parametrize("url", [
        "postgres://u:p@host/db", "postgresql://u:p@host/db",
        "postgresql+psycopg://u:p@host/db",
    ])
    def test_postgres_urls_are_recognised(self, url):
        assert cfg.db_backend(url) == "postgresql"

    def test_an_unknown_scheme_is_an_error_not_a_silent_fallback(self):
        with pytest.raises(RuntimeError):
            cfg.db_backend("mysql://u:p@host/db")

    def test_an_unimplemented_backend_refuses_to_start(self, monkeypatch):
        from athletix import database
        monkeypatch.setattr("athletix.config.DB_BACKEND", "postgresql")
        monkeypatch.setattr("athletix.config.DATABASE_URL",
                            "postgresql://u:secret@host/db")
        with pytest.raises(RuntimeError) as exc:
            database.require_supported_backend()
        assert "POSTGRESQL_READINESS.md" in str(exc.value)
        assert "secret" not in str(exc.value), "the password leaked"

    def test_the_connection_url_is_redacted_before_it_is_shown(self):
        assert cfg.safe_database_url("postgresql://user:hunter2@host/db") == \
            "postgresql://user:***@host/db"
        assert "hunter2" not in cfg.safe_database_url(
            "postgresql://user:hunter2@host/db")
        # No credentials, nothing to redact.
        assert cfg.safe_database_url("sqlite:///local.db") == \
            "sqlite:///local.db"
        assert cfg.safe_database_url("") == ""

    def test_no_connection_credentials_are_hard_coded(self):
        """A URL may be *mentioned* in a comment; a credential may never be
        written down. This looks for the shape of one, in every module."""
        credential = re.compile(r"://[^\s'\"/]+:[^\s'\"/@]+@")
        offenders = []
        for rel, text in _python_files():
            for line in text.splitlines():
                if "***" in line:
                    continue            # the redaction helper's own format
                if credential.search(line):
                    offenders.append("%s: %s" % (rel, line.strip()[:60]))
        assert not offenders, offenders


class TestPortableBehaviourStillHolds:

    def test_email_lookup_is_case_insensitive(self, flask_app, athlete):
        """Pinned because it is provided by COLLATE NOCASE today and must be
        re-provided by citext (or a lower() index) after a port."""
        from athletix.repositories import users
        with flask_app.app_context():
            assert users.find_by_email("ATHLETE.A@TEST.LOCAL") is not None

    def test_duplicate_addresses_differing_only_in_case_are_refused(
            self, flask_app, athlete):
        c = appmod.app.test_client()
        from conftest import ATHLETE_PW, make_client
        dup = make_client(flask_app)
        dup.refresh_csrf()
        r = dup.post("/api/auth/signup",
                     {"role": "athlete", "name": "Dup",
                      "email": "ATHLETE.A@test.local",
                      "password": ATHLETE_PW})
        assert r.status_code == 409

    def test_timestamps_sort_lexicographically(self, flask_app, athlete):
        """The ordering guarantee every paginated query depends on."""
        rows = athlete.get("/api/my/reports").get_json()["reports"]
        athlete.post("/api/reports",
                     {"m": {k: 61 for k in appmod.METRICS}})
        athlete.post("/api/reports",
                     {"m": {k: 62 for k in appmod.METRICS}})
        dates = [r["date"] for r in
                 athlete.get("/api/my/reports").get_json()["reports"]]
        assert dates == sorted(dates)
        assert len(dates) == len(rows) + 2

    def test_the_upsert_is_ansi_on_conflict_syntax(self):
        from athletix.repositories.ratings import RatingRepository
        import inspect
        source = inspect.getsource(RatingRepository.upsert)
        assert "ON CONFLICT" in source and "excluded." in source
        # No SQLite-only spellings.
        assert "INSERT OR REPLACE" not in source
        assert "REPLACE INTO" not in source

    def test_boolean_columns_round_trip_as_real_bools(self, athlete):
        report = athlete.post("/api/reports",
                              {"m": {k: 63 for k in appmod.METRICS},
                               "live": True}).get_json()["report"]
        assert report["live"] is True
