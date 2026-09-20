# -*- coding: utf-8 -*-
"""Consistent pagination for collection endpoints.

Contract (additive, so existing clients keep working):

    {"ok": true, "<collection>": [...], "page": {
        "limit": 100, "offset": 0, "total": 1234, "returned": 100,
        "has_more": true, "next_offset": 100}}

The data key keeps its original name and shape - only the `page` object is
new. A caller that ignores it behaves exactly as before, except that it now
receives a bounded slice instead of an unbounded one.

Offset paging is only correct when the ordering is total, so every paginated
query carries a unique tiebreaker (the primary key) alongside its sort column.
"""

from athletix.validation import as_int

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def page_params(args, default=DEFAULT_LIMIT, maximum=MAX_LIMIT):
    """Parse and clamp ?limit= and ?offset=.

    Hostile or malformed input falls back to the safe default rather than
    erroring: these are read endpoints, and a bad query string should not be
    a 400 when a sane bounded answer exists.
    """
    limit = as_int(args.get("limit"), 1, maximum, default)
    offset = as_int(args.get("offset"), 0, 10 ** 9, 0)
    return limit, offset


def page_meta(total, limit, offset, returned):
    return {
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": returned,
        "has_more": offset + returned < total,
        "next_offset": (offset + returned) if offset + returned < total else None,
    }
