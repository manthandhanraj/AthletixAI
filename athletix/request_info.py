# -*- coding: utf-8 -*-
"""Facts derived from the incoming HTTP request.

`client_ip` lives here rather than in the rate limiter because two unrelated
concerns need it - rate limiting and audit logging - and having `audit` import
`security.ratelimit` created a dependency cycle
(errors -> audit -> ratelimit -> errors). Attributing a request to a caller is
its own small concern, so it gets its own module.
"""

from flask import request

from athletix.config import TRUST_PROXY


def client_ip():
    """Best-effort client address.

    Only trusts X-Forwarded-For when TRUST_PROXY is set, because on a direct
    deployment that header is fully attacker-controlled and would let anyone
    reset their own rate-limit bucket by rotating a fake value.
    """
    if TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]
