# -*- coding: utf-8 -*-
"""Rate limiting with a swappable backend.

LIMITATION (documented deliberately): the default backend is an in-process
dictionary, so under `gunicorn --workers N` the effective allowance is ~Nx and
counters reset on deploy. A later phase can drop in a shared backend by
subclassing RateLimiter and overriding `_hits`/`_record`; every call site goes
through `check()`/`hit()`, so nothing else changes.
"""

import time

from flask import jsonify

from athletix.config import RATE_RULES
from athletix.errors import ErrorCode
from athletix.request_info import client_ip


class RateLimiter(object):
    """Fixed-window rate limiter."""

    def __init__(self):
        self._store = {}          # {(bucket, key): [timestamps]}

    def _hits(self, bucket, key, window):
        now = time.time()
        hits = [t for t in self._store.get((bucket, key), [])
                if now - t < window]
        if hits:
            self._store[(bucket, key)] = hits
        else:
            self._store.pop((bucket, key), None)
        return hits

    def _record(self, bucket, key):
        self._store.setdefault((bucket, key), []).append(time.time())
        # Opportunistic cleanup so a long-running process cannot grow forever.
        if len(self._store) > 20000:
            cutoff = time.time() - 3600
            for k in [k for k, v in self._store.items()
                      if not v or v[-1] < cutoff]:
                self._store.pop(k, None)

    def check(self, bucket, key, limit, window):
        """True if the caller is over the limit (request should be refused)."""
        return len(self._hits(bucket, key, window)) >= limit

    def hit(self, bucket, key):
        """Record one attempt against the bucket."""
        self._record(bucket, key)

    def retry_after(self, bucket, key, window):
        hits = self._hits(bucket, key, window)
        if not hits:
            return 0
        return max(1, int(window - (time.time() - hits[0])))

    def reset(self, bucket, key):
        self._store.pop((bucket, key), None)


limiter = RateLimiter()


def too_many(bucket, key=None):
    """Check-and-record. Returns a ready-to-return 429 response, or None.

    Two buckets are always checked, which matters more than it looks:

      * IP + key  - protects one specific target (this account's login, this
                    address's reset codes) from being hammered.
      * IP alone  - protects the *platform*. Keying only on the subject would
                    hand an attacker a fresh allowance for every new value
                    they invent, so "5 signups per hour" would mean unlimited
                    signups as long as each used a different e-mail address.
    """
    key_limit, ip_limit, window = RATE_RULES[bucket]
    ip = client_ip()
    checks = [("%s|" % ip, ip_limit)]
    if key:
        checks.append(("%s|%s" % (ip, str(key).lower()[:120]), key_limit))

    for ident, limit in checks:
        if limiter.check(bucket, ident, limit, window):
            wait = limiter.retry_after(bucket, ident, window)
            resp = jsonify(ok=False,
                           error="Too many attempts. Please try again later.",
                           code=ErrorCode.RATE_LIMITED)
            resp.status_code = 429
            resp.headers["Retry-After"] = str(wait)
            return resp
    for ident, _ in checks:
        limiter.hit(bucket, ident)
    return None
