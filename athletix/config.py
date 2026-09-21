# -*- coding: utf-8 -*-
"""Configuration for AthletixAI.

Single source of truth for every environment-derived setting. Values are
resolved once, at import of this module, and exposed as module-level names so
the rest of the package can `from athletix.config import SPORTS` without a
service locator.

Reading the environment is the only thing this module does. It does not touch
the database, provision accounts, or open sockets — that is `bootstrap.py`,
and it runs only when explicitly called. Importing any part of the
application must never mutate anything.

The package is named `athletix` rather than `app` on purpose: `app.py` remains
the WSGI entrypoint (`gunicorn app:app`), and a package named `app` would
shadow it.
"""

import datetime
import mimetypes
import os
import re
import secrets

# Windows' registry often has no entry for these, so Flask would serve them
# as application/octet-stream. Combined with the X-Content-Type-Options:
# nosniff header the app sets, a browser can then refuse to render the hero
# images. Registering the types here fixes it for every static route.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("model/gltf-binary", ".glb")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------
def load_dotenv(base_dir=None):
    """Load a .env file sitting next to the project root into os.environ.

    Deliberately forgiving, because .env files get created by hand on Windows:
    utf-8-sig strips the byte-order mark Notepad adds (without it the first
    key silently becomes "\ufeffOWNER_EMAIL" and is ignored), stray BOM
    characters are removed from every key, an optional "export " prefix is
    accepted, and surrounding quotes are dropped.

    Uses setdefault, so anything already in the environment (a real deployment,
    or a test harness) always wins over the file.
    """
    try:
        here = base_dir or BASE_DIR
        path = os.path.join(here, ".env")
        if not os.path.exists(path):
            # Windows hides extensions, so ".env" is often saved as ".env.txt"
            alt = path + ".txt"
            if os.path.exists(alt):
                path = alt
                print("[WARN] Using .env.txt - rename it to .env")
            else:
                return
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip().lstrip("\ufeff")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lstrip("\ufeff")
                if k.lower().startswith("export "):
                    k = k[7:].strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)
    except Exception as exc:
        print("[WARN] Could not read .env: %s" % exc)


load_dotenv()


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
# Three states, deliberately separate:
#   DEBUG_MODE     - the Werkzeug debugger. Requires FLASK_DEBUG=1 explicitly.
#                    It is a remote-code-execution surface, so "unset" must
#                    never mean "on".
#   IS_PRODUCTION  - an explicit production signal (FLASK_DEBUG=0, which is
#                    what render.yaml sets, or APP_ENV=production). Turns on
#                    Secure cookies, HSTS, and the fail-fast config checks.
#   neither        - a plain local run: no debugger, no HTTPS-only cookies.
_flask_debug = os.environ.get("FLASK_DEBUG", "").strip()
DEBUG_MODE = _flask_debug == "1"
IS_PRODUCTION = (_flask_debug == "0" or
                 os.environ.get("APP_ENV", "").lower() in ("production", "prod"))

# --------------------------------------------------------------------------
# Database location and backend (Phase 2.9)
# --------------------------------------------------------------------------
# Two settings, deliberately separate:
#
#   DB_PATH       the SQLite file. What every deployment uses today.
#   DATABASE_URL  a full connection URL. Recognised, validated and reported
#                 on - but PostgreSQL is NOT implemented yet, and pretending
#                 otherwise would be worse than saying so. Setting a
#                 postgres:// URL makes startup fail with an explicit message
#                 rather than silently writing to a local SQLite file that
#                 nobody is backing up.
#
# Credentials are never hard-coded and never logged: `safe_database_url()`
# redacts the password before any diagnostic prints it.
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "athletixai.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

SQLITE_SCHEMES = ("sqlite", "sqlite3", "file")
POSTGRES_SCHEMES = ("postgres", "postgresql", "postgresql+psycopg",
                    "postgresql+psycopg2")


def _scheme(url):
    return url.split("://", 1)[0].lower() if "://" in url else ""


def db_backend(url=None):
    """The backend named by DATABASE_URL: 'sqlite' or 'postgresql'.

    An empty URL means SQLite at DB_PATH, which is the current deployment.
    """
    url = DATABASE_URL if url is None else url
    if not url:
        return "sqlite"
    scheme = _scheme(url)
    if scheme in POSTGRES_SCHEMES:
        return "postgresql"
    if scheme in SQLITE_SCHEMES:
        return "sqlite"
    raise RuntimeError(
        "Unsupported DATABASE_URL scheme %r. Supported: %s"
        % (scheme, ", ".join(SQLITE_SCHEMES + POSTGRES_SCHEMES)))


def safe_database_url(url=None):
    """The connection URL with any password removed, safe to print or log."""
    url = DATABASE_URL if url is None else url
    if not url or "://" not in url:
        return url or ""
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return "%s://%s:***@%s" % (scheme, user, host)


DB_BACKEND = db_backend()


