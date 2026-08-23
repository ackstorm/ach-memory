import hashlib
import hmac
import secrets

KEY_PREFIX = "mem_"


def generate_key() -> str:
    """A 256-bit random API key. Returned to the caller exactly once."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(plaintext: str) -> str:
    """SHA-256, deliberately not a slow KDF.

    A slow KDF (bcrypt, argon2) exists to make brute force expensive against
    *low-entropy* secrets. These keys carry 256 bits of entropy from
    secrets.token_urlsafe, so brute force is already infeasible, and a slow
    hash would add its cost to every single authenticated request.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(plaintext), stored_hash)
