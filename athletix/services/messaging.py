# -*- coding: utf-8 -*-
"""Messaging, notification and rating workflows.

The communication policy lives here because it is a business rule, not a
transport concern - and keeping it in one place is what stops a future
endpoint from quietly bypassing it.
"""

from athletix.audit import log_activity
from athletix.config import PRIVILEGED_ROLES
from athletix.database import now_iso
from athletix.errors import AuthorizationError, NotFoundError, ValidationError
from athletix.repositories import messages, notifications, ratings, users
from athletix.serializers import is_privileged
from athletix.unit_of_work import unit_of_work
from athletix.validation import as_id, as_int, clean


def may_message(sender, recipient):
    """Modelled on what the product offers, not on what an id makes
    technically reachable:

      coach   -> athlete : yes (recruiting is the point of a coach account)
      athlete -> coach   : yes (the coach directory has a Message button)
      admin/owner -> any : yes (operator communication)
      anyone  -> admin/owner : no, unless that operator wrote first
      athlete -> athlete : no  (no product feature, pure harassment surface)
      coach   -> coach   : no
      anyone  -> self    : no

    The "wrote first" exception lets a user reply inside a thread somebody
    else opened, without opening a channel to arbitrary accounts.
    """
    if sender["id"] == recipient["id"]:
        return False
    if sender["role"] in PRIVILEGED_ROLES:
        return True
    if {sender["role"], recipient["role"]} == {"athlete", "coach"}:
        return True
    return messages.has_written_to(recipient["id"], sender["id"])


def send_message(sender, to_id, text):
    to_id = as_id(to_id)
    text = clean(text, 2000)
    if not to_id or not text:
        raise ValidationError("Message cannot be empty.")

    recipient = users.find_by_id(to_id)
    if not recipient:
        raise NotFoundError("Recipient not found.")
    if not may_message(sender, recipient):
        log_activity(sender["id"], "message_blocked", "to=%s" % to_id)
        # 404, not 403: a 403 would confirm which ids belong to operators.
        raise NotFoundError("Recipient not found.")

    # from_id and from_nm come from the session, never from the request, so a
    # message cannot be forged to appear to come from someone else. The
    # message and its notification are one operation.
    with unit_of_work():
        message_id = messages.create(to_id, sender["id"], sender["name"],
                                     text, now_iso())
        notifications.create(
            to_id, clean("New message from " + sender["name"], 200), now_iso())
    log_activity(sender["id"], "message_sent", "to=%s" % to_id)
    return message_id


def push_notification(user, to_id, text):
    """A normal user may only notify themselves; operators may notify anyone.

    Every genuinely cross-user notification the product needs (new message,
    new rating) is generated server-side by the operation that causes it -
    never accepted from a client.
    """
    to_id = as_id(to_id) or user["id"]
    text = clean(text, 300)
    if not text:
        raise ValidationError("Empty notification.")
    if to_id != user["id"] and not is_privileged(user):
        log_activity(user["id"], "notify_blocked", "to=%s" % to_id)
        raise AuthorizationError("You cannot send notifications to other "
                                 "users.")
    if not users.find_by_id(to_id):
        raise NotFoundError("Recipient not found.")
    with unit_of_work():
        notifications.create(to_id, text, now_iso())
    if to_id != user["id"]:
        log_activity(user["id"], "notify_sent", "to=%s" % to_id)


def mark_notifications_read(user):
    with unit_of_work():
        notifications.mark_all_read(user["id"])


def rate_coach(user, coach_id, stars):
    """Athletes only, coach targets only, attribution from the session.

    The composite primary key (coach_id, athlete_id) plus a session-derived
    athlete_id means an upsert can only ever touch the caller's own row.
    """
    if user["role"] != "athlete":
        log_activity(user["id"], "rating_blocked", "role=%s" % user["role"])
        raise AuthorizationError("Only athletes can rate coaches.")
    coach_id = as_id(coach_id)
    stars = as_int(stars, 1, 5)
    if stars is None:
        raise ValidationError("Rating must be between 1 and 5.")
    if not coach_id or not users.find_by_id_and_role(coach_id, "coach"):
        raise NotFoundError("Coach not found.")

    with unit_of_work():
        ratings.upsert(coach_id, user["id"], stars, now_iso())
        # Generated here rather than accepted from the client, so the coach's
        # notification feed cannot be used to deliver arbitrary text.
        notifications.create(
            coach_id,
            clean("%s rated you %d stars." % (user["name"], stars), 200),
            now_iso())
    log_activity(user["id"], "rate_coach",
                 "coach=%s stars=%s" % (coach_id, stars))
