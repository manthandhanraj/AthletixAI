# -*- coding: utf-8 -*-
"""One-time e-mail tokens (verification, password reset, e-mail change).

Only SHA-256 hashes are persisted: a leaked database backup must not be
replayable into an account takeover. Expiry, single use, and an attempt
counter are all enforced in `consume_token` so no call site can forget them.

Phase 2.9: the SQL moved to `repositories/tokens.py`. What is left here is
policy, which is what this module was always for.
"""

import datetime
import secrets

from athletix.config import (EMAIL_DEV_MODE, RESEND_COOLDOWN_SECONDS,
                             TOKEN_MAX_ATTEMPTS, TOKEN_TTL_MINUTES)
from athletix.database import now_iso
from athletix.repositories import tokens as token_repo
from athletix.unit_of_work import unit_of_work


def sha256(value):
    import hashlib
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def token_cooldown_active(email, purpose):
    """True if an OTP for this address+purpose was issued moments ago.

    Stops /auth/forgot and /auth/resend from being used to mail-bomb a
    victim's inbox (and to burn our sender reputation) even before the
    IP-based rate limiter kicks in.
    """
    row = token_repo.latest(email, purpose)
    if not row or not row["created_at"]:
        return False
    try:
        made = datetime.datetime.fromisoformat(row["created_at"])
    except ValueError:
        return False
    age = (datetime.datetime.now() - made).total_seconds()
    return 0 <= age < RESEND_COOLDOWN_SECONDS


def issue_token(email, purpose, payload=None):
    """Create a fresh OTP + link token. Only the hashes are persisted.

    The plaintext code is returned to the caller so it can be e-mailed, then
    it is gone: the database only ever holds sha256(code), so a stolen
    backup cannot be replayed into an account takeover.
    """
    code = "%06d" % secrets.randbelow(1000000)       # CSPRNG, not random()
    token = secrets.token_urlsafe(32)
    expires = (datetime.datetime.now() +
               datetime.timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat()
    with unit_of_work():
        # Replacing the outstanding token and inserting the new one are one
        # operation: two valid codes must never be in flight at once.
        token_repo.delete_for(email, purpose)
        token_repo.insert(email, purpose, sha256(code), sha256(token),
                          payload, now_iso(), expires)
        # Housekeeping: expired rows never need to be kept.
        token_repo.delete_expired(datetime.datetime.now().isoformat())
    if EMAIL_DEV_MODE:
        print("[EMAIL DEV MODE] %s code for %s: %s" % (purpose, email, code))
    return code, token


def consume_token(email, purpose, code):
    """Validate a one-time OTP. Returns the row on success, else None.

    Enforced here, in one place, for every OTP flow:
      * expiry              - a stale code is deleted and rejected
      * one-time use        - the row is deleted the moment it is accepted
      * attempt limiting    - TOKEN_MAX_ATTEMPTS wrong guesses burns the code,
                              which is what makes a 6-digit OTP safe
      * constant-time compare on the hash
    """
    row = token_repo.latest(email, purpose)
    if not row:
        return None
    if row["expires_at"] < datetime.datetime.now().isoformat():
        with unit_of_work():
            token_repo.delete(row["id"])
        return None
    if not secrets.compare_digest(row["code_hash"], sha256(code)):
        attempts = (row["attempts"] or 0) + 1
        with unit_of_work():
            if attempts >= TOKEN_MAX_ATTEMPTS:
                token_repo.delete(row["id"])
            else:
                token_repo.record_attempt(row["id"], attempts)
        return None
    with unit_of_work():
        token_repo.delete(row["id"])
    return row
