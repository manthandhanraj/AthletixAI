# -*- coding: utf-8 -*-
"""Shared repository plumbing."""

from athletix.database import get_db


class Repository(object):
    """Base for every repository.

    Deliberately tiny. It resolves the *active* connection on each call
    rather than holding one, so a repository instance is safe to share across
    requests and always participates in whatever `unit_of_work()` is open.

    There is no `commit()` here on purpose: transaction control belongs to the
    caller, never to the data layer.
    """

    @property
    def db(self):
        return get_db()

    def _one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        return self.db.execute(sql, params).fetchall()

    def _exec(self, sql, params=()):
        return self.db.execute(sql, params)
