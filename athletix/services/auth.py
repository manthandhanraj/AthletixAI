# -*- coding: utf-8 -*-
"""Authentication and credential workflows.

Owns the business rules; the route owns HTTP. Services raise typed
`AppError`s rather than building responses, so the same rule produces the
same status and message wherever it is called from.

Session cookies and rate limiting stay in the route layer on purpose: both
are HTTP concerns, and a service that reaches for `flask.session` stops being
testable in isolation. The one exception is `bump_epoch`, which is a database
write (the revocation half of a credential change) and must share the
caller's transaction.
"""

from athletix.audit import log_activity
from athletix.config import (ALL_ROLES, OWNER_EMAIL, PHONE_RE,
                             PRIVILEGED_ROLES, PUBLIC_ROLES, SPORTS)
from athletix.database import now_iso
from athletix.errors import (AuthenticationError, AuthorizationError,
                             ConflictError, ValidationError)
from athletix.mail import (email_account_created, email_password_changed,
                           email_reset, email_verify, email_welcome,
                           queue_email)
from athletix.repositories import users
from athletix.security.passwords import check_password, hash_password
from athletix.security.session import bump_epoch
from athletix.security.tokens import (consume_token, issue_token,
                                      token_cooldown_active)
from athletix.unit_of_work import unit_of_work
from athletix.validation import as_int, clean, password_problem, valid_email


class NeedsVerification(Exception):
    """Raised when a correct credential belongs to an unverified account.

    Not an AppError: the route answers 403 with a `need_verify` flag and an
    optional dev code, which is a response shape the generic handler does not
    model.
    """

    def __init__(self, email, dev_code=None):
        super(NeedsVerification, self).__init__(email)
        self.email = email
        self.dev_code = dev_code


def register(data):
    """Create an unverified account plus its role profile, and issue the
    verification code. Returns (user_id, email, code)."""
    role = data.get("role")
    name = clean(data.get("name"), 80)
    email = (data.get("email") or "").strip().lower()[:254]
    phone = clean(data.get("phone"), 20)
    password = data.get("password") or ""

    # Public signup can ONLY ever create a public role. This is the single
    # point where a role is chosen from client input, and it is an allowlist:
    # 'admin' and 'owner' are provisioned out-of-band and can never be
    # requested by a client.
    if role not in PUBLIC_ROLES:
        log_activity(None, "signup_role_rejected", str(role)[:40])
        raise ValidationError("Please select a valid role.")
    if not name:
        raise ValidationError("Please enter your full name.")
    if not valid_email(email):
        raise ValidationError("Please enter a valid email address.")
    if phone and not PHONE_RE.match(phone):
        raise ValidationError("Please enter a valid phone number.")
    problem = password_problem(password)
    if problem:
        raise ValidationError(problem)

    # The owner identity is bootstrapped from the environment; nobody may
    # squat on that address through the public form.
    if email == OWNER_EMAIL:
        raise AuthorizationError("This email cannot be registered.")

    if users.exists_email(email):
        # Generic wording: actionable for a real person who forgot they have
        # an account, but not a machine-friendly confirmation, and rate
        # limited per IP - so not a usable enumeration oracle.
        raise ConflictError("This email cannot be used. If you already have "
                            "an account, please log in or reset your "
                            "password.")

    sport = data.get("sport")
    if sport is not None and sport not in SPORTS:
        sport = None
    age = as_int(data.get("age"), 5, 100, 16)

    # verified=0: the account exists but cannot log in until the emailed code
    # is entered. The account row and its role profile are one registration -
    # an account with no profile row would be half-created.
    with unit_of_work():
        uid = users.create(role, name, email, phone, hash_password(password),
                           verified=0, photo=None, created_at=now_iso())
        if role == "athlete":
            users.create_athlete_profile(uid, sport or "Athletics", age,
                                         "Urban")
        elif role == "coach":
            users.create_coach_profile(uid, clean(data.get("specialty"), 120),
                                       clean(data.get("bio"), 400))

    code, _ = issue_token(email, "verify")
    subject, html = email_verify(name, code)
    queue_email(email, subject, html)
    log_activity(uid, "signup", role)
    return uid, email, code


