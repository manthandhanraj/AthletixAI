# -*- coding: utf-8 -*-
"""API surface and versioning.

Every API blueprint declares its routes RELATIVELY (`/auth/login`, not
`/api/auth/login`) and is mounted twice:

    /api/...        the Phase 1 paths, kept forever as compatibility aliases
    /api/v1/...     the versioned namespace

Both mounts are the same blueprint object, so both run the same handler, the
same schema, the same authorization and the same service call. There is no
duplicated business logic anywhere in the versioning - a bug fixed in one
namespace is fixed in the other by construction, and a security check cannot
be present in one and missing from the other.

What is deliberately NOT versioned:

    /health, /health/ready   infrastructure probes, not product API
    /, /privacy, /terms,     server-rendered pages and PWA assets
    /manifest.json, /sw.js

Rate limiting, CSRF, body-size limits and the no-store cache policy all key
off the `/api/` prefix, which both mounts share - so no control is bypassable
by picking the other namespace.
"""

from athletix.api import (account, admin, assessments, auth, health, messaging,
                          notifications, pages, profile, ratings, summary)

API_PREFIX = "/api"
API_V1_PREFIX = "/api/v1"

# Product API: mounted under both prefixes.
VERSIONED_BLUEPRINTS = (
    auth.bp,
    profile.bp,
    assessments.bp,
    messaging.bp,
    notifications.bp,
    ratings.bp,
    account.bp,
    admin.bp,
    summary.bp,
)

# Infrastructure and pages: absolute paths, mounted once.
ROOT_BLUEPRINTS = (
    health.bp,
    pages.bp,
)

BLUEPRINTS = ROOT_BLUEPRINTS + VERSIONED_BLUEPRINTS


def register(app):
    for bp in ROOT_BLUEPRINTS:
        app.register_blueprint(bp)
    for bp in VERSIONED_BLUEPRINTS:
        app.register_blueprint(bp, url_prefix=API_PREFIX)
        # Same blueprint, second mount point. Flask requires a distinct
        # registration name, which is the only thing that differs.
        app.register_blueprint(bp, name="v1_%s" % bp.name,
                               url_prefix=API_V1_PREFIX)
