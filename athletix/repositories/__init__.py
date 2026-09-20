# -*- coding: utf-8 -*-
"""Data access layer.

One repository per aggregate. Repositories know SQL and nothing else:

  * they never open, commit or roll back a transaction — the caller owns the
    boundary via `unit_of_work()`. A repository that commits on its own is how
    a business operation gets split in half, which is exactly the Phase 2.1
    finding B1 that Phase 2.3A fixed;
  * they never make authorization decisions;
  * they never shape an API response — that stays in `serializers.py`, which
    is the Phase 1 privacy boundary.

They return raw `sqlite3.Row` objects. Rows are INTERNAL: every route must
still pass them through the privacy-aware serializers before they reach a
client.
"""

from athletix.repositories.activity import ActivityRepository
from athletix.repositories.messages import MessageRepository
from athletix.repositories.notifications import NotificationRepository
from athletix.repositories.ratings import RatingRepository
from athletix.repositories.reports import ReportRepository
from athletix.repositories.tokens import TokenRepository
from athletix.repositories.users import UserRepository

# Module-level singletons. These hold no state - they resolve the active
# connection per call - so sharing them is safe and keeps call sites terse.
users = UserRepository()
reports = ReportRepository()
messages = MessageRepository()
notifications = NotificationRepository()
ratings = RatingRepository()
activity = ActivityRepository()
tokens = TokenRepository()

__all__ = ["users", "reports", "messages", "notifications", "ratings",
           "activity", "tokens", "UserRepository", "ReportRepository",
           "MessageRepository", "NotificationRepository", "RatingRepository",
           "ActivityRepository", "TokenRepository"]
