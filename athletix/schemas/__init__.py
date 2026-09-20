# -*- coding: utf-8 -*-
"""Request schemas and response DTOs — the declared edge of the API.

Two halves, deliberately separate:

`fields` + `requests`
    Per-endpoint input schemas. They replace the ad-hoc `d.get(...)`,
    `int(...)` and scattered required-field checks that used to sit in route
    handlers. A schema normalises and shape-checks; the *business* rules stay
    in `athletix/services/`, which is also reachable without HTTP.

`responses`
    The response DTOs. These describe what leaves the process. They do NOT
    make privacy decisions: `athletix/serializers.py` remains the single
    privacy boundary (`user_public`, `report_public`, `scoped_state`), and the
    DTOs below either call it or shape rows that the query itself already
    scoped to the caller.

Nothing here ever serialises a raw database row straight to a client.
"""

from athletix.schemas import requests, responses          # noqa: F401
from athletix.schemas.fields import Schema, ValidationError  # noqa: F401

__all__ = ["requests", "responses", "Schema", "ValidationError"]
