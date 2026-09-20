# -*- coding: utf-8 -*-
"""Per-endpoint request schemas.

One schema per endpoint that takes a body. Each one declares - in the order
the value is validated - exactly which keys the endpoint accepts, what each
one is normalised to, and which failures are rejected at the edge.

Two rules keep this layer honest:

1. **The schema owns shape; the service owns meaning.** A schema trims,
   lowercases, coerces and range-checks. Whether a role may be granted,
   whether an address is already taken, whether the caller may write to a row
   - those are business rules, they live in `athletix/services/`, and they
   must keep working when a service is called without a request context.

2. **Field order is the error order.** When a body is wrong in two ways, the
   caller sees the message for whichever field is declared first. The order
   below mirrors the order the corresponding service validates in, so moving
   parsing out of the handlers did not change which message a client gets.

Fields that a service validates *conditionally* (an athlete's sport is
checked, a coach's is ignored) are declared `Raw`: pre-empting that check here
would answer 400 where Phase 1 answered 200.
"""

from athletix.config import ALLOWED_CONSENTS
from athletix.schemas.fields import (Choice, Code, DataUrl, Email, Flag,
                                     Identifier, Integer, Mapping, Password,
                                     Phone, Raw, Schema, StringList, Text)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
class SignupRequest(Schema):
    """POST /auth/signup.

    `role` is Raw on purpose: `services.auth.register` is the single place a
    role is ever chosen from client input, it checks against PUBLIC_ROLES, and
    it writes an audit line when a client asks for one it may not have.
    """
    fields = (
        ("role", Raw()),
        ("name", Text(80)),
        ("email", Email()),
        ("phone", Text(20)),
        ("password", Password()),
        ("sport", Raw()),
        ("age", Raw()),
        ("specialty", Text(120)),
        ("bio", Text(400)),
    )


class VerifyRequest(Schema):
    """POST /auth/verify."""
    fields = (("email", Email()), ("code", Code()))


class ResendRequest(Schema):
    """POST /auth/resend. `email` is not validated here: the endpoint must
    answer identically for a valid and an invalid address."""
    fields = (("email", Email()),)


class LoginRequest(Schema):
    """POST /auth/login.

    `email` IS validated here, because Phase 1 answered 400 for a malformed
    address before it consumed a rate-limit slot, and that ordering is what
    stops a garbage-address flood from locking a real user out.
    """
    fields = (
        ("email", Email(validate=True)),
        ("password", Password()),
        ("role", Raw()),
        ("remember", Flag()),
    )


class ForgotPasswordRequest(Schema):
    """POST /auth/forgot. Unvalidated address: the response is deliberately
    identical whether or not the account exists."""
    fields = (("email", Email()),)


class ResetPasswordRequest(Schema):
    """POST /auth/reset."""
    fields = (
        ("email", Email()),
        ("code", Code()),
        ("password", Password()),
    )


# --------------------------------------------------------------------------
# Profile / account
# --------------------------------------------------------------------------
class ProfileUpdateRequest(Schema):
    """POST /profile/update.

    sport / age / location are Raw because `services.profile.update_profile`
    validates them only for athletes; a coach sending a stray `sport` is
    ignored, not rejected, and that is the Phase 1 behaviour.
    """
    fields = (
        ("name", Text(80)),
        ("phone", Phone()),
        ("sport", Raw()),
        ("age", Raw()),
        ("location", Raw()),
    )


class PhotoRequest(Schema):
    """POST /profile/photo."""
    fields = (("photo", DataUrl()),)


class PasswordChangeRequest(Schema):
    """POST /profile/password."""
    fields = (("current", Password()), ("password", Password()))


class EmailChangeRequest(Schema):
    """POST /profile/email/request."""
    fields = (("email", Email()), ("password", Password()))


class EmailConfirmRequest(Schema):
    """POST /profile/email/confirm."""
    fields = (("email", Email()), ("code", Code()))


class DeleteAccountRequest(Schema):
    """POST /profile/delete."""
    fields = (("password", Password()),)


class ConsentRequest(Schema):
    """POST /consent. The key must be one we actually store; the value is a
    plain truthy flag."""
    fields = (
        ("key", Choice(ALLOWED_CONSENTS, default=None)),
        ("value", Raw()),
    )


class CoachProfileRequest(Schema):
    """POST /coach/profile.

    Loaded with `load_present()`: this endpoint is a partial update, so a key
    that was not sent must not be written. `experience` is rejected at the
    edge with the Phase 1 message.
    """
    fields = (
        ("specialty", Text(120)),
        ("bio", Text(400)),
        ("experience", Integer(0, 80, error="Invalid years of experience.")),
        ("city", Text(80)),
        ("achievements", StringList(max_items=20, max_len=160)),
    )


# --------------------------------------------------------------------------
# Assessments
# --------------------------------------------------------------------------
class ReportCreateRequest(Schema):
    """POST /reports.

    `m` and `ai` are client-computed: the AI/CV pipeline runs in the browser.
    They are UNTRUSTED input - see `athletix/scoring/` for the trust boundary
    that decides what the server is willing to own.
    """
    fields = (
        ("m", Mapping(default={})),
        ("ai", Mapping(default=None)),
        ("video", Mapping(default=None)),
        ("live", Flag()),
        ("scoring", Mapping(default=None)),
    )


# --------------------------------------------------------------------------
# Messaging / notifications / ratings
# --------------------------------------------------------------------------
class SendMessageRequest(Schema):
    """POST /messages."""
    fields = (("toId", Identifier()), ("text", Text(2000)))


class NotificationRequest(Schema):
    """POST /notifications."""
    fields = (("toId", Identifier()), ("text", Text(300)))


class RatingRequest(Schema):
    """POST /ratings.

    `stars` is Raw because `services.messaging.rate_coach` checks the caller's
    role *before* the value, so a coach with a malformed body must still get
    403 rather than 400.
    """
    fields = (("coachId", Identifier()), ("stars", Raw()))


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
class RoleChangeRequest(Schema):
    """POST /admin/users/<id>/role. 'owner' is absent from the vocabulary on
    purpose: no endpoint may ever mint a second owner."""
    fields = (("role", Choice(("athlete", "coach", "admin"),
                              error="Invalid role.")),)
