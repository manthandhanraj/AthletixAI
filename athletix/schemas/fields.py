# -*- coding: utf-8 -*-
"""A very small declarative schema layer.

Deliberately dependency-free: the project ships `flask`, `bcrypt` and
`gunicorn` and nothing else, and pulling a validation framework in to parse
twenty small JSON bodies would be a worse trade than a few hundred readable
lines.

Design rules
------------
*Total.* Every field returns a safe value or raises `ValidationError`, which
the existing error handler already renders as the Phase 1 `{"ok": false,
"error": ...}` body with a 400. Hostile input never reaches a handler
half-parsed.

*Ordered.* Fields are declared as an ordered tuple, not as class attributes,
because the order decides which message a caller sees when two fields are
both wrong. Making that order explicit is the point.

*Normalise here, decide in the service.* A schema trims, lowercases, coerces
and range-checks. It does not know whether an e-mail is already registered or
whether a role may be granted - those are business rules and live in
`athletix/services/`, which must stay usable without a request context.
"""

import re

from athletix.errors import ValidationError
from athletix.validation import as_id, as_int, clean

__all__ = ["Schema", "Field", "Text", "Email", "Password", "Code", "Integer",
           "Identifier", "Flag", "Choice", "Mapping", "StringList", "Phone",
           "DataUrl", "Raw", "ValidationError"]

_MISSING = object()


class Field(object):
    """Base field. `error` is the message shown when this field is rejected."""

    def __init__(self, required=False, default=None, error=None):
        self.required = required
        self.default = default
        self.error = error

    def _fail(self, fallback):
        raise ValidationError(self.error or fallback)

    def load(self, raw, present, name):
        if not present or raw is None:
            if self.required:
                self._fail("Missing required field: %s." % name)
            return self.default
        return self.parse(raw, name)

    def parse(self, raw, name):
        return raw


class Text(Field):
    """A short free-text value.

    `sanitize` runs it through `validation.clean()` - control characters
    stripped, length capped, angle brackets neutralised - which is what every
    stored string in this application already goes through.
    """

    def __init__(self, max_len=300, sanitize=True, strip=True, lower=False,
                 required=False, default="", error=None, allow_empty=True):
        super(Text, self).__init__(required=required, default=default,
                                   error=error)
        self.max_len = max_len
        self.sanitize = sanitize
        self.strip = strip
        self.lower = lower
        self.allow_empty = allow_empty

    def parse(self, raw, name):
        if isinstance(raw, (dict, list, bool)):
            self._fail("Invalid value for %s." % name)
        if self.sanitize:
            value = clean(raw, self.max_len)
        else:
            value = str(raw)[:self.max_len]
        if self.strip:
            value = value.strip()
        if self.lower:
            value = value.lower()
        if not value and not self.allow_empty:
            self._fail("Missing required field: %s." % name)
        return value


class Email(Field):
    """Normalised e-mail address: trimmed, lowercased, capped at 254 bytes.

    `validate=True` additionally rejects a malformed address here rather than
    letting it travel further in. It is left off for the endpoints that must
    answer identically for a valid and an invalid address, so they cannot be
    turned into an account-enumeration oracle.
    """

    def __init__(self, validate=False, required=False, error=None):
        super(Email, self).__init__(required=required, default="", error=error)
        self.validate = validate

    def parse(self, raw, name):
        if not isinstance(raw, str):
            if self.validate:
                self._fail("Please enter a valid email address.")
            return ""
        value = raw.strip().lower()[:254]
        if self.validate:
            from athletix.validation import valid_email
            if not valid_email(value):
                self._fail("Please enter a valid email address.")
        return value


class Password(Field):
    """A credential. Never sanitised, never trimmed, never truncated.

    `clean()` would silently rewrite a legitimate password containing '<',
    which turns into an unexplainable login failure. Strength policy lives in
    `validation.password_problem` and runs in the service.
    """

    def __init__(self, required=False, error=None):
        super(Password, self).__init__(required=required, default="",
                                       error=error)

    def parse(self, raw, name):
        if not isinstance(raw, str):
            return ""
        return raw


class Code(Field):
    """A 6-digit OTP: digits only, capped. Never anything else."""

    def __init__(self, length=6, required=False, error=None):
        super(Code, self).__init__(required=required, default="", error=error)
        self.length = length

    def parse(self, raw, name):
        if isinstance(raw, (dict, list, bool)):
            return ""
        return re.sub(r"\D", "", str(raw))[:self.length]


