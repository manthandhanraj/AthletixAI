# -*- coding: utf-8 -*-
"""Transaction boundary: one business operation, one atomic transaction.

Phase 2.1 finding B1: `reset_password`, `change_password` and `admin_set_role`
each committed the credential/role change and then called `bump_epoch()`,
which committed *again*. A failure between the two commits left the password
changed but every pre-existing session still valid — defeating the exact
Phase 1 guarantee that session revocation exists to provide.

This module gives that fix somewhere to live.

    with unit_of_work():
        users.update_password(uid, hashed)
        users.bump_session_epoch(uid)
    # one COMMIT here, or one ROLLBACK if anything raised

Design notes
------------
*Re-entrant by depth.* Nested `unit_of_work()` blocks join the outermost one
instead of committing early. This matters because helpers that legitimately
commit when called standalone (`bump_epoch`, `log_activity`) are also called
from inside larger operations; without nesting, they would split the
transaction again and reintroduce B1.

*Only the outermost block commits or rolls back.* An exception in a nested
block propagates outward, so the outermost handler performs a single rollback.
There is no partial commit path.

*sqlite3 semantics.* The driver opens a deferred transaction on the first
mutating statement and holds it until `commit()`. So the guarantee here is not
"we added transactions" — it is "we stopped ending them in the middle of a
business operation".
"""

from contextlib import contextmanager

from flask import g

from athletix.database import get_db

_DEPTH = "_uow_depth"


def in_unit_of_work():
    """True when a transaction boundary is already open on this request.

    Helpers that would otherwise commit on their own check this so they can
    join the surrounding transaction rather than cutting it in half.
    """
    try:
        return getattr(g, _DEPTH, 0) > 0
    except RuntimeError:          # outside an application context
        return False


@contextmanager
def unit_of_work():
    """Atomic transaction boundary. Commits on success, rolls back on error.

    Yields the live connection so callers (and repositories) can execute
    statements. Nested uses join the outer boundary and neither commit nor
    roll back.
    """
    db = get_db()
    depth = getattr(g, _DEPTH, 0)
    g.__setattr__(_DEPTH, depth + 1)
    try:
        yield db
    except BaseException:
        g.__setattr__(_DEPTH, depth)
        if depth == 0:
            # Outermost boundary owns the rollback; nested blocks let the
            # exception travel outward so exactly one rollback happens.
            db.rollback()
        raise
    g.__setattr__(_DEPTH, depth)
    if depth == 0:
        db.commit()