def verify_email(email, code):
    """Consume the OTP and activate the account."""
    if not valid_email(email) or len(code) != 6:
        raise ValidationError("Invalid or expired verification code.")
    if not consume_token(email, "verify", code):
        log_activity(None, "verify_failed", email)
        raise ValidationError("Invalid or expired verification code.")

    with unit_of_work():
        users.mark_verified_by_email(email)
    row = users.find_by_email(email)
    if row:
        s, h = email_account_created(row["name"], row["role"])
        queue_email(email, s, h)
        s2, h2 = email_welcome(row["name"])
        queue_email(email, s2, h2)
        log_activity(row["id"], "verify_email")


def resend_verification(email):
    """Re-issue a verification code. Returns the code, or None when nothing
    was sent. Says nothing about whether the address exists."""
    if not valid_email(email):
        return None
    row = users.find_by_email(email)
    if not row or row["verified"] or token_cooldown_active(email, "verify"):
        return None
    code, _ = issue_token(email, "verify")
    s, h = email_verify(row["name"], code)
    queue_email(email, s, h)
    log_activity(row["id"], "verify_resend")
    return code


def authenticate(email, password, role=None):
    """Verify credentials and return the user row.

    Raises AuthenticationError for a bad credential, AuthorizationError for a
    role mismatch, NeedsVerification for an unverified account.
    """
    row = users.find_by_email(email)
    if not row or not check_password(password, row["pass_hash"]):
        log_activity(row["id"] if row else None, "login_failed", email)
        raise AuthenticationError("Invalid email or password.")

    # Role mismatch is reported only *after* the password checked out, so this
    # never tells an anonymous prober what role an address holds.
    if role and role in ALL_ROLES and row["role"] != role \
            and row["role"] not in PRIVILEGED_ROLES:
        raise AuthorizationError("This account is registered as a "
                                 + row["role"]
                                 + ". Please select the correct role.")

    if not row["verified"]:
        log_activity(row["id"], "login_unverified")
        code = None
        if not token_cooldown_active(email, "verify"):
            code, _ = issue_token(email, "verify")
            s, h = email_verify(row["name"], code)
            queue_email(email, s, h)
        raise NeedsVerification(email, code)
    return row


def note_login(user_id):
    with unit_of_work():
        users.touch_last_login(user_id, now_iso())
    log_activity(user_id, "login")


def request_password_reset(email):
    """Issue a reset code. Returns (code, sent) - (None, True) when the
    address is unknown, so the caller cannot distinguish the two cases."""
    if not valid_email(email) or token_cooldown_active(email, "reset"):
        return None, True
    row = users.find_by_email(email)
    if not row:
        return None, True
    code, _ = issue_token(email, "reset")
    s, hbody = email_reset(row["name"], code)
    # `accepted`, not `delivered`: the job boundary cannot promise delivery.
    # The caller uses it only to avoid claiming a code was sent when the send
    # was refused outright (queue saturated, or an inline send failed).
    sent = queue_email(email, s, hbody)
    log_activity(row["id"], "password_reset_requested")
    return code, sent


def reset_password(email, code, new_password):
    """Consume the reset OTP, set the new credential and revoke sessions."""
    problem = password_problem(new_password)
    if problem:
        raise ValidationError(problem)
    if not valid_email(email) or len(code) != 6:
        raise ValidationError("Invalid or expired reset code.")
    if not consume_token(email, "reset", code):
        log_activity(None, "password_reset_failed", email)
        raise ValidationError("Invalid or expired reset code.")

    row = users.find_by_email(email)
    if not row:
        return
    # ATOMIC: the new credential and the session revocation commit together
    # or not at all. Split across two transactions, a failure would leave the
    # password changed while every old session stayed valid.
    with unit_of_work():
        users.update_password(row["id"], hash_password(new_password))
        bump_epoch(row["id"])
    s, h = email_password_changed(row["name"])
    queue_email(email, s, h)
    log_activity(row["id"], "password_reset")


def change_password(user, current_password, new_password):
    """Change a known credential. Returns the refreshed user row."""
    if not check_password(current_password, user["pass_hash"]):
        log_activity(user["id"], "password_change_failed")
        raise ValidationError("Current password is incorrect.")
    problem = password_problem(new_password)
    if problem:
        raise ValidationError(problem)

    with unit_of_work():
        users.update_password(user["id"], hash_password(new_password))
        bump_epoch(user["id"])
    fresh = users.find_by_id(user["id"])
    s, h = email_password_changed(user["name"])
    queue_email(user["email"], s, h)
    log_activity(user["id"], "password_change")
    return fresh


def revoke_all_sessions(user_id):
    bump_epoch(user_id)
    log_activity(user_id, "logout_all")
