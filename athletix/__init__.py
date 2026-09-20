# -*- coding: utf-8 -*-
"""AthletixAI application package.

`create_app()` is the single construction point. Importing this package has
no side effects: no database is opened, no account is provisioned, no network
call is made. Startup work happens only when `create_app()` runs, and can be
skipped entirely with `initialize=False` for unit tests that do not need a
database.

Phase 2.1 finding D1 — importing the old `app` module ran `_load_dotenv()`,
`seed()`, `ensure_owner_account()` and `startup_checks()`, which meant a bare
import mutated the database and could raise. That is fixed here.
"""

import os

from flask import Flask

__all__ = ["create_app"]


def create_app(config_overrides=None, initialize=True):
    """Build the Flask application.

    config_overrides : dict merged into app.config after the defaults (used by
                       tests to flip TESTING on, and by future config profiles).
    initialize       : run startup tasks (schema, migrations, seed, owner
                       bootstrap, safety checks). Set False for tests that
                       want an app object without touching a database.
    """
    from athletix import config as cfg

    app = Flask(
        __name__,
        template_folder=os.path.join(cfg.BASE_DIR, "templates"),
        static_folder=os.path.join(cfg.BASE_DIR, "static"),
    )
    app.config.update(cfg.flask_settings())
    if config_overrides:
        app.config.update(config_overrides)

    _register_request_hooks(app)
    _register_blueprints(app)

    # The asynchronous work boundary (Phase 2.7). Installing it here - not at
    # import - keeps `import athletix` free of side effects: no worker thread
    # exists until an application is actually built.
    from athletix import jobs
    jobs.configure(app)

    from athletix.errors import register_error_handlers
    register_error_handlers(app)

    from athletix.database import close_db
    app.teardown_appcontext(close_db)

    if initialize:
        from athletix.bootstrap import initialize as run_startup
        run_startup(app)

    return app


def _register_request_hooks(app):
    """Order matters: body size is checked before CSRF, and CSRF before any
    handler runs. Response headers are applied on the way out."""
    from athletix.security.http import (csrf_protect, limit_body_size,
                                        security_headers)
    app.before_request(limit_body_size)
    app.before_request(csrf_protect)
    app.after_request(security_headers)


def _register_blueprints(app):
    """Mount the API surface.

    Route paths are unchanged from Phase 1 — the frontend contract is stable.
    Blueprints are an internal organisation change only; `/api/v1` versioning
    is prepared for a later sub-phase and deliberately not introduced here.
    """
    from athletix import api
    api.register(app)
