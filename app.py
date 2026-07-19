# -*- coding: utf-8 -*-
"""
AthletixAI - Production Backend
================================
Flask + SQLite backend providing persistent accounts, secure authentication,
email verification, password reset, profile management and write-through
persistence for reports, messages, ratings and notifications.

The frontend (templates/index.html) keeps its existing UI untouched; it now
hydrates its in-memory store from this API and writes every change through.

Run:
    pip install -r requirements.txt
    python app.py            ->  http://127.0.0.1:5000

Email:
    Set SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_FROM environment
    variables to send real email. Without them the server runs in EMAIL DEV
    MODE: codes are printed to the console and returned to the client so the
    full flows remain demoable offline.
"""

import os

# ── Load a local .env file (KEY=VALUE per line) so email/secret settings
#    persist without typing them in the terminal each run. ──
def _load_dotenv():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()
import re
import json
import time
import sqlite3
import secrets
import smtplib
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

from flask import (Flask, g, jsonify, make_response, render_template,
                   send_from_directory,
                   request, session)

# Password hashing: bcrypt preferred (project requirement), werkzeug fallback
try:
    import bcrypt as _bcrypt

    def hash_password(plain: str) -> str:
        return _bcrypt.hashpw(plain.encode("utf-8"),
                              _bcrypt.gensalt(rounds=12)).decode("utf-8")

    def check_password(plain: str, hashed: str) -> bool:
        try:
            return _bcrypt.checkpw(plain.encode("utf-8"),
                                   hashed.encode("utf-8"))
        except ValueError:
            return False
except ImportError:  # pragma: no cover - fallback keeps app runnable
    from werkzeug.security import (check_password_hash,
                                   generate_password_hash)

    def hash_password(plain: str) -> str:
        return generate_password_hash(plain)

    def check_password(plain: str, hashed: str) -> bool:
        return check_password_hash(hashed, plain)


# --------------------------------------------------------------------------
# App configuration
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(BASE_DIR, "athletixai.db"))
# On hosts with a persistent disk (e.g. Render /var/data), set DB_PATH there
# so the database survives restarts and redeploys.
_db_dir = os.path.dirname(DB_PATH)
if _db_dir and not os.path.exists(_db_dir):
    try:
        os.makedirs(_db_dir, exist_ok=True)
    except Exception:
        pass

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("COOKIE_SECURE")),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,  # 8 MB (profile photos are base64)
)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "AthletixAI <no-reply@athletix.ai>")
EMAIL_DEV_MODE = not SMTP_HOST  # no SMTP configured -> dev mode

# ── Owner / admin account ───────────────────────────────────────────────
# Credentials come from the environment, never from source control.
# Set OWNER_EMAIL and OWNER_PASSWORD in your .env (locally) or in your
# host's environment settings (in production). If OWNER_PASSWORD is not
# set, no owner account is created at all — the panel simply stays closed.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@athletix.ai").strip().lower()
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "").strip()

# In production (HTTPS host), harden the session cookie
if os.environ.get("FLASK_DEBUG", "1") == "0":
    app.config.update(SESSION_COOKIE_SECURE=True)

TOKEN_TTL_MINUTES = 15          # OTP / reset link validity
LOGIN_MAX_ATTEMPTS = 8          # per email+ip within window
LOGIN_WINDOW_SECONDS = 600
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^[0-9+\-() ]{7,20}$")
SPORTS = ["Cricket", "Athletics", "Football", "Wrestling", "Kabaddi"]

_login_attempts = {}  # {(email, ip): [timestamps]}


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT    NOT NULL CHECK (role IN ('athlete','coach','admin','owner')),
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    phone         TEXT,
    pass_hash     TEXT    NOT NULL,
    photo         TEXT,                       -- base64 data URL (optional)
    verified      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS athlete_profiles (
    user_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    sport     TEXT,
    age       INTEGER,
    location  TEXT DEFAULT 'Urban'
);

CREATE TABLE IF NOT EXISTS coach_profiles (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    specialty    TEXT DEFAULT '',
    bio          TEXT DEFAULT '',
    experience   INTEGER DEFAULT 0,
    city         TEXT DEFAULT '',
    achievements TEXT DEFAULT '[]'            -- JSON array of strings
);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date       TEXT    NOT NULL,
    sport      TEXT,
    speed      INTEGER, agility INTEGER, strength INTEGER,
    stamina    INTEGER, technique INTEGER,
    overall    REAL,
    live       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reports_athlete
    ON reports(athlete_id, date);

CREATE TABLE IF NOT EXISTS ai_results (
    report_id  INTEGER PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
    potential  TEXT, medal_prob INTEGER, risk TEXT, best_fit TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER REFERENCES reports(id) ON DELETE CASCADE,
    athlete_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename   TEXT, size_mb REAL, source TEXT, uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    to_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    from_nm TEXT NOT NULL,
    body    TEXT NOT NULL,
    date    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_id, date);

