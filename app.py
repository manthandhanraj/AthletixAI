# -*- coding: utf-8 -*-
"""AthletixAI - WSGI entrypoint.

The application itself lives in the `athletix` package. This module is the
deployment surface and nothing more:

    gunicorn app:app          production (Procfile, render.yaml)
    python app.py             local development

Phase 2.2 moved ~2,800 lines of configuration, security, serialization, data
access and routing out of here and into `athletix/`. What remains is wiring.

Why the package is called `athletix` and not `app`: a package named `app`
would shadow this module and break `gunicorn app:app`. Keeping this file as
the entrypoint means the Procfile, render.yaml and every deployment doc
continue to work untouched.

The names re-exported below are this module's public surface. They exist so
tooling and the test-suite can reach the application without depending on the
internal package layout; each is forwarded to its real home.
"""

import os

from athletix import create_app
from athletix.audit import log_activity
from athletix.config import (ALL_ROLES, BCRYPT_ROUNDS, DB_PATH, DEBUG_MODE,
                             DEV_CODES, EMAIL_DEV_MODE, IS_PRODUCTION,
                             LOCATIONS, MAX_JSON_BYTES, METRICS, OWNER_EMAIL,
                             PASSWORD_MAX, PASSWORD_MIN, PHOTO_MIMES,
                             PRIVILEGED_ROLES, PUBLIC_ROLES, RATE_RULES,
                             RESEND_COOLDOWN_SECONDS, SEED_DEMO,
                             SEED_PASSWORD, SESSION_IDLE_SECONDS, SMTP_HOST,
                             SPORTS, TOKEN_MAX_ATTEMPTS, TOKEN_TTL_MINUTES,
                             VIDEO_EXTS, VIDEO_SOURCES, _bcrypt_rounds)
from athletix.database import get_db, init_db, migrate, now_iso
from athletix.mail import queue_email, send_email
from athletix.security.http import CSP, CSP_HEADER, SAFE_METHODS
from athletix.security.passwords import check_password, hash_password
from athletix.request_info import client_ip
from athletix.security.ratelimit import RateLimiter, limiter, too_many
from athletix.security.session import (bump_epoch, current_user,
                                       login_required, roles_required,
                                       start_session)
from athletix.security.tokens import (consume_token, issue_token, sha256,
                                      token_cooldown_active)
from athletix.serializers import (can_view_report_detail, is_privileged,
                                  leaderboard_leaders, report_public,
                                  scoped_state, user_public)
from athletix.validation import (as_id, as_int, body, clean, overall_of,
                                 password_problem, safe_filename, valid_email,
                                 valid_password, validate_photo)

__all__ = ["app", "create_app"]

# The WSGI application. Constructing it runs the explicit startup sequence
# (schema, migrations, demo seed, owner bootstrap, safety checks) - see
# athletix.bootstrap.initialize.
app = create_app()


if __name__ == "__main__":
    print("=" * 60)
    print(" AthletixAI backend running -> http://127.0.0.1:5000")
    print(" Email mode:", "DEV (codes in console)" if EMAIL_DEV_MODE
          else "SMTP (%s)" % SMTP_HOST)
    if SEED_DEMO:
        print(" Demo logins: arjun@athletix.ai / coach@athletix.ai")
        print(" Demo password:", SEED_PASSWORD)
    print("=" * 60)
    # Debug is OFF unless explicitly asked for. The Werkzeug debugger is a
    # remote-code-execution surface, so it must never be the default.
    port = int(os.environ.get("PORT", "5000"))          # hosts inject PORT
    host = os.environ.get("HOST", "127.0.0.1" if DEBUG_MODE else "0.0.0.0")
    app.run(debug=DEBUG_MODE, host=host, port=port)
