# -*- coding: utf-8 -*-
"""The asynchronous work boundary.

Request handlers hand slow, non-critical work to `dispatch()` and return.
Today that is exactly one kind of work - transactional e-mail - and the rule
that keeps this boundary honest is:

    Nothing behind this boundary may decide whether the request succeeded.

A verification code is *issued and committed* in the request (so the user can
type it the moment it arrives); only the SMTP conversation is deferred. A
password is *changed and every session revoked* in the request; only the
"your password changed" notice is deferred. If the mail transport is down,
the security-relevant work has still happened.

Selection
---------
The dispatcher is chosen once, by configuration, in `configure()`:

    EMAIL_DEV_MODE (no SMTP host)  -> inline; "sending" is a console print,
                                      there is nothing to wait for, and the
                                      test suite stays deterministic.
    SMTP configured                -> background threads; this is the case
                                      that used to block a worker for ~40s.

`JOB_BACKEND=inline|background` overrides both, which is how the tests drive
the background path deliberately.
"""

from athletix.jobs.dispatcher import (BackgroundDispatcher, Dispatcher,
                                      InlineDispatcher)

__all__ = ["dispatch", "configure", "get_dispatcher", "set_dispatcher",
           "shutdown", "Dispatcher", "InlineDispatcher",
           "BackgroundDispatcher"]

_dispatcher = None


def _default_dispatcher():
    from athletix import config as cfg
    choice = (getattr(cfg, "JOB_BACKEND", "") or "").strip().lower()
    if not choice:
        choice = "inline" if cfg.EMAIL_DEV_MODE else "background"
    if choice == "background":
        return BackgroundDispatcher(workers=cfg.JOB_WORKERS,
                                    queue_size=cfg.JOB_QUEUE_SIZE)
    return InlineDispatcher()


def get_dispatcher():
    """The active dispatcher, built on first use."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _default_dispatcher()
    return _dispatcher


def set_dispatcher(dispatcher):
    """Replace the active dispatcher. Returns the previous one.

    Used by `configure()` and by tests that need to drive a specific
    implementation; there is no other way to swap it, so a stray import
    cannot change how jobs run.
    """
    global _dispatcher
    previous = _dispatcher
    _dispatcher = dispatcher
    return previous


def configure(app=None):
    """Install the configured dispatcher. Called once by `create_app`."""
    dispatcher = _default_dispatcher()
    set_dispatcher(dispatcher)
    if app is not None:
        app.extensions.setdefault("athletix", {})["jobs"] = dispatcher
    return dispatcher


def dispatch(fn, *args, **kwargs):
    """Hand work to the boundary. Returns True when it was accepted.

    Never raises: a request must not fail because a background notice could
    not be scheduled.
    """
    try:
        return get_dispatcher().submit(fn, *args, **kwargs)
    except Exception:                      # pragma: no cover - defensive
        import logging
        logging.getLogger("athletix.jobs").exception("dispatch failed")
        return False


def shutdown(wait=True):
    if _dispatcher is not None:
        _dispatcher.shutdown(wait=wait)