CREATE TABLE IF NOT EXISTS ratings (
    coach_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    athlete_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stars      INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (coach_id, athlete_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text    TEXT NOT NULL,
    date    TEXT NOT NULL,
    read    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS activity_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action  TEXT NOT NULL,
    detail  TEXT,
    ip      TEXT,
    at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_tokens (
    email      TEXT NOT NULL COLLATE NOCASE,
    purpose    TEXT NOT NULL,                 -- 'verify' | 'reset' | 'change'
    code       TEXT NOT NULL,                 -- 6-digit OTP
    token      TEXT NOT NULL,                 -- long token (links)
    payload    TEXT,                          -- e.g. new email for 'change'
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens ON email_tokens(email, purpose);
"""


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log_activity(user_id, action, detail=""):
    try:
        get_db().execute(
            "INSERT INTO activity_logs (user_id, action, detail, ip, at) "
            "VALUES (?,?,?,?,?)",
            (user_id, action, detail[:300],
             request.remote_addr if request else "", now_iso()))
        get_db().commit()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Email system (5 professional templates + dev mode)
# --------------------------------------------------------------------------
def _email_shell(title, body_html):
    return """<!DOCTYPE html><html><body style="margin:0;background:#0b0b10;
font-family:Arial,Helvetica,sans-serif;padding:28px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0"
 style="background:#15151c;border:1px solid #2a2a34;border-radius:14px;">
<tr><td style="background:#d90429;border-radius:14px 14px 0 0;padding:18px 28px;">
<span style="color:#ffffff;font-size:20px;font-weight:bold;
letter-spacing:1px;">ATHLETIX<span style="color:#ffd7d7;">AI</span></span></td></tr>
<tr><td style="padding:28px;color:#e8e9ee;font-size:14px;line-height:1.7;">
<h2 style="margin:0 0 12px;color:#ffffff;font-size:19px;">""" + title + """</h2>
""" + body_html + """
</td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #2a2a34;color:#8a8f9b;
font-size:11px;">AthletixAI &middot; AI Sports Talent Assessment Platform<br>
Owned by Archita Tripathi &amp; Manthan Dhanraj &middot; This is an automated
message, please do not reply.</td></tr>
</table></td></tr></table></body></html>"""


def _otp_block(code):
    return ("""<div style="background:#0b0b10;border:1px dashed #d90429;
border-radius:10px;padding:16px;text-align:center;margin:18px 0;">
<span style="font-size:30px;letter-spacing:10px;color:#ff5757;
font-weight:bold;">""" + code + """</span></div>
<p style="color:#8a8f9b;font-size:12px;">This code expires in """
            + str(TOKEN_TTL_MINUTES) + " minutes.</p>")


def email_welcome(name):
    return ("Welcome to AthletixAI", _email_shell(
        "Welcome aboard, " + name + "!",
        "<p>Your AthletixAI account has been created successfully.</p>"
        "<p>Upload your first training video to receive an AI performance "
        "report with Speed, Agility, Strength, Stamina and Technique scores "
        "&mdash; then climb the leaderboard.</p>"
        "<p><b>Discover. Analyze. Elevate. Dominate.</b></p>"))


def email_verify(name, code):
    return ("Verify your AthletixAI email", _email_shell(
        "Verify your email address",
        "<p>Hi " + name + ", use the code below to verify your email and "
        "activate your account:</p>" + _otp_block(code)))


def email_reset(name, code):
    return ("Reset your AthletixAI password", _email_shell(
        "Password reset requested",
        "<p>Hi " + name + ", we received a request to reset your password. "
        "Enter this code to continue:</p>" + _otp_block(code) +
        "<p>If you did not request this, you can safely ignore this email "
        "&mdash; your password will not change.</p>"))


def email_password_changed(name):
    return ("Your AthletixAI password was changed", _email_shell(
        "Password changed",
        "<p>Hi " + name + ", this is a confirmation that your account "
        "password was changed just now.</p>"
        "<p>If this was not you, reset your password immediately from the "
        "login screen.</p>"))


def email_account_created(name, role):
    return ("Your AthletixAI account is ready", _email_shell(
        "Account created",
        "<p>Hi " + name + ", your <b>" + role.title() +
        "</b> account is verified and ready to use.</p>"
        "<p>Log in anytime to analyze sessions, track progress and connect "
        "with the AthletixAI network.</p>"))


def send_email(to_addr, subject, html):
    """Send an email; in dev mode, print to console instead."""
    if EMAIL_DEV_MODE:
        print("\n[EMAIL DEV MODE] To: %s | Subject: %s" % (to_addr, subject))
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = MAIL_FROM
        msg["To"] = to_addr
        msg.attach(MIMEText(html, "html"))
        if SMTP_PORT == 465:
            # SSL (Gmail app-password, most providers)
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(MAIL_FROM, [to_addr], msg.as_string())
        else:
            # STARTTLS (port 587)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls()
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(MAIL_FROM, [to_addr], msg.as_string())
        return True
    except Exception as exc:  # pragma: no cover
        print("[EMAIL ERROR]", exc)
        return False


def issue_token(email, purpose, payload=None):
    """Create a fresh OTP + token pair for the given purpose."""
    db = get_db()
    db.execute("DELETE FROM email_tokens WHERE email=? AND purpose=?",
               (email, purpose))
    code = "%06d" % secrets.randbelow(1000000)
    token = secrets.token_urlsafe(32)
    expires = (datetime.datetime.now() +
               datetime.timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat()
    db.execute(
        "INSERT INTO email_tokens (email,purpose,code,token,payload,"
        "expires_at) VALUES (?,?,?,?,?,?)",
        (email, purpose, code, token, payload, expires))
    db.commit()
    if EMAIL_DEV_MODE:
        print("[EMAIL DEV MODE] %s code for %s: %s" % (purpose, email, code))
    return code, token


def consume_token(email, purpose, code):
    """Validate an OTP; returns payload row or None. Deletes on success."""
    db = get_db()
    row = db.execute(
        "SELECT rowid, * FROM email_tokens WHERE email=? AND purpose=? "
        "AND code=?", (email, purpose, code)).fetchone()
    if not row:
        return None
    if row["expires_at"] < datetime.datetime.now().isoformat():
        db.execute("DELETE FROM email_tokens WHERE rowid=?", (row["rowid"],))
        db.commit()
        return None
    db.execute("DELETE FROM email_tokens WHERE rowid=?", (row["rowid"],))
    db.commit()
    return row
# -*- coding: utf-8 -*-
# ==========================================================================
# PART 2 - validation, serialization, auth middleware, routes
# (concatenated after app_part1.py at build time)
# ==========================================================================

METRICS = ["speed", "agility", "strength", "stamina", "technique"]


def clean(text, limit=300):
    """Basic input hardening: strip, cap length, neutralize angle brackets
    to prevent stored XSS (frontend also encodes on render)."""
    if text is None:
        return ""
    text = str(text).strip()[:limit]
    return text.replace("<", "&lt;").replace(">", "&gt;")


def valid_email(email):
    return bool(EMAIL_RE.match(email or ""))


def valid_password(pw):
    """>= 6 chars (kept lenient so existing demo logins keep working)."""
    return isinstance(pw, str) and len(pw) >= 6


def overall_of(m):
    return round(sum(m[k] for k in METRICS) / 5.0, 1)


def rate_limited(email):
    key = (email.lower(), request.remote_addr or "")
    nowt = time.time()
    hits = [t for t in _login_attempts.get(key, []) if nowt - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[key] = hits
    return len(hits) >= LOGIN_MAX_ATTEMPTS


def record_attempt(email):
    key = (email.lower(), request.remote_addr or "")
    _login_attempts.setdefault(key, []).append(time.time())


# --------------------------------------------------------------------------
# Serializers - shape rows exactly like the frontend's in-memory objects
# --------------------------------------------------------------------------
def user_public(row):
    db = get_db()
    data = {
        "id": row["id"], "role": row["role"], "name": row["name"],
        "email": row["email"], "phone": row["phone"] or "",
        "photo": row["photo"] or "", "verified": bool(row["verified"]),
        "created_at": row["created_at"], "last_login": row["last_login"],
    }
    if row["role"] == "athlete":
        p = db.execute("SELECT * FROM athlete_profiles WHERE user_id=?",
                       (row["id"],)).fetchone()
        if p:
            data.update(sport=p["sport"], age=p["age"], location=p["location"])
    elif row["role"] == "coach":
        p = db.execute("SELECT * FROM coach_profiles WHERE user_id=?",
                       (row["id"],)).fetchone()
        if p:
            data.update(specialty=p["specialty"], bio=p["bio"],
                        experience=p["experience"], city=p["city"],
                        achievements=json.loads(p["achievements"] or "[]"))
    return data


def report_public(row):
    db = get_db()
    ai = db.execute("SELECT * FROM ai_results WHERE report_id=?",
                    (row["id"],)).fetchone()
    return {
        "id": row["id"], "athleteId": row["athlete_id"], "date": row["date"],
        "sport": row["sport"], "overall": row["overall"],
        "live": bool(row["live"]),
        "m": {k: row[k] for k in METRICS},
        "ai": ({"potential": ai["potential"], "medal": ai["medal_prob"],
                "risk": ai["risk"], "best": ai["best_fit"]} if ai else None),
    }


def full_state():
    """Return the entire dataset the frontend needs to hydrate its store."""
    db = get_db()
    users = [user_public(r) for r in
             db.execute("SELECT * FROM users ORDER BY id").fetchall()]
    reports = [report_public(r) for r in
               db.execute("SELECT * FROM reports ORDER BY date").fetchall()]
    messages = [{"id": r["id"], "toId": r["to_id"], "fromId": r["from_id"],
                 "from": r["from_nm"], "text": r["body"], "date": r["date"]}
                for r in db.execute(
                    "SELECT * FROM messages ORDER BY date").fetchall()]
    ratings = [{"coachId": r["coach_id"], "byId": r["athlete_id"],
                "stars": r["stars"]} for r in
               db.execute("SELECT * FROM ratings").fetchall()]
    notifs = [{"id": r["id"], "toId": r["user_id"], "text": r["text"],
               "date": r["date"], "read": bool(r["read"])} for r in
              db.execute("SELECT * FROM notifications ORDER BY date").fetchall()]
    return {"users": users, "reports": reports, "messages": messages,
            "ratings": ratings, "notifs": notifs}


# --------------------------------------------------------------------------
# Auth middleware
# --------------------------------------------------------------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return jsonify(ok=False, error="Authentication required"), 401
        return fn(*a, **kw)
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


# --------------------------------------------------------------------------
# CSRF protection (double-submit cookie)
# --------------------------------------------------------------------------
@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if request.path.startswith("/api/auth/"):
            return  # login/signup bootstrap the token; exempt
        sent = request.headers.get("X-CSRF-Token", "")
        if not sent or sent != session.get("csrf"):
            return jsonify(ok=False, error="Invalid CSRF token"), 403


def ensure_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.route("/")
def home():
    ensure_csrf()
    return render_template("index.html")


@app.get("/privacy")
def privacy_page():
    """Public privacy policy (DPDP-aligned)."""
    return render_template("privacy.html")


@app.get("/terms")
def terms_page():
    """Public terms of service."""
    return render_template("terms.html")


@app.get("/manifest.json")
def manifest():
    """PWA manifest served from root so the app is installable."""
    return send_from_directory("static", "manifest.json",
                               mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    """Service worker must be served from root to control the whole scope."""
    resp = make_response(send_from_directory("static", "sw.js",
                                             mimetype="application/javascript"))
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/public/stats")
def public_stats():
    """Real platform numbers for the landing page (no login needed).

    These are read live from the database - nothing here is hard-coded,
    so the landing page can never show a number we cannot back up.
    """
    db = get_db()
    athletes = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='athlete'").fetchone()[0]
    coaches = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='coach'").fetchone()[0]
    reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    sessions_live = db.execute(
        "SELECT COUNT(*) FROM reports WHERE live=1").fetchone()[0]
    return jsonify(ok=True, athletes=athletes, coaches=coaches,
                   reports=reports, live_sessions=sessions_live,
                   sports=35)


@app.get("/api/csrf")
def api_csrf():
    return jsonify(ok=True, token=ensure_csrf())


# --------------------------------------------------------------------------
# AUTH: signup, verify, login, logout, session
# --------------------------------------------------------------------------
@app.post("/api/auth/signup")
def signup():
    d = request.get_json(force=True, silent=True) or {}
    role = d.get("role")
    name = clean(d.get("name"), 80)
    email = (d.get("email") or "").strip().lower()
    phone = clean(d.get("phone"), 20)
    password = d.get("password") or ""

    if role not in ("athlete", "coach", "admin"):
        return jsonify(ok=False, error="Please select a valid role."), 400
    if not name:
        return jsonify(ok=False, error="Please enter your full name."), 400
    if not valid_email(email):
        return jsonify(ok=False, error="Please enter a valid email address."), 400
    if phone and not PHONE_RE.match(phone):
        return jsonify(ok=False, error="Please enter a valid phone number."), 400
    if not valid_password(password):
        return jsonify(ok=False,
                       error="Password must be at least 6 characters."), 400

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return jsonify(ok=False,
                       error="This email is already registered. Please log in."), 409

    cur = db.execute(
        "INSERT INTO users (role,name,email,phone,pass_hash,photo,verified,"
        "created_at) VALUES (?,?,?,?,?,?,1,?)",
        (role, name, email, phone, hash_password(password),
         clean(d.get("photo"), 500000) or None, now_iso()))
    uid = cur.lastrowid
    if role == "athlete":
        db.execute("INSERT INTO athlete_profiles (user_id,sport,age,location)"
                   " VALUES (?,?,?,?)",
                   (uid, d.get("sport") or "Athletics",
                    int(d.get("age") or 16), "Urban"))
    elif role == "coach":
        db.execute("INSERT INTO coach_profiles (user_id,specialty,bio) "
                   "VALUES (?,?,?)",
                   (uid, clean(d.get("specialty"), 120),
                    clean(d.get("bio"), 400)))
    db.commit()

    code, _ = issue_token(email, "verify")
    subject, html = email_verify(name, code)
    send_email(email, subject, html)
    log_activity(uid, "signup", role)

    resp = {"ok": True, "message":
            "Account created. Please verify your email to continue.",
            "email": email}
    if EMAIL_DEV_MODE:
        resp["dev_code"] = code  # surfaced only when SMTP not configured
    return jsonify(resp)


@app.post("/api/auth/verify")
def verify_email():
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    code = clean(d.get("code"), 6)
    if not consume_token(email, "verify", code):
        return jsonify(ok=False,
                       error="Invalid or expired verification code."), 400
    db = get_db()
    db.execute("UPDATE users SET verified=1 WHERE email=?", (email,))
    db.commit()
    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        s, h = email_account_created(row["name"], row["role"])
        send_email(email, s, h)
        s2, h2 = email_welcome(row["name"])
        send_email(email, s2, h2)
        log_activity(row["id"], "verify_email")
    return jsonify(ok=True, message="Email verified successfully. You can now log in.")


@app.post("/api/auth/resend")
def resend_verification():
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    row = get_db().execute("SELECT * FROM users WHERE email=?",
                           (email,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No account found for this email."), 404
    if row["verified"]:
        return jsonify(ok=True, message="This email is already verified.")
    code, _ = issue_token(email, "verify")
    s, h = email_verify(row["name"], code)
    send_email(email, s, h)
    resp = {"ok": True, "message": "Verification email sent."}
    if EMAIL_DEV_MODE:
        resp["dev_code"] = code
    return jsonify(resp)


@app.post("/api/auth/login")
def login():
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    role = d.get("role")
    remember = bool(d.get("remember"))

    if not valid_email(email):
        return jsonify(ok=False, error="Please enter a valid email address."), 400
    if rate_limited(email):
        return jsonify(ok=False,
                       error="Too many attempts. Please try again later."), 429

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password(password, row["pass_hash"]):
        record_attempt(email)
        log_activity(row["id"] if row else None, "login_failed", email)
        return jsonify(ok=False, error="Invalid email or password."), 401
    if role and row["role"] != role and row["role"] != "owner":
        return jsonify(ok=False, error="This account is registered as a "
                       + row["role"] + ". Please select the correct role."), 403
    if not row["verified"]:
        # Auto-verify on login (email verification is optional in this build)
        get_db().execute("UPDATE users SET verified=1 WHERE id=?", (row["id"],))
        get_db().commit()

    session.clear()
    session["uid"] = row["id"]
    session["csrf"] = secrets.token_urlsafe(24)
    session.permanent = remember
    db.execute("UPDATE users SET last_login=? WHERE id=?",
               (now_iso(), row["id"]))
    db.commit()
    log_activity(row["id"], "login")
    return jsonify(ok=True, user=user_public(row), csrf=session["csrf"])


@app.post("/api/auth/logout")
def logout():
    u = current_user()
    if u:
        log_activity(u["id"], "logout")
    session.clear()
    return jsonify(ok=True)


@app.get("/api/session")
def get_session():
    u = current_user()
    if not u:
        return jsonify(ok=True, user=None, csrf=ensure_csrf())
    return jsonify(ok=True, user=user_public(u), csrf=ensure_csrf())


@app.get("/api/state")
@login_required
def api_state():
    return jsonify(ok=True, state=full_state())


# --------------------------------------------------------------------------
# FORGOT / RESET PASSWORD
# --------------------------------------------------------------------------
@app.post("/api/auth/forgot")
def forgot_password():
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    row = get_db().execute("SELECT * FROM users WHERE email=?",
                           (email,)).fetchone()
    # Do not reveal whether the email exists (avoids enumeration)
    if row:
        code, _ = issue_token(email, "reset")
        s, h = email_reset(row["name"], code)
        send_email(email, s, h)
        if EMAIL_DEV_MODE:
            return jsonify(ok=True, message="If the email exists, a reset "
                           "code has been sent.", dev_code=code)
    return jsonify(ok=True,
                   message="If the email exists, a reset code has been sent.")


@app.post("/api/auth/reset")
def reset_password():
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    code = clean(d.get("code"), 6)
    new_pw = d.get("password") or ""
    if not valid_password(new_pw):
        return jsonify(ok=False,
                       error="Password must be at least 6 characters."), 400
    if not consume_token(email, "reset", code):
        return jsonify(ok=False, error="Invalid or expired reset code."), 400
    db = get_db()
    db.execute("UPDATE users SET pass_hash=? WHERE email=?",
               (hash_password(new_pw), email))
    db.commit()
    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        s, h = email_password_changed(row["name"])
        send_email(email, s, h)
        log_activity(row["id"], "password_reset")
    return jsonify(ok=True,
                   message="Password reset successfully. You can now log in.")
# -*- coding: utf-8 -*-
# ==========================================================================
# PART 3 - profile management, data write-through APIs, seed, boot
# ==========================================================================

# --------------------------------------------------------------------------
# PROFILE MANAGEMENT
# --------------------------------------------------------------------------
@app.post("/api/profile/update")
@login_required
def profile_update():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    db = get_db()
    name = clean(d.get("name"), 80) or u["name"]
    phone = clean(d.get("phone"), 20)
    if phone and not PHONE_RE.match(phone):
        return jsonify(ok=False, error="Please enter a valid phone number."), 400
    db.execute("UPDATE users SET name=?, phone=? WHERE id=?",
               (name, phone, u["id"]))
    if u["role"] == "athlete":
        sport = d.get("sport")
        age = d.get("age")
        if sport in SPORTS:
            db.execute("UPDATE athlete_profiles SET sport=? WHERE user_id=?",
                       (sport, u["id"]))
        if age:
            db.execute("UPDATE athlete_profiles SET age=? WHERE user_id=?",
                       (int(age), u["id"]))
    db.commit()
    log_activity(u["id"], "profile_update")
    return jsonify(ok=True, message="Profile updated successfully.",
                   user=user_public(current_user()))


@app.post("/api/profile/photo")
@login_required
def profile_photo():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    photo = d.get("photo") or ""
    if photo and not photo.startswith("data:image/"):
        return jsonify(ok=False, error="Invalid image format."), 400
    if len(photo) > 500000:
        return jsonify(ok=False, error="Image is too large (max ~350 KB)."), 400
    get_db().execute("UPDATE users SET photo=? WHERE id=?",
                     (photo or None, u["id"]))
    get_db().commit()
    log_activity(u["id"], "profile_photo")
    return jsonify(ok=True, message="Profile photo updated.")


@app.post("/api/profile/password")
@login_required
def change_password():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    if not check_password(d.get("current") or "", u["pass_hash"]):
        return jsonify(ok=False, error="Current password is incorrect."), 400
    new_pw = d.get("password") or ""
    if not valid_password(new_pw):
        return jsonify(ok=False,
                       error="New password must be at least 6 characters."), 400
    get_db().execute("UPDATE users SET pass_hash=? WHERE id=?",
                     (hash_password(new_pw), u["id"]))
    get_db().commit()
    s, h = email_password_changed(u["name"])
    send_email(u["email"], s, h)
    log_activity(u["id"], "password_change")
    return jsonify(ok=True, message="Password changed successfully.")


@app.post("/api/profile/email/request")
@login_required
def request_email_change():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    new_email = (d.get("email") or "").strip().lower()
    if not valid_email(new_email):
        return jsonify(ok=False, error="Please enter a valid email address."), 400
    if get_db().execute("SELECT 1 FROM users WHERE email=?",
                        (new_email,)).fetchone():
        return jsonify(ok=False, error="This email is already in use."), 409
    code, _ = issue_token(new_email, "change", payload=str(u["id"]))
    s, h = email_verify(u["name"], code)
    send_email(new_email, s, h)
    resp = {"ok": True, "message": "Verification code sent to the new email."}
    if EMAIL_DEV_MODE:
        resp["dev_code"] = code
    return jsonify(resp)


@app.post("/api/profile/email/confirm")
@login_required
def confirm_email_change():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    new_email = (d.get("email") or "").strip().lower()
    code = clean(d.get("code"), 6)
    row = consume_token(new_email, "change", code)
    if not row or row["payload"] != str(u["id"]):
        return jsonify(ok=False, error="Invalid or expired code."), 400
    get_db().execute("UPDATE users SET email=? WHERE id=?",
                     (new_email, u["id"]))
    get_db().commit()
    log_activity(u["id"], "email_change", new_email)
    return jsonify(ok=True, message="Email updated successfully.",
                   user=user_public(current_user()))


@app.post("/api/profile/delete")
@login_required
def delete_account():
    u = current_user()
    if not check_password(d_pw(request), u["pass_hash"]):
        return jsonify(ok=False, error="Password confirmation failed."), 400
    get_db().execute("DELETE FROM users WHERE id=?", (u["id"],))
    get_db().commit()
    log_activity(None, "account_deleted", u["email"])
    session.clear()
    return jsonify(ok=True, message="Your account has been deleted.")


def d_pw(req):
    return (req.get_json(force=True, silent=True) or {}).get("password") or ""


# --------------------------------------------------------------------------
# REPORTS (persist AI analysis results)
# --------------------------------------------------------------------------
@app.post("/api/reports")
@login_required
def add_report():
    u = current_user()
    if u["role"] != "athlete":
        return jsonify(ok=False, error="Only athletes can create reports."), 403
    d = request.get_json(force=True, silent=True) or {}
    m = d.get("m") or {}
    try:
        metrics = {k: max(0, min(100, int(m[k]))) for k in METRICS}
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="Invalid metric values."), 400
    db = get_db()
    prof = db.execute("SELECT sport FROM athlete_profiles WHERE user_id=?",
                      (u["id"],)).fetchone()
    sport = (prof["sport"] if prof else None) or "Athletics"
    ov = overall_of(metrics)
    cur = db.execute(
        "INSERT INTO reports (athlete_id,date,sport,speed,agility,strength,"
        "stamina,technique,overall,live) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (u["id"], now_iso(), sport, metrics["speed"], metrics["agility"],
         metrics["strength"], metrics["stamina"], metrics["technique"], ov,
         1 if d.get("live") else 0))
    rid = cur.lastrowid
    ai = d.get("ai") or {}
    if ai:
        db.execute("INSERT INTO ai_results (report_id,potential,medal_prob,"
                   "risk,best_fit) VALUES (?,?,?,?,?)",
                   (rid, ai.get("potential"), ai.get("medal"),
                    ai.get("risk"), ai.get("best")))
    vinfo = d.get("video")
    if vinfo:
        db.execute("INSERT INTO videos (report_id,athlete_id,filename,size_mb,"
                   "source,uploaded_at) VALUES (?,?,?,?,?,?)",
                   (rid, u["id"], clean(vinfo.get("name"), 200),
                    vinfo.get("size", 0), vinfo.get("source", "upload"),
                    now_iso()))
    db.commit()
    log_activity(u["id"], "report_created", "overall=%s" % ov)
    row = db.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    return jsonify(ok=True, report=report_public(row))


# --------------------------------------------------------------------------
# MESSAGES
# --------------------------------------------------------------------------
@app.post("/api/messages")
@login_required
def send_message():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    to_id = d.get("toId")
    body = clean(d.get("text"), 2000)
    if not to_id or not body:
        return jsonify(ok=False, error="Message cannot be empty."), 400
    if not get_db().execute("SELECT 1 FROM users WHERE id=?",
                            (to_id,)).fetchone():
        return jsonify(ok=False, error="Recipient not found."), 404
    cur = get_db().execute(
        "INSERT INTO messages (to_id,from_id,from_nm,body,date) "
        "VALUES (?,?,?,?,?)", (to_id, u["id"], u["name"], body, now_iso()))
    get_db().execute("INSERT INTO notifications (user_id,text,date) "
                     "VALUES (?,?,?)",
                     (to_id, "New message from " + u["name"], now_iso()))
    get_db().commit()
    log_activity(u["id"], "message_sent", "to=%s" % to_id)
    return jsonify(ok=True, id=cur.lastrowid, message="Message sent.")


# --------------------------------------------------------------------------
# RATINGS
# --------------------------------------------------------------------------
@app.post("/api/ratings")
@login_required
def rate_coach():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    coach_id = d.get("coachId")
    try:
        stars = int(d.get("stars"))
    except (ValueError, TypeError):
        return jsonify(ok=False, error="Invalid rating."), 400
    if stars < 1 or stars > 5:
        return jsonify(ok=False, error="Rating must be between 1 and 5."), 400
    coach = get_db().execute("SELECT 1 FROM users WHERE id=? AND role='coach'",
                             (coach_id,)).fetchone()
    if not coach:
        return jsonify(ok=False, error="Coach not found."), 404
    get_db().execute(
        "INSERT INTO ratings (coach_id,athlete_id,stars,updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(coach_id,athlete_id) DO UPDATE SET "
        "stars=excluded.stars, updated_at=excluded.updated_at",
        (coach_id, u["id"], stars, now_iso()))
    get_db().commit()
    log_activity(u["id"], "rate_coach", "coach=%s stars=%s" % (coach_id, stars))
    return jsonify(ok=True, message="Rating submitted.")


# --------------------------------------------------------------------------
# NOTIFICATIONS
# --------------------------------------------------------------------------
@app.post("/api/notifications")
@login_required
def push_notification():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    to_id = d.get("toId") or u["id"]
    text = clean(d.get("text"), 300)
    if not text:
        return jsonify(ok=False, error="Empty notification."), 400
    get_db().execute("INSERT INTO notifications (user_id,text,date) "
                     "VALUES (?,?,?)", (to_id, text, now_iso()))
    get_db().commit()
    return jsonify(ok=True)


@app.post("/api/notifications/read")
@login_required
def read_notifications():
    u = current_user()
    get_db().execute("UPDATE notifications SET read=1 WHERE user_id=?",
                     (u["id"],))
    get_db().commit()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# COACH PROFILE (specialty, bio, achievements)
# --------------------------------------------------------------------------
@app.post("/api/coach/profile")
@login_required
def coach_profile():
    u = current_user()
    if u["role"] != "coach":
        return jsonify(ok=False, error="Coaches only."), 403
    d = request.get_json(force=True, silent=True) or {}
    ach = d.get("achievements")
    fields, params = [], []
    if "specialty" in d:
        fields.append("specialty=?"); params.append(clean(d["specialty"], 120))
    if "bio" in d:
        fields.append("bio=?"); params.append(clean(d["bio"], 400))
    if "experience" in d:
        fields.append("experience=?"); params.append(int(d.get("experience") or 0))
    if "city" in d:
        fields.append("city=?"); params.append(clean(d["city"], 80))
    if isinstance(ach, list):
        fields.append("achievements=?")
        params.append(json.dumps([clean(a, 160) for a in ach][:20]))
    if fields:
        params.append(u["id"])
        get_db().execute("UPDATE coach_profiles SET " + ",".join(fields) +
                         " WHERE user_id=?", params)
        get_db().commit()
    log_activity(u["id"], "coach_profile_update")
    return jsonify(ok=True, message="Profile saved.",
                   user=user_public(current_user()))


@app.post("/api/consent")
@login_required
def set_consent():
    u = current_user()
    d = request.get_json(force=True, silent=True) or {}
    key = clean(d.get("key"), 40)
    val = "1" if d.get("value") else "0"
    if not key:
        return jsonify(ok=False, error="Invalid consent key."), 400
    get_db().execute(
        "INSERT INTO profile_settings (user_id,key,value) VALUES (?,?,?) "
        "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
        (u["id"], "consent_" + key, val))
    get_db().commit()
    return jsonify(ok=True)


@app.get("/api/export")
@login_required
def export_data():
    """Privacy: export all of the user's own data as JSON."""
    u = current_user()
    db = get_db()
    data = {"account": user_public(u),
            "reports": [report_public(r) for r in db.execute(
                "SELECT * FROM reports WHERE athlete_id=?",
                (u["id"],)).fetchall()],
            "messages_received": [dict(r) for r in db.execute(
                "SELECT from_nm,body,date FROM messages WHERE to_id=?",
                (u["id"],)).fetchall()]}
    log_activity(u["id"], "data_export")
    resp = make_response(json.dumps(data, indent=2))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = \
        "attachment; filename=athletixai_my_data.json"
    return resp


# --------------------------------------------------------------------------
# ADMIN
# --------------------------------------------------------------------------
@app.get("/api/admin/metrics")
@login_required
def admin_metrics():
    u = current_user()
    if u["role"] != "admin":
        return jsonify(ok=False, error="Admins only."), 403
    db = get_db()

    def count(q):
        return db.execute(q).fetchone()[0]
    return jsonify(ok=True, metrics={
        "users": count("SELECT COUNT(*) FROM users"),
        "athletes": count("SELECT COUNT(*) FROM users WHERE role='athlete'"),
        "coaches": count("SELECT COUNT(*) FROM users WHERE role='coach'"),
        "reports": count("SELECT COUNT(*) FROM reports"),
        "messages": count("SELECT COUNT(*) FROM messages"),
    })


# --------------------------------------------------------------------------
# Database seed - demo accounts + sample data (runs once)
# --------------------------------------------------------------------------
def seed():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        db.close()
        return

    import random

    def clamp(v):
        return max(40, min(99, int(v)))

    roster = [
        ("Arjun Singh", 17, "Cricket", "arjun@athletix.ai", "Rural"),
        ("Priya Sharma", 16, "Athletics", "priya.sharma@athletix.ai", "Urban"),
        ("Ravi Kumar", 15, "Football", "ravi.kumar@athletix.ai", "Rural"),
        ("Anjali Patel", 18, "Wrestling", "anjali.patel@athletix.ai", "Urban"),
        ("Deepak Yadav", 14, "Kabaddi", "deepak.yadav@athletix.ai", "Rural"),
        ("Sneha Gupta", 17, "Athletics", "sneha.gupta@athletix.ai", "Urban"),
        ("Manish Tiwari", 16, "Cricket", "manish.tiwari@athletix.ai", "Rural"),
        ("Kavita Rajput", 15, "Football", "kavita.rajput@athletix.ai", "Urban"),
        ("Suresh Mehra", 19, "Wrestling", "suresh.mehra@athletix.ai", "Rural"),
        ("Pooja Nair", 16, "Kabaddi", "pooja.nair@athletix.ai", "Urban"),
        ("Vikram Chauhan", 18, "Cricket", "vikram.chauhan@athletix.ai", "Rural"),
        ("Divya Mishra", 17, "Athletics", "divya.mishra@athletix.ai", "Urban"),
    ]
    drift = [1.6, 2.2, -1.2, 0.8, 1.9, 2.4, -0.6, 1.1, 0.4, 1.7, -1.5, 2.6]
    pw = hash_password("1234")
    metric_keys = ["speed", "agility", "strength", "stamina", "technique"]
    for idx, (name, age, sport, email, loc) in enumerate(roster):
        cur = db.execute(
            "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
            "created_at) VALUES ('athlete',?,?,?,?,1,?)",
            (name, email, "+91 90000 000%02d" % idx, pw, now_iso()))
        uid = cur.lastrowid
        db.execute("INSERT INTO athlete_profiles (user_id,sport,age,location)"
                   " VALUES (?,?,?,?)", (uid, sport, age, loc))
        m = {k: random.randint(58, 80) for k in metric_keys}
        for k in range(6):
            date = (datetime.datetime.now() -
                    datetime.timedelta(days=k * 3, hours=random.randint(0, 10)))
            for key in metric_keys:
                m[key] = clamp(m[key] + drift[idx] + random.randint(-3, 3))
            ov = round(sum(m.values()) / 5.0, 1)
            db.execute(
                "INSERT INTO reports (athlete_id,date,sport,speed,agility,"
                "strength,stamina,technique,overall) VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, date.isoformat(timespec="seconds"), sport, m["speed"],
                 m["agility"], m["strength"], m["stamina"], m["technique"], ov))
    # Coach + admin
    cur = db.execute(
        "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
        "created_at) VALUES ('coach',?,?,?,?,1,?)",
        ("Coach Verma", "coach@athletix.ai", "+91 90000 11111", pw, now_iso()))
    db.execute("INSERT INTO coach_profiles (user_id,specialty,bio,experience,"
               "city,achievements) VALUES (?,?,?,?,?,?)",
               (cur.lastrowid, "Sprint & Strength Conditioning",
                "National-level athletics coach dedicated to finding and "
                "building India's next generation of athletes.", 12, "Delhi",
                json.dumps(["Produced 3 national-level sprinters",
                            "NIS certified coach",
                            "15+ district champions trained"])))
    # two more coaches (parity with the demo roster)
    for nm, em, spec, bio, exp, city, ach in [
        ("Coach Meera Iyer", "meera@athletix.ai", "Athletics & Sprints",
         "Sprint specialist. Speed is a skill - I teach it.", 9, "Pune",
         ["Asian Junior Athletics - Silver (2015)",
          "Produced 3 national-level sprinters",
          "World Athletics Level-2 Sprints Coach"]),
        ("Coach Rajesh Khanna", "rajesh@athletix.ai", "Cricket",
         "Former Ranji player. Technique first, everything else follows.", 14,
         "Mumbai",
         ["Ranji Trophy player (2008-14)", "U-19 State team Head Coach",
          "BCCI Level-B Certified"]),
    ]:
        cur2 = db.execute(
            "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
            "created_at) VALUES ('coach',?,?,?,?,1,?)",
            (nm, em, "", pw, now_iso()))
        db.execute("INSERT INTO coach_profiles (user_id,specialty,bio,"
                   "experience,city,achievements) VALUES (?,?,?,?,?,?)",
                   (cur2.lastrowid, spec, bio, exp, city, json.dumps(ach)))
    db.execute(
        "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
        "created_at) VALUES ('admin',?,?,?,?,1,?)",
        ("Platform Admin", "admin@athletix.ai", "", pw, now_iso()))
    # NOTE: the owner account is NOT created here. seed() only runs on an
    # empty database, but the owner may be configured at any time — see
    # ensure_owner_account() below, which runs on every start.
    db.commit()
    db.close()
    print("Database seeded with demo accounts (password: 1234).")


# Seed the database at import time so it also runs under a production
# server (gunicorn), not just when this file is executed directly.

def ensure_owner_account():
    """Create or update the owner account from the environment.

    Runs on every start, independently of seed(): seed() only touches an empty
    database, but OWNER_PASSWORD can be set (or changed) at any time. If no
    password is configured, nothing happens and no owner exists — a safe default.
    """
    if not OWNER_PASSWORD:
        print("[INFO] OWNER_PASSWORD not set - owner account unavailable.")
        return
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA)
        row = db.execute("SELECT id, pass_hash FROM users WHERE email = ?",
                         (OWNER_EMAIL,)).fetchone()
        if row is None:
            # Remove any older owner account left on a different email.
            db.execute("DELETE FROM users WHERE role = 'owner'")
            db.execute(
                "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
                "created_at) VALUES ('owner',?,?,?,?,1,?)",
                ("Platform Owner", OWNER_EMAIL, "",
                 hash_password(OWNER_PASSWORD), now_iso()))
            print("[INFO] Owner account created for %s" % OWNER_EMAIL)
        elif not check_password(OWNER_PASSWORD, row["pass_hash"]):
            # Password changed in the environment -> update it.
            db.execute("UPDATE users SET pass_hash = ?, role = 'owner', "
                       "verified = 1 WHERE id = ?",
                       (hash_password(OWNER_PASSWORD), row["id"]))
            print("[INFO] Owner password updated for %s" % OWNER_EMAIL)
        db.commit()
    finally:
        db.close()


seed()
ensure_owner_account()


if __name__ == "__main__":
    print("=" * 60)
    print(" AthletixAI backend running -> http://127.0.0.1:5000")
    print(" Email mode:", "DEV (codes in console)" if EMAIL_DEV_MODE
          else "SMTP (%s)" % SMTP_HOST)
    print(" Demo: arjun@athletix.ai / 1234  |  coach@athletix.ai / 1234")
    print("=" * 60)
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"   # set FLASK_DEBUG=0 in production
    port = int(os.environ.get("PORT", "5000"))          # hosts inject PORT
    app.run(debug=debug, host="0.0.0.0", port=port)