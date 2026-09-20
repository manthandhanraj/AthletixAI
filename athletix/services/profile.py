# -*- coding: utf-8 -*-
"""Self-service profile workflows.

Every operation here is scoped to the caller's own account: the user row is
passed in from the session, never looked up from a client-supplied id, so
none of these can be aimed at another account.
"""

from athletix.audit import log_activity
from athletix.config import ALLOWED_CONSENTS, LOCATIONS, PHONE_RE, SPORTS
from athletix.errors import AuthorizationError, ValidationError
from athletix.repositories import users
from athletix.security.passwords import check_password
from athletix.security.session import bump_epoch
from athletix.config import OWNER_EMAIL
from athletix.mail import email_verify, queue_email
from athletix.security.tokens import (consume_token, issue_token,
                                      token_cooldown_active)
from athletix.unit_of_work import unit_of_work
from athletix.validation import as_int, clean, valid_email, validate_photo


def update_profile(user, data):
    """Update name/phone and, for athletes, the sport/age/location fields."""
    name = clean(data.get("name"), 80) or user["name"]
    phone = clean(data.get("phone"), 20)
    if phone and not PHONE_RE.match(phone):
        raise ValidationError("Please enter a valid phone number.")

    # Validate everything BEFORE opening the transaction, so a bad field
    # cannot leave a half-updated profile behind.
    sport, age, loc = data.get("sport"), None, data.get("location")
    if user["role"] == "athlete":
        if sport is not None and sport not in SPORTS:
            raise ValidationError("Unknown sport.")
        if data.get("age") is not None:
            age = as_int(data.get("age"), 5, 100)
            if age is None:
                raise ValidationError("Please enter a valid age.")
        if loc is not None and loc not in LOCATIONS:
            raise ValidationError("Unknown location.")

    with unit_of_work():
        users.update_name_and_phone(user["id"], name, phone)
        if user["role"] == "athlete":
            if sport is not None:
                users.update_athlete_profile_field(user["id"], "sport", sport)
            if age is not None:
                users.update_athlete_profile_field(user["id"], "age", age)
            if loc is not None:
                users.update_athlete_profile_field(user["id"], "location", loc)
    log_activity(user["id"], "profile_update")


def update_photo(user, photo):
    """Validated by content, not by the label the client attached."""
    if not isinstance(photo, str):
        raise ValidationError("Invalid image format.")
    if photo:
        problem = validate_photo(photo)
        if problem:
            raise ValidationError(problem)
    with unit_of_work():
        users.update_photo(user["id"], photo or None)
    log_activity(user["id"], "profile_photo")


def request_email_change(user, new_email, password):
    """Start an address change. Returns the OTP, or None when nothing was
    sent - the caller cannot tell an unavailable address from a throttled
    one, so this is not an enumeration oracle."""
    if not valid_email(new_email):
        raise ValidationError("Please enter a valid email address.")
    if new_email == OWNER_EMAIL:
        raise AuthorizationError("This email cannot be used.")
    # Requires the account password, so a hijacked tab cannot silently move
    # the account to an address the attacker controls.
    if not check_password(password or "", user["pass_hash"]):
        raise ValidationError("Password confirmation failed.")
    if users.exists_email(new_email) or token_cooldown_active(new_email, "change"):
        return None
    code, _ = issue_token(new_email, "change", payload=str(user["id"]))
    s, h = email_verify(user["name"], code)
    queue_email(new_email, s, h)
    log_activity(user["id"], "email_change_requested")
    return code


def confirm_email_change(user, new_email, code):
    """Apply a verified address change and revoke sessions atomically."""
    if not valid_email(new_email) or len(code) != 6:
        raise ValidationError("Invalid or expired code.")
    row = consume_token(new_email, "change", code)
    # The token is bound to the user who requested it: someone else's valid
    # code cannot be replayed to move *their* pending address onto my account.
    if not row or row["payload"] != str(user["id"]):
        raise ValidationError("Invalid or expired code.")
    if users.email_taken_by_other(new_email, user["id"]):
        raise ValidationError("Invalid or expired code.")

    with unit_of_work():
        users.update_email(user["id"], new_email)
        bump_epoch(user["id"])
    log_activity(user["id"], "email_change")
    return users.find_by_id(user["id"])


def set_consent(user, key, value):
    key = clean(key, 40)
    if key not in ALLOWED_CONSENTS:
        raise ValidationError("Invalid consent key.")
    val = "1" if value else "0"
    with unit_of_work():
        users.set_consent(user["id"], "consent_" + key, val)
    log_activity(user["id"], "consent_change", "%s=%s" % (key, val))


def delete_account(user, password):
    if not check_password(password or "", user["pass_hash"]):
        log_activity(user["id"], "account_delete_failed")
        raise ValidationError("Password confirmation failed.")
    if user["role"] == "owner":
        raise AuthorizationError("The owner account cannot be deleted here.")
    # ON DELETE CASCADE removes profiles, reports, videos, messages,
    # notifications, ratings and settings with the row.
    with unit_of_work():
        users.delete(user["id"])
    log_activity(None, "account_deleted", "user_id=%s" % user["id"])