def ensure_db_dir(path=None):
    """Create the database's parent directory if a host mounts a disk there
    (e.g. Render's /var/data). Safe to call repeatedly."""
    d = os.path.dirname(path or DB_PATH)
    if d and not os.path.exists(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


ensure_db_dir()


# --------------------------------------------------------------------------
# Session signing key
# --------------------------------------------------------------------------
def resolve_secret_key():
    """Resolve the session signing key, failing fast in production.

    A missing SECRET_KEY in production is fatal: falling back to a random
    per-process key means each gunicorn worker signs cookies with a different
    key (sessions break at random) and it hides a real misconfiguration.
    Locally we persist a generated key in a gitignored file so `python app.py`
    just works and sessions survive a restart.
    """
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret and IS_PRODUCTION:
        raise RuntimeError(
            "SECRET_KEY is not set. Refusing to start in production with a "
            "random per-process session key. Generate one with: "
            "python -c \"import secrets;print(secrets.token_hex(32))\"")
    if secret and IS_PRODUCTION and len(secret) < 32:
        raise RuntimeError("SECRET_KEY is too short (need >= 32 characters).")
    if secret:
        return secret

    keyfile = os.path.join(BASE_DIR, ".dev_secret_key")
    try:
        if os.path.exists(keyfile):
            with open(keyfile) as fh:
                secret = fh.read().strip()
        if not secret:
            secret = secrets.token_hex(32)
            with open(keyfile, "w") as fh:
                fh.write(secret)
            try:                       # owner-only where the OS supports it
                os.chmod(keyfile, 0o600)
            except Exception:
                pass
    except Exception:
        secret = secrets.token_hex(32)
    print("[WARN] SECRET_KEY not set - using a local development key from "
          ".dev_secret_key. Set SECRET_KEY before deploying.")
    return secret


SECRET_KEY = resolve_secret_key()


# --------------------------------------------------------------------------
# Password hashing cost
# --------------------------------------------------------------------------
def _bcrypt_rounds():
    """12 rounds is the production setting and the floor.

    BCRYPT_ROUNDS exists so the security test suite can run at a lower cost
    (~1000 hashes at 12 rounds is 18 minutes, which means the suite stops
    being run at all - a slow test suite is a security problem of its own).
    The override is clamped to >= 12 whenever an explicit production signal is
    present, so it can never weaken a real deployment.
    """
    default = 12
    raw = os.environ.get("BCRYPT_ROUNDS", "").strip()
    if not raw:
        return default
    production = (os.environ.get("FLASK_DEBUG", "").strip() == "0" or
                  os.environ.get("APP_ENV", "").lower() in ("production", "prod"))
    try:
        n = int(raw)
    except ValueError:
        return default
    if production:
        return max(default, min(n, 16))
    return max(4, min(n, 16))


BCRYPT_ROUNDS = _bcrypt_rounds()


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "AthletixAI <no-reply@athletix.ai>")
# Email is normally delivered through SMTP. Render's free instances block
# SMTP ports, so the Brevo HTTPS API is also supported for production.
# Leaving EMAIL_PROVIDER unset preserves the existing local-development
# behaviour: SMTP when a host is configured, console-only otherwise.
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "").strip().lower()
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
if not EMAIL_PROVIDER:
    EMAIL_PROVIDER = "smtp" if SMTP_HOST else "dev"
EMAIL_DEV_MODE = EMAIL_PROVIDER == "dev"

# --------------------------------------------------------------------------
# Background job boundary (Phase 2.7)
# --------------------------------------------------------------------------
# Where slow, non-critical work goes so a request handler does not wait on it.
# Empty means "decide from the mail configuration": with no SMTP host there is
# nothing to wait for (sending is a console print), so jobs run inline; with a
# real SMTP host they go to background worker threads, which is the case that
# used to block a gunicorn worker for up to ~40 seconds.
#
# The in-process queue is bounded and NOT durable - see athletix/jobs/. Set
# JOB_BACKEND explicitly to force one or the other.
JOB_BACKEND = os.environ.get("JOB_BACKEND", "").strip().lower()
JOB_WORKERS = max(1, min(8, int(os.environ.get("JOB_WORKERS", "2") or 2)))
JOB_QUEUE_SIZE = max(16, min(4096,
                             int(os.environ.get("JOB_QUEUE_SIZE", "256") or 256)))


# Returning an OTP in the API response is a development convenience that
# would be a critical vulnerability in production (anyone could reset any
# account). It is gated on BOTH "no SMTP configured" AND "not production",
# so an incomplete production deploy cannot start handing out codes.
DEV_CODES = EMAIL_DEV_MODE and not IS_PRODUCTION


# --------------------------------------------------------------------------
# Owner / admin account
# --------------------------------------------------------------------------
# Credentials come from the environment, never from source control. If
# OWNER_PASSWORD is not set, no owner account is created at all.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@athletix.ai").strip().lower()
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "").strip()


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
# PUBLIC_ROLES is the *only* set of roles that /api/auth/signup will ever
# create. 'admin' and 'owner' are provisioned out-of-band and can never be
# requested by a client.
PUBLIC_ROLES = ("athlete", "coach")
ALL_ROLES = ("athlete", "coach", "admin", "owner")
PRIVILEGED_ROLES = ("admin", "owner")


