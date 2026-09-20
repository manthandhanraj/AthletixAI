# -*- coding: utf-8 -*-
"""User, athlete-profile and coach-profile data access."""

import json

from athletix.repositories.base import Repository


class UserRepository(Repository):

    # ── reads ──────────────────────────────────────────────────────────
    def find_by_id(self, user_id):
        return self._one("SELECT * FROM users WHERE id=?", (user_id,))

    def find_by_email(self, email):
        return self._one("SELECT * FROM users WHERE email=?", (email,))

    def find_by_id_and_role(self, user_id, role):
        return self._one("SELECT * FROM users WHERE id=? AND role=?",
                         (user_id, role))

    def exists_email(self, email):
        return self._one("SELECT 1 FROM users WHERE email=?", (email,)) is not None

    def email_taken_by_other(self, email, user_id):
        return self._one("SELECT 1 FROM users WHERE email=? AND id<>?",
                         (email, user_id)) is not None

    def list_all(self, limit=None, offset=0):
        if limit is None:
            return self._all("SELECT * FROM users ORDER BY id")
        return self._all("SELECT * FROM users ORDER BY id LIMIT ? OFFSET ?",
                         (limit, offset))

    def list_by_role(self, role, order="name", limit=None, offset=0):
        column = "name" if order == "name" else "id"
        if limit is None:
            return self._all(
                "SELECT * FROM users WHERE role=? ORDER BY %s, id" % column,
                (role,))
        return self._all(
            "SELECT * FROM users WHERE role=? ORDER BY %s, id LIMIT ? OFFSET ?"
            % column, (role, limit, offset))

    def count(self):
        return self._one("SELECT COUNT(*) FROM users")[0]

    def count_by_role(self, role):
        return self._one("SELECT COUNT(*) FROM users WHERE role=?", (role,))[0]

    def athlete_profile(self, user_id):
        return self._one("SELECT * FROM athlete_profiles WHERE user_id=?",
                         (user_id,))

    def coach_profile(self, user_id):
        return self._one("SELECT * FROM coach_profiles WHERE user_id=?",
                         (user_id,))

    def athlete_sport(self, user_id):
        row = self._one("SELECT sport FROM athlete_profiles WHERE user_id=?",
                        (user_id,))
        return row["sport"] if row else None

    def athlete_profiles_all(self):
        """Every athlete profile, keyed by user id.

        Safe to load wholesale: sport/age/location are directory fields that
        user_public() already exposes to every viewer, so this is exactly the
        set the caller is authorized to see - not a widening of scope.
        """
        return {r["user_id"]: r
                for r in self._all("SELECT * FROM athlete_profiles")}

    def coach_profiles_all(self):
        """Every coach profile, keyed by user id. Same reasoning as above:
        specialty/bio/experience/city/achievements are public directory
        fields."""
        return {r["user_id"]: r
                for r in self._all("SELECT * FROM coach_profiles")}

    def athlete_names(self):
        """{id: name} for every athlete.

        A deliberate projection: the leaderboard needs three names, not three
        full user rows, and a row that is never loaded cannot be leaked.
        """
        return {r["id"]: r["name"] for r in
                self._all("SELECT id, name FROM users WHERE role='athlete'")}

    def consents(self, user_id):
        return self._all("SELECT key,value FROM profile_settings WHERE user_id=?",
                         (user_id,))

    # ── writes (never commit; caller owns the transaction) ─────────────
    def create(self, role, name, email, phone, pass_hash, verified=0,
               photo=None, created_at=None):
        cur = self._exec(
            "INSERT INTO users (role,name,email,phone,pass_hash,photo,verified,"
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (role, name, email, phone, pass_hash, photo, verified, created_at))
        return cur.lastrowid

    def create_athlete_profile(self, user_id, sport, age, location):
        self._exec("INSERT INTO athlete_profiles (user_id,sport,age,location)"
                   " VALUES (?,?,?,?)", (user_id, sport, age, location))

    def create_coach_profile(self, user_id, specialty, bio):
        self._exec("INSERT INTO coach_profiles (user_id,specialty,bio) "
                   "VALUES (?,?,?)", (user_id, specialty, bio))

    def update_name_and_phone(self, user_id, name, phone):
        self._exec("UPDATE users SET name=?, phone=? WHERE id=?",
                   (name, phone, user_id))

    def update_password(self, user_id, pass_hash):
        self._exec("UPDATE users SET pass_hash=? WHERE id=?",
                   (pass_hash, user_id))

    def update_email(self, user_id, email):
        self._exec("UPDATE users SET email=? WHERE id=?", (email, user_id))

    def update_photo(self, user_id, photo):
        self._exec("UPDATE users SET photo=? WHERE id=?", (photo, user_id))

    def update_role(self, user_id, role):
        self._exec("UPDATE users SET role=? WHERE id=?", (role, user_id))

    def mark_verified_by_email(self, email):
        self._exec("UPDATE users SET verified=1 WHERE email=?", (email,))

    def touch_last_login(self, user_id, when):
        self._exec("UPDATE users SET last_login=? WHERE id=?", (when, user_id))

    def bump_session_epoch(self, user_id):
        """Invalidate every session for this user.

        The revocation half of a security operation. It must run inside the
        SAME transaction as the credential/role change it accompanies -
        see athletix/unit_of_work.py.
        """
        self._exec("UPDATE users SET sess_epoch = sess_epoch + 1 WHERE id=?",
                   (user_id,))

    def delete(self, user_id):
        """ON DELETE CASCADE removes profiles, reports, videos, messages,
        notifications, ratings and settings with the row."""
        self._exec("DELETE FROM users WHERE id=?", (user_id,))

    def update_athlete_profile_field(self, user_id, field, value):
        if field not in ("sport", "age", "location"):
            raise ValueError("unknown athlete profile field: %r" % field)
        self._exec("UPDATE athlete_profiles SET %s=? WHERE user_id=?" % field,
                   (value, user_id))

    def update_coach_profile(self, user_id, fields):
        """fields: ordered mapping of column -> value, allowlisted here."""
        allowed = ("specialty", "bio", "experience", "city", "achievements")
        cols, params = [], []
        for col, val in fields.items():
            if col not in allowed:
                raise ValueError("unknown coach profile field: %r" % col)
            cols.append("%s=?" % col)
            params.append(val)
        if not cols:
            return
        params.append(user_id)
        self._exec("UPDATE coach_profiles SET " + ",".join(cols) +
                   " WHERE user_id=?", params)

    def set_consent(self, user_id, key, value):
        self._exec(
            "INSERT INTO profile_settings (user_id,key,value) VALUES (?,?,?) "
            "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
            (user_id, key, value))
