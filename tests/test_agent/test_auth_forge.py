"""Unit tests for session forging.

The committed code was additionally validated end-to-end against the real
next-auth (v4) and @auth/core (v5) ``decode()`` as an oracle; these tests lock
the crypto parameters (HKDF derivation, JWE structure) and verify claims survive,
so an accidental change to a salt/info/enc value fails fast.
"""
import base64
import hashlib
import hmac
import json

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.agent.auth.forge import (
    _hkdf,
    forge_jwt,
    forge_nextauth,
    forge_nextauth_v4,
    forge_nextauth_v5,
    forge_supabase_jwt,
)

SECRET = "super-secret-test-value-at-least-32-bytes-long"


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- locked derivation vectors (break if salt/info/length change) ---

def test_hkdf_v4_vector():
    assert _hkdf(b"secret", b"", b"NextAuth.js Generated Encryption Key", 32).hex() == (
        "9ab5058ebf5be88f82f4aee99d22cf8be7a34a4af1836299d0668f4d83020f5a"
    )


def test_hkdf_v5_vector():
    key = _hkdf(b"secret", b"authjs.session-token",
                b"Auth.js Generated Encryption Key (authjs.session-token)", 64)
    assert key.hex() == (
        "b1ef6cdd915c44c0a71232574a6a70546a5e16d12bb38df4638e3b96bdd32025"
        "131e90695ff072a9d148f1511c7a4981a85b63a628311da1fa7ccfc49c47c2bb"
    )


# --- NextAuth v4 (A256GCM) ---

def _decode_v4(token: str, secret: str, salt: str = "") -> dict:
    header_b64, _, iv_b64, ct_b64, tag_b64 = token.split(".")
    header = json.loads(_b64u_dec(header_b64))
    assert header == {"alg": "dir", "enc": "A256GCM"}
    key = _hkdf(secret.encode(), salt.encode(), b"NextAuth.js Generated Encryption Key", 32)
    pt = AESGCM(key).decrypt(_b64u_dec(iv_b64), _b64u_dec(ct_b64) + _b64u_dec(tag_b64), header_b64.encode())
    return json.loads(pt)


def test_forge_nextauth_v4_roundtrips():
    token = forge_nextauth_v4(SECRET, {"email": "preview@renderpr.dev", "sub": "abc"})
    assert token.count(".") == 4  # compact JWE has 5 parts
    claims = _decode_v4(token, SECRET)
    assert claims["email"] == "preview@renderpr.dev"
    assert claims["sub"] == "abc"
    assert claims["exp"] > claims["iat"]


def test_forge_nextauth_v4_wrong_secret_fails():
    token = forge_nextauth_v4(SECRET, {"email": "x@y.z"})
    with pytest.raises(Exception):
        _decode_v4(token, "different-secret")


# --- Auth.js v5 (A256CBC-HS512) ---

def _decode_v5(token: str, secret: str, salt: str = "authjs.session-token") -> dict:
    header_b64, _, iv_b64, ct_b64, tag_b64 = token.split(".")
    header = json.loads(_b64u_dec(header_b64))
    assert header == {"alg": "dir", "enc": "A256CBC-HS512"}
    key = _hkdf(secret.encode(), salt.encode(),
                f"Auth.js Generated Encryption Key ({salt})".encode(), 64)
    mac_key, enc_key = key[:32], key[32:]
    iv, ct, tag = _b64u_dec(iv_b64), _b64u_dec(ct_b64), _b64u_dec(tag_b64)
    aad = header_b64.encode()
    al = (len(aad) * 8).to_bytes(8, "big")
    expected = hmac.new(mac_key, aad + iv + ct + al, hashlib.sha512).digest()[:32]
    assert hmac.compare_digest(expected, tag), "auth tag mismatch"
    dec = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    padded = dec.update(ct) + dec.finalize()
    return json.loads(padded[: -padded[-1]])


def test_forge_nextauth_v5_roundtrips():
    token = forge_nextauth_v5(SECRET, {"email": "preview@renderpr.dev", "sub": "xyz"})
    assert token.count(".") == 4
    claims = _decode_v5(token, SECRET)
    assert claims["email"] == "preview@renderpr.dev"
    assert claims["sub"] == "xyz"


def test_forge_nextauth_dispatch_defaults_to_v5():
    v5 = forge_nextauth(SECRET, {"sub": "a"})
    assert json.loads(_b64u_dec(v5.split(".")[0]))["enc"] == "A256CBC-HS512"
    v4 = forge_nextauth(SECRET, {"sub": "a"}, version="v4")
    assert json.loads(_b64u_dec(v4.split(".")[0]))["enc"] == "A256GCM"


def test_forge_nextauth_v5_custom_salt_matches_cookie_name():
    salt = "__Secure-authjs.session-token"
    token = forge_nextauth_v5(SECRET, {"sub": "a"}, salt=salt)
    assert _decode_v5(token, SECRET, salt=salt)["sub"] == "a"


# --- generic JWT + Supabase ---

def test_forge_jwt_roundtrips_with_pyjwt():
    token = forge_jwt(SECRET, {"sub": "user-1", "role": "admin"})
    decoded = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    assert decoded["sub"] == "user-1"
    assert decoded["role"] == "admin"
    assert decoded["exp"] > decoded["iat"]


def test_forge_supabase_jwt_has_authenticated_aud():
    token = forge_supabase_jwt(SECRET, {"email": "preview@renderpr.dev"})
    decoded = pyjwt.decode(token, SECRET, algorithms=["HS256"], audience="authenticated")
    assert decoded["role"] == "authenticated"
    assert decoded["aud"] == "authenticated"
    assert decoded["email"] == "preview@renderpr.dev"
    assert decoded["sub"]  # synthesised when not provided
