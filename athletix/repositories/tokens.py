# -*- coding: utf-8 -*-
"""One-time e-mail token data access (verification, reset, address change).

Phase 2.9 moved this SQL out of `athletix/security/tokens.py`. Two reasons,
and the second is the important one:

1. The security module now holds policy - expiry, single use, attempt
   limiting, constant-time comparison - and no SQL, which is the same
   separation every other part of the application already has.

2. It was the last place in the codebase that used SQLite's implicit
   `rowid`. PostgreSQL has no equivalent, so the table now carries an
   explicit surrogate key and every statement addresses it by `id`.

Rows here are 15-minute one-time codes and only their SHA-256 hashes are
stored, so this table is the one place in the schema where a rebuild costs
nothing but a re-request.
"""

from athletix.repositories.base import Repository


class TokenRepository(Repository):

    def latest(self, email, purpose):
        """The most recent token for this address and purpose, or None."""
        return self._one(
            "SELECT * FROM email_tokens WHERE email=? AND purpose=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (email, purpose))

    def insert(self, email, purpose, code_hash, token_hash, payload,
               created_at, expires_at):
        cur = self._exec(
            "INSERT INTO email_tokens (email,purpose,code_hash,token_hash,"
            "payload,attempts,created_at,expires_at) VALUES (?,?,?,?,?,0,?,?)",
            (email, purpose, code_hash, token_hash, payload, created_at,
             expires_at))
        return cur.lastrowid

    def delete_for(self, email, purpose):
        """Drop any outstanding token, so issuing a new code invalidates the
        old one rather than leaving two valid codes in flight."""
        self._exec("DELETE FROM email_tokens WHERE email=? AND purpose=?",
                   (email, purpose))

    def delete(self, token_id):
        self._exec("DELETE FROM email_tokens WHERE id=?", (token_id,))

    def delete_expired(self, now):
        self._exec("DELETE FROM email_tokens WHERE expires_at < ?", (now,))

    def record_attempt(self, token_id, attempts):
        self._exec("UPDATE email_tokens SET attempts=? WHERE id=?",
                   (attempts, token_id))

    def count(self):
        return self._one("SELECT COUNT(*) FROM email_tokens")[0]
