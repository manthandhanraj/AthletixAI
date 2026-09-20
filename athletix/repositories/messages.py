# -*- coding: utf-8 -*-
"""Direct message data access."""

from athletix.repositories.base import Repository


class MessageRepository(Repository):

    def list_for_user(self, user_id, limit=None, offset=0):
        if limit is None:
            return self._all(
                "SELECT * FROM messages WHERE to_id=? OR from_id=? "
                "ORDER BY date, id", (user_id, user_id))
        return self._all(
            "SELECT * FROM messages WHERE to_id=? OR from_id=? "
            "ORDER BY date, id LIMIT ? OFFSET ?",
            (user_id, user_id, limit, offset))

    def count_for_user(self, user_id):
        return self._one("SELECT COUNT(*) FROM messages WHERE to_id=? "
                         "OR from_id=?", (user_id, user_id))[0]

    def count_conversation(self, user_id, other_id):
        return self._one(
            "SELECT COUNT(*) FROM messages WHERE (to_id=? AND from_id=?) "
            "OR (to_id=? AND from_id=?)",
            (user_id, other_id, other_id, user_id))[0]

    def list_all(self):
        return self._all("SELECT * FROM messages ORDER BY date")

    def conversation(self, user_id, other_id, limit=None, offset=0):
        if limit is None:
            return self._all(
                "SELECT * FROM messages WHERE (to_id=? AND from_id=?) "
                "OR (to_id=? AND from_id=?) ORDER BY date, id",
                (user_id, other_id, other_id, user_id))
        return self._all(
            "SELECT * FROM messages WHERE (to_id=? AND from_id=?) "
            "OR (to_id=? AND from_id=?) ORDER BY date, id LIMIT ? OFFSET ?",
            (user_id, other_id, other_id, user_id, limit, offset))

    def received_by(self, user_id):
        return self._all(
            "SELECT from_nm,body,date FROM messages WHERE to_id=? ORDER BY date",
            (user_id,))

    def sent_by(self, user_id):
        return self._all(
            "SELECT to_id,body,date FROM messages WHERE from_id=? ORDER BY date",
            (user_id,))

    def has_written_to(self, sender_id, recipient_id):
        """Used by the messaging policy to allow replies inside an existing
        conversation. Data access only - the policy itself stays in the
        messaging module."""
        return self._one(
            "SELECT 1 FROM messages WHERE from_id=? AND to_id=? LIMIT 1",
            (sender_id, recipient_id)) is not None

    def count(self):
        return self._one("SELECT COUNT(*) FROM messages")[0]

    def sent_counts(self):
        """{from_id: messages sent}. One GROUP BY for the operator table that
        used to count by scanning every message in the browser."""
        return {r["from_id"]: r["n"] for r in self._all(
            "SELECT from_id, COUNT(*) AS n FROM messages "
            "WHERE from_id IS NOT NULL GROUP BY from_id")}

    def parties_active_since(self, since):
        """Ids on either end of a message sent since `since`."""
        ids = set()
        for r in self._all("SELECT to_id, from_id FROM messages WHERE date >= ?",
                           (since,)):
            ids.add(r["to_id"])
            if r["from_id"]:
                ids.add(r["from_id"])
        return ids

    def recent_with_names(self, limit=8):
        """Newest messages with the recipient's name, for the operator feed."""
        return self._all(
            "SELECT m.id, m.date, m.from_nm, u.name AS to_name FROM messages m "
            "LEFT JOIN users u ON u.id = m.to_id "
            "ORDER BY m.date DESC, m.id DESC LIMIT ?", (limit,))

    def create(self, to_id, from_id, from_name, body, date):
        cur = self._exec(
            "INSERT INTO messages (to_id,from_id,from_nm,body,date) "
            "VALUES (?,?,?,?,?)", (to_id, from_id, from_name, body, date))
        return cur.lastrowid