class Integer(Field):
    """A bounded integer.

    Out-of-range or non-numeric input falls back to `default`, or is rejected
    outright when `error` is set - both behaviours exist in the Phase 1 API
    and the difference is per-endpoint, so it is declared per field.
    """

    def __init__(self, lo, hi, default=None, required=False, error=None):
        super(Integer, self).__init__(required=required, default=default,
                                      error=error)
        self.lo = lo
        self.hi = hi

    def parse(self, raw, name):
        value = as_int(raw, self.lo, self.hi, _MISSING)
        if value is _MISSING:
            if self.error:
                self._fail("Invalid value for %s." % name)
            return self.default
        return value


class Identifier(Field):
    """A row id from a client: a positive integer, or None."""

    def parse(self, raw, name):
        value = as_id(raw)
        if value is None and self.error:
            self._fail("Invalid id.")
        return value


class Flag(Field):
    """A truthy client flag, normalised to a real bool."""

    def __init__(self, default=False):
        super(Flag, self).__init__(required=False, default=default)

    def parse(self, raw, name):
        return bool(raw)


class Choice(Field):
    """A value that must come from a fixed vocabulary.

    With `error` set the value is rejected; without it an unknown value
    becomes `default`, which is how several Phase 1 endpoints already behave
    (an unrecognised sport falls back rather than 400ing).
    """

    def __init__(self, choices, default=None, required=False, error=None):
        super(Choice, self).__init__(required=required, default=default,
                                     error=error)
        self.choices = tuple(choices)

    def parse(self, raw, name):
        if raw not in self.choices:
            if self.error:
                self._fail("Invalid value for %s." % name)
            return self.default
        return raw


class Mapping(Field):
    """A nested object. Non-objects become `default` unless `error` is set."""

    def __init__(self, default=None, required=False, error=None):
        super(Mapping, self).__init__(required=required, default=default,
                                      error=error)

    def parse(self, raw, name):
        if not isinstance(raw, dict):
            if self.error:
                self._fail("Invalid value for %s." % name)
            return self.default
        return raw


class StringList(Field):
    """A list of short strings, each sanitised, the list itself capped."""

    def __init__(self, max_items=20, max_len=160, default=None, error=None):
        super(StringList, self).__init__(required=False, default=default,
                                         error=error)
        self.max_items = max_items
        self.max_len = max_len

    def parse(self, raw, name):
        if not isinstance(raw, list):
            if self.error:
                self._fail("Invalid value for %s." % name)
            return self.default
        return [clean(item, self.max_len) for item in raw][:self.max_items]


class Phone(Field):
    """A phone number, validated against the same regex the services use."""

    def __init__(self, required=False, error=None):
        super(Phone, self).__init__(required=required, default="", error=error)

    def parse(self, raw, name):
        from athletix.config import PHONE_RE
        value = clean(raw, 20)
        if value and not PHONE_RE.match(value):
            self._fail("Please enter a valid phone number.")
        return value


class DataUrl(Field):
    """A large base64 `data:` payload (the profile photo).

    Never sanitised and never truncated: `clean()` would corrupt base64, and
    truncating would turn "too large" into "malformed". Content validation
    (declared type AND magic bytes) is `validation.validate_photo`, which the
    service calls. A falsy value normalises to "" - the Phase 1 way of saying
    "clear my photo".
    """

    def __init__(self):
        super(DataUrl, self).__init__(required=False, default="")

    def parse(self, raw, name):
        if not raw:
            return ""
        return raw


class Raw(Field):
    """Passed through untouched, for values a service validates itself."""


class Schema(object):
    """Base class for a per-endpoint request schema.

    Usage:

        class LoginRequest(Schema):
            fields = (("email", Email(validate=True)),
                      ("password", Password()))

        data = LoginRequest.load(body())

    `load` returns a plain dict containing exactly the declared keys, so a
    handler can never accidentally forward an undeclared client key into a
    service call. Unknown keys in the body are ignored rather than rejected,
    which keeps older and newer clients working against the same endpoint.
    """

    fields = ()

    @classmethod
    def load(cls, data):
        if not isinstance(data, dict):
            data = {}
        out = {}
        for name, field in cls.fields:
            out[name] = field.load(data.get(name), name in data, name)
        return out

    @classmethod
    def field_names(cls):
        return tuple(name for name, _ in cls.fields)

    @classmethod
    def load_present(cls, data):
        """Like `load`, but returns only the keys the body actually carried.

        Some endpoints treat "absent" and "empty" differently: a coach profile
        update writes only the columns the client sent, so "" must be
        distinguishable from "not mentioned". Fields whose value normalises to
        None (a client sent the key with an unusable value) are dropped, which
        is the Phase 1 `isinstance(...)`-guard behaviour.
        """
        if not isinstance(data, dict):
            return {}
        out = {}
        for name, field in cls.fields:
            if name not in data:
                continue
            value = field.load(data.get(name), True, name)
            if value is None:
                continue
            out[name] = value
        return out

    @classmethod
    def present(cls, data, name):
        """True when the raw body actually carried this key."""
        return isinstance(data, dict) and name in data
