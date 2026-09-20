# -*- coding: utf-8 -*-
"""Input validation and normalisation.

Every value that arrives from a client passes through here. These helpers are
intentionally strict and total: they return a safe value or a clear failure,
never a partially-trusted one, and never raise on hostile input.
"""

import base64
import re

from flask import request

from athletix.config import (EMAIL_RE, METRICS, PASSWORD_MAX, PASSWORD_MIN,
                             PHOTO_MAGIC, PHOTO_MIMES, VIDEO_EXTS)


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text, limit=300):
    """Input hardening: strip control characters, cap length, neutralize
    angle brackets so stored text can never open an HTML tag (the frontend
    also encodes on render - this is defence in depth, not the only layer).
    """
    if text is None:
        return ""
    text = _CTRL_RE.sub("", str(text)).strip()[:limit]
    return text.replace("<", "&lt;").replace(">", "&gt;")


def valid_email(email):
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def password_problem(pw):
    """Return a human-readable reason the password is unacceptable, or None.

    Policy: 8+ characters, must not be one of the obvious throwaways, and no
    longer than bcrypt's 72-byte working limit (beyond which extra characters
    are silently ignored, which would be worse than refusing them). No
    punctuation/case gymnastics - length beats complexity theatre for real
    users, and hostile rules push people towards 'Passw0rd!'.
    """
    if not isinstance(pw, str):
        return "Please enter a password."
    if len(pw) < PASSWORD_MIN:
        return "Password must be at least %d characters." % PASSWORD_MIN
    if len(pw.encode("utf-8")) > PASSWORD_MAX:
        return "Password must be at most %d characters." % PASSWORD_MAX
    if pw.lower() in _COMMON_PASSWORDS:
        return "That password is too common. Please choose another."
    if len(set(pw)) < 4:
        return "Please choose a less repetitive password."
    return None


_COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwertyuiop", "qwerty123", "iloveyou", "abc12345",
    "11111111", "letmein1", "admin123", "welcome1", "athletix", "football",
    "cricket1", "athletixai", "changeme", "passw0rd",
}


def valid_password(pw):
    return password_problem(pw) is None


def as_int(value, lo, hi, default=None):
    """Coerce client input to a bounded int, or return `default`.

    Every numeric field from a request goes through this - `int(x)` on a
    client-supplied value is how endpoints end up returning 500s with a
    traceback attached.
    """
    if isinstance(value, bool):
        return default            # True would otherwise sail through as 1
    if isinstance(value, float) and not value.is_integer():
        return default            # 2.5 must not silently become a 2-star rating
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if n < lo or n > hi:
        return default
    return n


def as_id(value):
    """A database row id from the client: positive int or None."""
    return as_int(value, 1, 2 ** 53, None)


def body():
    """Parsed JSON request body, always a dict, never raising.

    Malformed JSON, a JSON array, a bare string or no body at all all become
    `{}` so every handler can validate fields instead of guarding parsing.
    Oversized bodies are rejected earlier, in `limit_body_size()`.
    """
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def overall_of(m):
    return round(sum(m[k] for k in METRICS) / 5.0, 1)


def safe_filename(name):
    """Reduce a client-supplied filename to a harmless label.

    Nothing on the server opens this path - it is metadata only - but it is
    displayed and stored, so directory traversal, NUL bytes and unexpected
    extensions are stripped rather than trusted.
    """
    name = _CTRL_RE.sub("", str(name or ""))
    name = name.replace("\\", "/").split("/")[-1]      # drop any path part
    name = re.sub(r"[^A-Za-z0-9._ \-]", "_", name)[:120].strip(" .")
    if not name:
        return "video"
    stem, dot, ext = name.rpartition(".")
    if not dot or ("." + ext.lower()) not in VIDEO_EXTS:
        return "video"
    return (stem[:100] or "video") + "." + ext.lower()


def validate_photo(photo):
    """Validate a base64 data-URL image by declared type AND real content.

    Returns an error string, or None when the value is safe to store. The
    old check was `startswith("data:image/")`, which let 500 KB of arbitrary
    attacker-controlled text be stored and later injected into an <img src>.
    """
    import base64
    if len(photo) > 500000:
        return "Image is too large (max ~350 KB)."
    m = re.match(r"^data:image/([a-z+]{2,10});base64,([A-Za-z0-9+/=\s]+)$",
                 photo)
    if not m:
        return "Invalid image format."
    subtype = m.group(1)
    if subtype not in PHOTO_MIMES:
        return "Unsupported image type. Use PNG, JPEG, GIF or WebP."
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except Exception:
        return "Invalid image data."
    if len(raw) > 350 * 1024:
        return "Image is too large (max ~350 KB)."
    if not any(raw.startswith(sig) for sig in PHOTO_MAGIC):
        # Content does not match any image format we accept - the declared
        # MIME type alone is never trusted.
        return "That file does not look like an image."
    return None
