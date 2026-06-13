import json

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.agent.auth.providers import (
    ProviderError,
    mint_clerk_sign_in_token,
    mint_firebase_id_token,
    mint_supabase_session,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    """Routes POSTs to canned responses by URL substring; records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for substring, resp in self.routes:
            if substring in url:
                return resp
        return FakeResponse(404, {})

    def close(self):
        pass


# --- Supabase ---

def test_mint_supabase_session_happy_path():
    client = FakeClient([
        ("/admin/users", FakeResponse(200, {})),
        ("/admin/generate_link", FakeResponse(200, {"hashed_token": "hash123"})),
        ("/auth/v1/verify", FakeResponse(200, {"access_token": "at", "refresh_token": "rt"})),
    ])
    session = mint_supabase_session("https://proj.supabase.co", "service_role_key",
                                    {"email": "p@r.dev"}, client=client)
    assert session["access_token"] == "at"
    assert session["refresh_token"] == "rt"
    # user-create attempted before link generation
    assert any("/admin/users" in url for url, _ in client.calls)


def test_mint_supabase_session_nested_hashed_token():
    client = FakeClient([
        ("/admin/users", FakeResponse(200, {})),
        ("/admin/generate_link", FakeResponse(200, {"properties": {"hashed_token": "h"}})),
        ("/auth/v1/verify", FakeResponse(200, {"access_token": "at"})),
    ])
    assert mint_supabase_session("https://x.co", "k", client=client)["access_token"] == "at"


def test_mint_supabase_session_generate_link_error():
    client = FakeClient([
        ("/admin/users", FakeResponse(200, {})),
        ("/admin/generate_link", FakeResponse(500, {})),
    ])
    with pytest.raises(ProviderError, match="generate_link"):
        mint_supabase_session("https://x.co", "k", client=client)


def test_mint_supabase_session_no_access_token():
    client = FakeClient([
        ("/admin/users", FakeResponse(200, {})),
        ("/admin/generate_link", FakeResponse(200, {"hashed_token": "h"})),
        ("/auth/v1/verify", FakeResponse(200, {})),
    ])
    with pytest.raises(ProviderError, match="access_token"):
        mint_supabase_session("https://x.co", "k", client=client)


# --- Clerk ---

def test_mint_clerk_sign_in_token():
    client = FakeClient([("sign_in_tokens", FakeResponse(200, {"token": "ticket_abc"}))])
    assert mint_clerk_sign_in_token("sk_test", "user_123", client=client) == "ticket_abc"


def test_mint_clerk_sign_in_token_error():
    client = FakeClient([("sign_in_tokens", FakeResponse(401, {}))])
    with pytest.raises(ProviderError, match="clerk"):
        mint_clerk_sign_in_token("sk_test", "user_123", client=client)


# --- Firebase ---

def _service_account() -> dict:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return {"client_email": "sa@proj.iam.gserviceaccount.com", "private_key": pem, "_public": pub}


def test_mint_firebase_id_token_signs_valid_custom_token():
    sa = _service_account()
    captured = {}

    class CapturingClient(FakeClient):
        def post(self, url, **kwargs):
            captured["body"] = kwargs.get("json")
            return FakeResponse(200, {"idToken": "id123", "refreshToken": "r"})

    out = mint_firebase_id_token(sa, "API_KEY", uid="preview-1", client=CapturingClient([]))
    assert out["idToken"] == "id123"

    # The custom token sent to Firebase is a valid RS256 JWT with the right aud/uid.
    decoded = pyjwt.decode(
        captured["body"]["token"], sa["_public"], algorithms=["RS256"],
        audience="https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkitService",
    )
    assert decoded["uid"] == "preview-1"
    assert decoded["iss"] == sa["client_email"]


def test_mint_firebase_accepts_service_account_json_string():
    sa = _service_account()
    client = FakeClient([("signInWithCustomToken", FakeResponse(200, {"idToken": "id"}))])
    out = mint_firebase_id_token(json.dumps(sa), "API_KEY", client=client)
    assert out["idToken"] == "id"


def test_mint_firebase_id_token_error():
    sa = _service_account()
    client = FakeClient([("signInWithCustomToken", FakeResponse(400, {}))])
    with pytest.raises(ProviderError, match="firebase"):
        mint_firebase_id_token(sa, "API_KEY", client=client)
