# -*- coding: utf-8 -*-
"""Job dispatchers: where work goes when a request must not wait for it.

Phase 2.1 finding A3: `send_email()` runs inside request handlers. Its SMTP
client has a 20 second timeout and tries two ports, so one unreachable mail
host blocks a gunicorn worker for up to ~40 seconds - on signup, on login of
an unverified account, on password reset. With a small worker count that is a
denial of service that any attacker can trigger, and that a bad afternoon at
the mail provider can trigger by accident.

This module is the boundary that fixes it. Two implementations, one
interface:

`InlineDispatcher`
    Runs the job immediately on the calling thread and reports its real
    result. This is the correct choice when there is nothing to wait for -
    with no SMTP host configured, sending an e-mail is a console print - and
    it is what keeps the test suite deterministic.

`BackgroundDispatcher`
    Hands the job to a small pool of daemon worker threads and returns as
    soon as it is queued. SMTP is network I/O, so the workers release the GIL
    while they wait; this is not "fake async" - it is the right shape for
    blocking I/O in a synchronous WSGI process.

What this deliberately is NOT
-----------------------------
It is not Celery, RQ or a Redis-backed queue, because the project does not
have (and this phase must not invent) worker infrastructure. The honest
limits of the in-process design are:

  * queued jobs live in ONE process. A deploy, a crash or a SIGKILL loses
    whatever had not been sent yet;
  * there is no retry-with-backoff and no dead-letter queue;
  * the queue is bounded, and a saturated queue REFUSES work rather than
    growing without limit - `submit()` returns False and the caller is told
    the truth, which is why /auth/forgot can still answer 502 instead of
    promising a code that will never arrive.

Every call site goes through `submit()`, so replacing this with a durable
broker later is a one-class change - see PHASE2_6_TO_2_9_FINAL.md for the
deployment requirement that would come with it.
"""

import atexit
import logging
import queue
import threading

log = logging.getLogger("athletix.jobs")

DEFAULT_WORKERS = 2
DEFAULT_QUEUE_SIZE = 256
SHUTDOWN_GRACE_SECONDS = 5.0


class Dispatcher(object):
    """Interface. `submit` never raises and never propagates a job's error."""

    name = "base"

    def submit(self, fn, *args, **kwargs):
        """Accept a unit of work. Returns True when it was accepted.

        The meaning of True differs by implementation, and call sites are
        written against the weaker of the two guarantees:

          inline      - the job ran to completion and reported success
          background  - the job was queued for delivery

        It NEVER means "the e-mail arrived": nothing can promise that
        synchronously, and pretending otherwise is how a user ends up
        waiting for a code that was never going to come.
        """
        raise NotImplementedError

    def shutdown(self, wait=True):
        """Stop accepting work and, optionally, drain what is queued."""

    def pending(self):
        return 0


class InlineDispatcher(Dispatcher):
    """Run the job now, on this thread, and report its real result."""

    name = "inline"

    def submit(self, fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception:
            log.exception("inline job failed: %s", getattr(fn, "__name__", fn))
            return False
        # A job that returns nothing is treated as having succeeded; one that
        # returns a value (send_email returns a bool) is believed.
        return True if result is None else bool(result)


class BackgroundDispatcher(Dispatcher):
    """A bounded queue drained by daemon worker threads."""

    name = "background"

    def __init__(self, workers=DEFAULT_WORKERS, queue_size=DEFAULT_QUEUE_SIZE):
        self._queue = queue.Queue(maxsize=queue_size)
        self._threads = []
        self._started = False
        self._stopping = False
        self._lock = threading.Lock()
        self._workers = max(1, workers)

    # ── lifecycle ─────────────────────────────────────────────────────
    def _ensure_started(self):
        """Threads are started on first use, not at import.

        Importing this package must stay free of side effects (Phase 2.1
        finding D1), and a process that never sends an e-mail should never
        spawn a worker.
        """
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            for i in range(self._workers):
                t = threading.Thread(target=self._run,
                                     name="athletix-job-%d" % i, daemon=True)
                t.start()
                self._threads.append(t)
            self._started = True
            atexit.register(self.shutdown)

    def _run(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:              # shutdown sentinel
                    return
                fn, args, kwargs = item
                try:
                    fn(*args, **kwargs)
                except Exception:
                    # A failed job must never take the worker down with it:
                    # one bad address would otherwise stop all mail.
                    log.exception("background job failed: %s",
                                  getattr(fn, "__name__", fn))
            finally:
                self._queue.task_done()

    # ── interface ─────────────────────────────────────────────────────
    def submit(self, fn, *args, **kwargs):
        if self._stopping:
            return False
        self._ensure_started()
        try:
            self._queue.put_nowait((fn, args, kwargs))
        except queue.Full:
            # Refuse rather than grow. The caller can then tell the user the
            # truth instead of silently dropping their reset code.
            log.error("job queue is full; refused %s",
                      getattr(fn, "__name__", fn))
            return False
        return True

    def pending(self):
        return self._queue.qsize()

    def shutdown(self, wait=True):
        if not self._started or self._stopping:
            self._stopping = True
            return
        self._stopping = True
        deadline = SHUTDOWN_GRACE_SECONDS
        if wait:
            # Give in-flight mail a short chance to finish. Daemon threads
            # mean we never block a shutdown indefinitely.
            drained = threading.Thread(target=self._queue.join, daemon=True)
            drained.start()
            drained.join(deadline)
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
