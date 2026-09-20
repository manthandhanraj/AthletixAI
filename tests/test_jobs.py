# -*- coding: utf-8 -*-
"""Phase 2.7 - the asynchronous work boundary.

Phase 2.1 finding A3: `send_email()` ran inside request handlers. Its SMTP
client waits up to 20 seconds per attempt across two ports, so one unreachable
mail host blocked a worker for ~40 seconds on signup, on login of an
unverified account and on password reset.

These tests prove the fix rather than assert it:

  * a deliberately slow transport is installed and the request is TIMED;
  * the thread the transport runs on is recorded and compared with the
    request thread;
  * the security-relevant half (token issued and committed, session revoked,
    rate limits) is checked to still happen inside the request.
"""

import os
import threading
import time

import pytest

import app as appmod
from athletix import jobs
from athletix.jobs.dispatcher import (BackgroundDispatcher, Dispatcher,
                                      InlineDispatcher)

from conftest import ATHLETE_PW, make_client

SLOW = 0.75          # a "slow SMTP host", in seconds
FAST = 0.30          # a request that waited on it could not beat this


@pytest.fixture
def restore_dispatcher():
    """Any test that swaps the dispatcher must put the old one back."""
    previous = jobs.get_dispatcher()
    yield
    jobs.set_dispatcher(previous)


@pytest.fixture
def background(restore_dispatcher):
    dispatcher = BackgroundDispatcher(workers=2, queue_size=8)
    jobs.set_dispatcher(dispatcher)
    yield dispatcher
    dispatcher.shutdown(wait=False)


class Recorder:
    """Stands in for the SMTP transport and remembers what it was asked."""

    def __init__(self, delay=0.0, fail=False):
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.threads = []
        self.done = threading.Event()

    def __call__(self, to_addr, subject, html):
        self.threads.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((to_addr, subject, html))
        self.done.set()
        if self.fail:
            raise RuntimeError("smtp exploded")
        return True


# ==========================================================================
# Dispatcher semantics
# ==========================================================================
class TestDispatchers:

    def test_inline_runs_now_and_reports_the_real_result(self):
        d = InlineDispatcher()
        assert d.submit(lambda: True) is True
        assert d.submit(lambda: False) is False
        assert d.submit(lambda: None) is True

    def test_a_failing_job_never_propagates(self):
        def boom():
            raise RuntimeError("nope")
        assert InlineDispatcher().submit(boom) is False

    def test_background_returns_before_the_job_finishes(self, background):
        rec = Recorder(delay=SLOW)
        started = time.time()
        assert background.submit(rec, "a@test.local", "s", "<p>h</p>") is True
        assert time.time() - started < FAST, "submit() waited for the job"
        assert rec.done.wait(5), "the job never ran"
        assert rec.calls == [("a@test.local", "s", "<p>h</p>")]

    def test_background_runs_off_the_calling_thread(self, background):
        rec = Recorder()
        background.submit(rec, "a@test.local", "s", "h")
        assert rec.done.wait(5)
        assert rec.threads[0] != threading.current_thread().name
        assert rec.threads[0].startswith("athletix-job-")

    def test_a_failing_background_job_does_not_kill_the_worker(self,
                                                               background):
        bad = Recorder(fail=True)
        good = Recorder()
        background.submit(bad, "a@test.local", "s", "h")
        assert bad.done.wait(5)
        background.submit(good, "b@test.local", "s", "h")
        assert good.done.wait(5), "the pool stopped after one failed job"

    def test_a_saturated_queue_refuses_instead_of_growing(self,
                                                          restore_dispatcher):
        blocked = threading.Event()
        d = BackgroundDispatcher(workers=1, queue_size=2)
        jobs.set_dispatcher(d)
        try:
            d.submit(blocked.wait, 5)          # occupies the single worker
            accepted = [d.submit(lambda: None) for _ in range(6)]
            assert False in accepted, "an unbounded queue would accept all"
            assert d.pending() <= 2
        finally:
            blocked.set()
            d.shutdown(wait=False)

    def test_dispatch_never_raises(self, restore_dispatcher):
        class Broken(Dispatcher):
            def submit(self, fn, *a, **kw):
                raise RuntimeError("dispatcher is down")
        jobs.set_dispatcher(Broken())
        assert jobs.dispatch(lambda: None) is False

    def test_configuration_picks_the_documented_backend(self):
        from athletix import config as cfg
        default = jobs._default_dispatcher()
        if cfg.JOB_BACKEND:
            expected = cfg.JOB_BACKEND
        else:
            expected = "inline" if cfg.EMAIL_DEV_MODE else "background"
        assert default.name == expected


