# -*- coding: utf-8 -*-
"""Notification data access."""

from athletix.repositories.base import Repository


class NotificationRepository(Repository):

    def list_for_user(self, user_id, limit=None, offset=0):
        if limit is None:
            return self._all(
                "SELECT * FROM notifications WHERE user_id=? ORDER BY date, id",
                (user_id,))
        return self._all(
            "SELECT * FROM notifications WHERE user_id=? "
            "ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset))

    def count_for_user(self, user_id):
        return self._one("SELECT COUNT(*) FROM notifications WHERE user_id=?",
                         (user_id,))[0]

    def count(self):
        return self._one("SELECT COUNT(*) FROM notifications")[0]

    def unread_count(self, user_id):
        return self._one("SELECT COUNT(*) FROM notifications WHERE user_id=? "
                         "AND read=0", (user_id,))[0]

    def list_all(self):
        return self._all("SELECT * FROM notifications ORDER BY date")

    def create(self, user_id, text, date):
        self._exec("INSERT INTO notifications (user_id,text,date) "
                   "VALUES (?,?,?)", (user_id, text, date))

    def mark_all_read(self, user_id):
        self._exec("UPDATE notifications SET read=1 WHERE user_id=?", (user_id,))