# --------------------------------------------------------------------------
# Security / validation policy
# --------------------------------------------------------------------------
TOKEN_TTL_MINUTES = 15          # OTP / reset link validity
TOKEN_MAX_ATTEMPTS = 5          # wrong OTP guesses before the token dies
RESEND_COOLDOWN_SECONDS = 60    # minimum gap between OTP e-mails
SESSION_IDLE_SECONDS = 12 * 3600   # absolute idle timeout for any session
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^[0-9+\-() ]{7,20}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 72               # bcrypt silently truncates beyond 72 bytes
MAX_JSON_BYTES = 1024 * 1024    # per-request JSON body cap

SESSION_LIFETIME = datetime.timedelta(days=30)
MAX_CONTENT_LENGTH = 8 * 1024 * 1024   # 8 MB (profile photos are base64)


# --------------------------------------------------------------------------
# Domain vocabularies
# --------------------------------------------------------------------------
METRICS = ["speed", "agility", "strength", "stamina", "technique"]

# Kept in sync with SPORTS_DB in templates/index.html - the backend is the
# authority, so an unknown sport is rejected rather than silently stored.
SPORTS = [
    "Football", "Basketball", "Volleyball", "Rugby", "Hockey (Field)",
    "Handball", "Lacrosse", "Sepak Takraw", "Kabaddi", "Cricket", "Baseball",
    "Softball", "Tennis", "Badminton", "Table Tennis", "Boxing", "Wrestling",
    "MMA", "Judo", "Karate", "Taekwondo", "Fencing", "Athletics", "Cycling",
    "Archery", "Shooting", "Golf", "Snooker/Pool", "Billiards",
    "Weightlifting", "Gymnastics", "Parkour", "Climbing", "Skateboarding",
    "Breakdancing",
]
LOCATIONS = ("Urban", "Rural")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg")
VIDEO_SOURCES = ("upload", "live", "camera", "recorded")
PHOTO_MIMES = ("png", "jpeg", "jpg", "gif", "webp")
# Magic bytes, so a photo is validated by content and not by the label the
# client attached to it.
PHOTO_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
               b"RIFF")

# --------------------------------------------------------------------------
# Scoring provider (Phase 2.8)
# --------------------------------------------------------------------------
# Which implementation `athletix/scoring/` uses. Only one exists today:
# "client-submitted", which is the accurate description of a pipeline that
# runs in the browser. The setting exists so a server-side provider can be
# switched on by configuration rather than by editing call sites. An unknown
# name is a hard error - never a silent fallback to a different trust level.
SCORING_PROVIDER = os.environ.get("SCORING_PROVIDER",
                                  "client-submitted").strip()


ALLOWED_CONSENTS = ("analytics", "contact", "leaderboard", "marketing",
                    "research")


# --------------------------------------------------------------------------
# Rate limit policy
# --------------------------------------------------------------------------
# bucket -> (per_subject_limit, per_ip_limit, window_seconds)
#
# Two tiers on purpose. The per-subject limit is tight, because hammering one
# account or one e-mail address is always abuse. The per-IP limit is looser,
# because this product's users genuinely share addresses - a school or academy
# can put a whole squad behind one NAT - but still low enough to make mass
# registration and credential spraying impractical.
RATE_RULES = {
    #                per-subject  per-IP  window
    "login":        (8,           40,     600),
    "signup":       (3,           8,      3600),
    "verify":       (10,          40,     900),
    "resend":       (5,           15,     3600),
    "forgot":       (5,           15,     3600),
    "reset":        (10,          30,     900),
    "email_change": (5,           15,     3600),
    "password":     (10,          30,     900),
    "message":      (30,          90,     3600),
    "rating":       (20,          60,     3600),
    "notify":       (30,          90,     3600),
    "report":       (60,          180,    3600),
    "photo":        (20,          60,     3600),
    "export":       (10,          30,     3600),
}

TRUST_PROXY = bool(os.environ.get("TRUST_PROXY"))
COOKIE_SECURE = IS_PRODUCTION or bool(os.environ.get("COOKIE_SECURE"))
CSP_REPORT_ONLY = bool(os.environ.get("CSP_REPORT_ONLY"))


# --------------------------------------------------------------------------
# Demo seeding
# --------------------------------------------------------------------------
# Never used in production: seeding is skipped entirely unless SEED_DEMO=1,
# and startup_checks() refuses to boot if it is forced on in production.
SEED_PASSWORD = os.environ.get("SEED_PASSWORD", "AthletixDemo!2026")
SEED_DEMO = os.environ.get("SEED_DEMO", "0" if IS_PRODUCTION else "1") == "1"


def flask_settings():
    """The Flask app.config mapping. Identical to the Phase 1 values."""
    return dict(
        SECRET_KEY=SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,     # JavaScript can never read the cookie
        SESSION_COOKIE_SAMESITE="Lax",    # blocks cross-site POST with cookies
        SESSION_COOKIE_SECURE=COOKIE_SECURE,
        SESSION_COOKIE_NAME="athletix_session",
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        JSON_SORT_KEYS=False,
        TRAP_HTTP_EXCEPTIONS=False,
    )