# ==========================================================================
# The request path
# ==========================================================================
class TestNoBlockingSmtpInRequests:

    def _slow_transport(self, monkeypatch, fail=False):
        rec = Recorder(delay=SLOW, fail=fail)
        monkeypatch.setattr("athletix.mail.send_email", rec)
        return rec

    def test_signup_does_not_wait_for_smtp(self, flask_app, background,
                                           monkeypatch):
        rec = self._slow_transport(monkeypatch)
        c = make_client(flask_app)
        c.refresh_csrf()
        started = time.time()
        r = c.post("/api/auth/signup",
                   {"role": "athlete", "name": "Async A",
                    "email": "async.a@test.local", "password": ATHLETE_PW})
        elapsed = time.time() - started
        assert r.status_code == 200
        assert elapsed < FAST, "the request waited on SMTP (%.2fs)" % elapsed
        assert rec.done.wait(5)
        assert rec.calls[0][0] == "async.a@test.local"

    def test_password_reset_request_does_not_wait_for_smtp(self, flask_app,
                                                           background,
                                                           monkeypatch):
        c = make_client(flask_app)
        c.signup("async.reset@test.local", ATHLETE_PW, name="Async R")
        rec = self._slow_transport(monkeypatch)
        c.refresh_csrf()
        started = time.time()
        r = c.post("/api/auth/forgot", {"email": "async.reset@test.local"})
        elapsed = time.time() - started
        assert r.status_code == 200
        assert elapsed < FAST, "the request waited on SMTP (%.2fs)" % elapsed
        assert rec.done.wait(5)

    def test_smtp_never_runs_on_the_request_thread(self, flask_app,
                                                   background, monkeypatch):
        rec = self._slow_transport(monkeypatch)
        request_thread = threading.current_thread().name
        c = make_client(flask_app)
        c.refresh_csrf()
        c.post("/api/auth/signup",
               {"role": "coach", "name": "Async C",
                "email": "async.c@test.local", "password": ATHLETE_PW})
        assert rec.done.wait(5)
        assert request_thread not in rec.threads

    def test_a_dead_mail_server_does_not_fail_the_request(self, flask_app,
                                                          background,
                                                          monkeypatch):
        rec = self._slow_transport(monkeypatch, fail=True)
        c = make_client(flask_app)
        c.refresh_csrf()
        r = c.post("/api/auth/signup",
                   {"role": "athlete", "name": "Async D",
                    "email": "async.d@test.local", "password": ATHLETE_PW})
        assert r.status_code == 200
        assert rec.done.wait(5)

    def test_verification_still_works_end_to_end_when_mail_is_deferred(
            self, flask_app, background, monkeypatch):
        """The code is issued and COMMITTED in the request; only the SMTP
        conversation is deferred. So the flow completes even while the mail
        job is still in flight."""
        self._slow_transport(monkeypatch)
        c = make_client(flask_app)
        c.refresh_csrf()
        signup = c.post("/api/auth/signup",
                        {"role": "athlete", "name": "Async V",
                         "email": "async.v@test.local",
                         "password": ATHLETE_PW}).get_json()
        code = signup["dev_code"]
        c.refresh_csrf()
        assert c.post("/api/auth/verify",
                      {"email": "async.v@test.local",
                       "code": code}).status_code == 200
        c.refresh_csrf()
        assert c.post("/api/auth/login",
                      {"email": "async.v@test.local",
                       "password": ATHLETE_PW}).status_code == 200

    def test_password_reset_completes_and_revokes_sessions_with_async_mail(
            self, flask_app, background, monkeypatch):
        self._slow_transport(monkeypatch)
        c = make_client(flask_app)
        c.signup("async.pw@test.local", ATHLETE_PW, name="Async P")
        c.login("async.pw@test.local", ATHLETE_PW)
        assert c.get("/api/me").status_code == 200

        victim = make_client(flask_app)
        victim.refresh_csrf()
        forgot = victim.post("/api/auth/forgot",
                             {"email": "async.pw@test.local"}).get_json()
        victim.refresh_csrf()
        r = victim.post("/api/auth/reset",
                        {"email": "async.pw@test.local",
                         "code": forgot["dev_code"],
                         "password": "BrandNewPass!2026"})
        assert r.status_code == 200
        # The revocation is request-side work and must have happened already.
        assert c.get("/api/me").status_code == 401

    def test_forgot_reports_failure_when_the_job_is_refused(
            self, flask_app, restore_dispatcher, monkeypatch):
        """A refused job must not be reported to the user as "code sent"."""
        class Refusing(Dispatcher):
            name = "refusing"

            def submit(self, fn, *a, **kw):
                return False
        c = make_client(flask_app)
        c.signup("async.refuse@test.local", ATHLETE_PW, name="Async F")
        jobs.set_dispatcher(Refusing())
        monkeypatch.setattr("athletix.config.DEV_CODES", False)
        monkeypatch.setattr("athletix.api.auth.DEV_CODES", False)
        c.refresh_csrf()
        r = c.post("/api/auth/forgot", {"email": "async.refuse@test.local"})
        assert r.status_code == 502
        assert r.get_json()["ok"] is False

    def test_rate_limiting_is_unchanged_by_the_job_boundary(self, flask_app,
                                                            background,
                                                            monkeypatch):
        self._slow_transport(monkeypatch)
        c = make_client(flask_app)
        c.signup("async.rl@test.local", ATHLETE_PW, name="Async RL")
        statuses = []
        for _ in range(8):
            c.refresh_csrf()
            statuses.append(c.post("/api/auth/forgot",
                                   {"email": "async.rl@test.local"}).status_code)
        assert 429 in statuses, statuses

    def test_unverified_login_defers_its_resend(self, flask_app, background,
                                                monkeypatch):
        rec = self._slow_transport(monkeypatch)
        c = make_client(flask_app)
        c.refresh_csrf()
        c.post("/api/auth/signup",
               {"role": "athlete", "name": "Async U",
                "email": "async.u@test.local", "password": ATHLETE_PW})
        assert rec.done.wait(5)
        rec.done.clear()
        c.refresh_csrf()
        started = time.time()
        r = c.post("/api/auth/login",
                   {"email": "async.u@test.local", "password": ATHLETE_PW})
        elapsed = time.time() - started
        assert r.status_code == 403 and r.get_json()["need_verify"] is True
        assert elapsed < FAST, "unverified login waited on SMTP"


