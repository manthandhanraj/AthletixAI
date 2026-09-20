# -*- coding: utf-8 -*-
"""Explicit startup tasks: demo seeding, owner provisioning, safety checks.

Phase 2.1 finding D1: these used to run as a side effect of `import app`,
which meant importing the application mutated the database and could raise.
They now run only when `create_app()` calls `initialize()`, or when a CLI
invokes them directly. Importing any athletix module does nothing.
"""

import datetime
import json
import os

from athletix.config import (BCRYPT_ROUNDS, COOKIE_SECURE, DEBUG_MODE,
                             DEV_CODES, EMAIL_DEV_MODE, IS_PRODUCTION,
                             OWNER_EMAIL, OWNER_PASSWORD, SEED_DEMO,
                             SEED_PASSWORD)
from athletix.config import DB_PATH
from athletix.database import SCHEMA, connect, init_db, migrate, now_iso
from athletix.security.passwords import check_password, hash_password
from athletix.validation import password_problem


def seed():
    # connect() is the data layer's entry point: it applies the connection
    # tuning, creates the database directory, and refuses a backend this
    # build does not implement. Opening sqlite3 by hand here bypassed all
    # three (Phase 2.9).
    db = connect()
    db.executescript(SCHEMA)
    migrate(db)
    if not SEED_DEMO:
        db.close()
        return
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
    pw = hash_password(SEED_PASSWORD)
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
    print("Database seeded with demo accounts (password: %s)." % SEED_PASSWORD)


def ensure_owner_account():
    """Create or update the owner account from the environment.

    Runs on every start, independently of seed(): seed() only touches an empty
    database, but OWNER_PASSWORD can be set (or changed) at any time. If no
    password is configured, nothing happens and no owner exists — a safe default.
    """
    if not OWNER_PASSWORD:
        print("[INFO] OWNER_PASSWORD not set - owner account unavailable. "
              "Add OWNER_EMAIL and OWNER_PASSWORD to your .env file.")
        return
    problem = password_problem(OWNER_PASSWORD)
    if problem:
        # The owner account is the most privileged identity on the platform.
        # Rather than provisioning it behind a weak password, refuse and say
        # so - loudly, and without printing the password itself.
        print("[FATAL] OWNER_PASSWORD does not meet the password policy: %s"
              % problem)
        print("[FATAL] The owner account was NOT provisioned. Set a stronger "
              "OWNER_PASSWORD and restart.")
        return
    print("[INFO] Owner login enabled for: %s" % OWNER_EMAIL)
    db = connect()
    try:
        db.executescript(SCHEMA)
        migrate(db)
        row = db.execute("SELECT id, pass_hash FROM users WHERE email = ?",
                         (OWNER_EMAIL,)).fetchone()
        if row is None:
            # Remove any older owner account left on a different email, so
            # exactly one owner identity exists at a time. This is the
            # security-conservative choice (no stale privileged account is
            # left behind) but it IS destructive: ON DELETE CASCADE takes
            # that account's reports, messages and notifications with it.
            # Announce it rather than doing it silently, so an operator who
            # only meant to rename the owner sees what happened.
            stale = db.execute(
                "SELECT email FROM users WHERE role = 'owner'").fetchall()
            for s in stale:
                print("[WARN] OWNER_EMAIL changed: removing the previous "
                      "owner account (%s) and any data owned by it."
                      % s["email"])
            db.execute("DELETE FROM users WHERE role = 'owner'")
            db.execute(
                "INSERT INTO users (role,name,email,phone,pass_hash,verified,"
                "created_at) VALUES ('owner',?,?,?,?,1,?)",
                ("Platform Owner", OWNER_EMAIL, "",
                 hash_password(OWNER_PASSWORD), now_iso()))
            print("[INFO] Owner account created for %s" % OWNER_EMAIL)
        elif not check_password(OWNER_PASSWORD, row["pass_hash"]):
            # Password changed in the environment -> update it, and revoke
            # every session that was minted with the old one.
            db.execute("UPDATE users SET pass_hash = ?, role = 'owner', "
                       "verified = 1, sess_epoch = sess_epoch + 1 "
                       "WHERE id = ?",
                       (hash_password(OWNER_PASSWORD), row["id"]))
            print("[INFO] Owner password updated for %s" % OWNER_EMAIL)
        db.commit()
    finally:
        db.close()


def startup_checks(app=None):
    """Refuse to serve traffic with an unsafe configuration; warn about the
    merely inadvisable.

    `app` is the application being built, so the cookie check reads the
    settings that were actually installed. It used to reference a global
    `app` that does not exist in this module, which meant that in production
    - the only branch that reaches it - the check raised NameError instead of
    running. The configuration value is used as the fallback so the function
    still works when called from a CLI with no app.
    """
    fatal, warn = [], []
    if IS_PRODUCTION:
        if DEBUG_MODE:
            fatal.append("FLASK_DEBUG=1 together with a production "
                         "environment. The Werkzeug debugger allows remote "
                         "code execution.")
        cookie_secure = (app.config["SESSION_COOKIE_SECURE"] if app is not None
                         else COOKIE_SECURE)
        if not cookie_secure:
            fatal.append("Session cookies are not marked Secure.")
        if SEED_DEMO:
            fatal.append("SEED_DEMO=1 in production would create accounts "
                         "with a publicly known password.")
        if EMAIL_DEV_MODE:
            warn.append("SMTP is not configured. Verification and password "
                        "reset e-mails cannot be delivered, so new users "
                        "will not be able to activate their accounts. "
                        "(Codes are NOT returned in API responses here - "
                        "that is disabled in production.)")
    if DEBUG_MODE:
        warn.append("Werkzeug debugger is ON. Never expose this instance to "
                    "an untrusted network.")
    if DEV_CODES:
        warn.append("Email dev mode: verification and reset codes are "
                    "printed to the console and returned by the API. Local "
                    "development only.")
    for w in warn:
        print("[WARN] %s" % w)
    if fatal:
        for f in fatal:
            print("[FATAL] %s" % f)
        raise RuntimeError("Unsafe production configuration; refusing to start.")


def initialize(app=None):
    """Run every startup task, in order. Called by create_app().

    Idempotent: safe on an existing database, and safe to call twice.
    """
    init_db()
    seed()
    ensure_owner_account()
    startup_checks(app)
