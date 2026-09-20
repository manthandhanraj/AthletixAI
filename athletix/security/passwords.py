# -*- coding: utf-8 -*-
"""Password hashing. bcrypt preferred, werkzeug fallback."""

from athletix.config import BCRYPT_ROUNDS

try:
    import bcrypt as _bcrypt

    def hash_password(plain):
        return _bcrypt.hashpw(plain.encode("utf-8"),
                              _bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")

    def check_password(plain, hashed):
        try:
            return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
        except ValueError:
            return False

except ImportError:  # pragma: no cover - fallback keeps the app runnable
    from werkzeug.security import check_password_hash, generate_password_hash

    def hash_password(plain):
        return generate_password_hash(plain)

    def check_password(plain, hashed):
        return check_password_hash(hashed, plain)



