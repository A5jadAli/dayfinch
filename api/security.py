from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LENGTH = 64


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LENGTH,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, n, r, p, salt_text, expected_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(expected_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def verify_totp(secret: str, code: str, *, at_time: int | None = None) -> bool:
    if not secret or not code.isdigit() or len(code) != 6:
        return False
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    try:
        key = base64.b32decode(padded)
    except (ValueError, TypeError):
        return False
    counter = int(at_time or time.time()) // 30
    for drift in (-1, 0, 1):
        digest = hmac.new(
            key, struct.pack(">Q", counter + drift), hashlib.sha1
        ).digest()
        offset = digest[-1] & 0x0F
        value = (
            struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        ) % 1_000_000
        if hmac.compare_digest(f"{value:06d}", code):
            return True
    return False
