# -*- coding: utf-8 -*-
"""Security audit-log data access."""

from athletix.repositories.base import Repository


class ActivityRepository(Repository):

    def append(self, user_id, action, detail, ip, at, ua):
        self._exec(
            "INSERT INTO activity_logs (user_id, action, detail, ip, at, ua) "
            "VALUES (?,?,?,?,?,?)", (user_id, action, detail, ip, at, ua))

    def count(self):
        return self._one("SELECT COUNT(*) FROM activity_logs")[0]

    def recent(self, limit=200, offset=0):
        return self._all(
            "SELECT a.id, a.user_id, a.action, a.detail, a.ip, a.at, u.name, "
            "u.role FROM activity_logs a LEFT JOIN users u ON u.id = a.user_id "
            "ORDER BY a.id DESC LIMIT ? OFFSET ?", (limit, offset))
