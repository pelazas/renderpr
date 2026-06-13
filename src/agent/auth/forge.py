"""Forge self-issued session tokens from an app's own signing secret.

No real login, no captured cookies: we mint a token the app already trusts and
hand it to the browser. Covers NextAuth/Auth.js (which encrypts its session as a
JWE) and any app that signs its own JWT (custom HS256, Supabase legacy JWT secret).

The NextAuth/Auth.js crypto parameters here were taken from the libraries' source
and validated against the real ``next-auth`` (v4) and ``@auth/core`` (v5)
``decode()`` used as an oracle. Because those tokens are *encrypted* (JWE, key
derived from the secret via HKDF), they cannot be produced with a plain JWT lib.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import jwt as pyjwt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.agent.config import SYNTHETIC_SESSION_TTL_SECONDS

# Default cookie name == HKDF salt for Auth.js v5 (non-secure origin).
NEXTAUTH_V5_DEFAULT_SALT = "authjs.session-token"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-SHA256, matching @panva/hkdf (used by NextAuth/Auth.js)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 0
    while len(okm) < length:
        counter += 1
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
    return okm[:length]


def _with_session_claims(claims: dict, ttl: int) -> dict:
    now = int(time.time())
    out = dict(claims)
    out.setdefault("iat", now)
    out.setdefault("exp", now + ttl)
    return out


def _json_compact(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def forge_nextauth_v4(secret: str, claims: dict, salt: str = "",
                      ttl: int = SYNTHETIC_SESSION_TTL_SECONDS) -> str:
    """NextAuth v4 session JWE: alg=dir, enc=A256GCM, empty HKDF salt."""
    info = f"NextAuth.js Generated Encryption Key{f' ({salt})' if salt else ''}".encode()
    key = _hkdf(secret.encode(), salt.encode(), info, 32)
    header = _b64u(_json_compact({"alg": "dir", "enc": "A256GCM"}))
    iv = os.urandom(12)
    ct_tag = AESGCM(key).encrypt(iv, _json_compact(_with_session_claims(claims, ttl)), header.encode())
    return ".".join([header, "", _b64u(iv), _b64u(ct_tag[:-16]), _b64u(ct_tag[-16:])])


def forge_nextauth_v5(secret: str, claims: dict, salt: str = NEXTAUTH_V5_DEFAULT_SALT,
                      ttl: int = SYNTHETIC_SESSION_TTL_SECONDS) -> str:
    """Auth.js v5 session JWE: alg=dir, enc=A256CBC-HS512, HKDF salt=cookie name."""
    key = _hkdf(secret.encode(), salt.encode(), f"Auth.js Generated Encryption Key ({salt})".encode(), 64)
    mac_key, enc_key = key[:32], key[32:]
    header = _b64u(_json_compact({"alg": "dir", "enc": "A256CBC-HS512"}))
    iv = os.urandom(16)
    plaintext = _json_compact(_with_session_claims(claims, ttl))
    pad = 16 - (len(plaintext) % 16)
    plaintext += bytes([pad]) * pad  # PKCS7
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    aad = header.encode()
    al = (len(aad) * 8).to_bytes(8, "big")
    tag = hmac.new(mac_key, aad + iv + ciphertext + al, hashlib.sha512).digest()[:32]
    return ".".join([header, "", _b64u(iv), _b64u(ciphertext), _b64u(tag)])


def forge_nextauth(secret: str, claims: dict, version: str = "v5",
                   salt: str | None = None, ttl: int = SYNTHETIC_SESSION_TTL_SECONDS) -> str:
    if version == "v4":
        return forge_nextauth_v4(secret, claims, salt if salt is not None else "", ttl)
    return forge_nextauth_v5(secret, claims, salt or NEXTAUTH_V5_DEFAULT_SALT, ttl)


def forge_jwt(secret: str, claims: dict, algorithm: str = "HS256",
              ttl: int = SYNTHETIC_SESSION_TTL_SECONDS) -> str:
    """Generic self-signed JWT for roll-your-own auth."""
    return pyjwt.encode(_with_session_claims(claims, ttl), secret, algorithm=algorithm)


def forge_supabase_jwt(jwt_secret: str, user: dict,
                       ttl: int = SYNTHETIC_SESSION_TTL_SECONDS) -> str:
    """Supabase access token: HS256 JWT signed with the project JWT secret.

    Works for projects on the (classic) symmetric JWT secret; projects using the
    newer asymmetric signing keys must use the service_role admin path instead.
    """
    now = int(time.time())
    claims = {
        "sub": user.get("sub") or user.get("id") or "00000000-0000-0000-0000-000000000000",
        "email": user.get("email"),
        "role": user.get("role", "authenticated"),
        "aud": user.get("aud", "authenticated"),
        "iat": now,
        "exp": now + ttl,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, jwt_secret, algorithm="HS256")