# ==========================================================================
# The boundary is not bypassable
# ==========================================================================
class TestBoundaryIsEnforced:

    def test_no_service_calls_the_blocking_transport_directly(self):
        """`send_email` is the transport; application code must go through
        `queue_email`. A direct call would put SMTP back on the request
        path."""
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "athletix")
        offenders = []
        for folder in ("services", "api"):
            directory = os.path.join(root, folder)
            for name in os.listdir(directory):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    text = fh.read()
                if "send_email(" in text.replace("queue_email(", ""):
                    offenders.append("%s/%s" % (folder, name))
        assert not offenders, "blocking SMTP call sites: %r" % offenders

    def test_queue_email_goes_through_the_active_dispatcher(self,
                                                            restore_dispatcher,
                                                            monkeypatch):
        seen = []

        class Spy(Dispatcher):
            name = "spy"

            def submit(self, fn, *a, **kw):
                seen.append((fn, a))
                return True
        jobs.set_dispatcher(Spy())
        from athletix import mail
        assert mail.queue_email("x@test.local", "subj", "<b>body</b>") is True
        assert seen and seen[0][1] == ("x@test.local", "subj", "<b>body</b>")

    def test_importing_the_package_starts_no_threads(self):
        """Importing must stay side-effect free (Phase 2.1 finding D1)."""
        before = {t.name for t in threading.enumerate()}
        import importlib
        importlib.reload(__import__("athletix.jobs.dispatcher",
                                    fromlist=["dispatcher"]))
        after = {t.name for t in threading.enumerate()}
        assert not {n for n in after - before if n.startswith("athletix-job-")}

    def test_email_templates_are_unchanged_through_the_boundary(self,
                                                                background,
                                                                monkeypatch):
        rec = Recorder()
        monkeypatch.setattr("athletix.mail.send_email", rec)
        from athletix import mail
        subject, html = mail.email_verify("Asha", "123456")
        mail.queue_email("t@test.local", subject, html)
        assert rec.done.wait(5)
        to, sent_subject, sent_html = rec.calls[0]
        assert to == "t@test.local"
        assert sent_subject == "Verify your AthletixAI email"
        assert "123456" in sent_html and "Asha" in sent_html
