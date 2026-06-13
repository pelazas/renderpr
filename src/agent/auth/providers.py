"""Mint real sessions for a synthetic user via managed providers' backend APIs.

Used when forging isn't possible because a managed provider holds the signing
key (Clerk, Firebase, or Supabase projects on asymmetric keys). Each provider
exposes a backend/admin API that issues a session for us using an injected secret
— so we never script Google/GitHub and never store a user's real password.

These functions cover the *server* side (minting the artifact); placing the
artifact into the browser is the session builder's job. They take an optional
``client`` so tests can inject a fake transport.
"""
import json
import logging
import time
from typing import Any

import httpx
import jwt as pyjwt

from src.agent.config import AUTH_PROVIDER_TIMEOUT, SYNTHETIC_USER_EMAIL

logger = logging.getLogger(__name__)

_FIREBASE_AUD = (
    "https://identitytoolkit.googleapis.com/"
    "google.identity.identitytoolkit.v1.IdentityToolkitService"
)


class ProviderError(Exception):
    """A provider admin/token API call failed or returned an unexpected shape."""


def _post(client: httpx.Client | None, timeout: int, url: str, **kwargs) -> httpx.Response:
    owned = client is None
    c = client or httpx.Client(timeout=timeout)
    try:
        return c.post(url, **kwargs)
    finally:
        if owned:
            c.close()


def mint_supabase_session(
    base_url: str,
    service_role_key: str,
    user: dict | None = None,
    *,
    client: httpx.Client | None = None,
    timeout: int = AUTH_PROVIDER_TIMEOUT,
) -> dict:
    """Return a Supabase session (``access_token``/``refresh_token``) for a user.

    Ensures the user exists (idempotent), generates a magic link to obtain a
    one-time token hash, then verifies it to exchange for a real session — all
    with the service_role key. Works regardless of the project's JWT signing mode.
    """
    user = user or {}
    email = user.get("email", SYNTHETIC_USER_EMAIL)
    base = base_url.rstrip("/")
    admin_headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }
    owned = client is None
    c = client or httpx.Client(timeout=timeout)
    try:
        # Idempotent create; a 422 "already registered" is fine and ignored.
        c.post(f"{base}/auth/v1/admin/users", headers=admin_headers,
               json={"email": email, "email_confirm": True,
                     "user_metadata": {k: v for k, v in user.items() if k != "email"}})

        link = c.post(f"{base}/auth/v1/admin/generate_link", headers=admin_headers,
                      json={"type": "magiclink", "email": email})
        if link.status_code >= 400:
            raise ProviderError(f"supabase generate_link failed: HTTP {link.status_code}")
        data = link.json()
        token_hash = data.get("hashed_token") or data.get("properties", {}).get("hashed_token")
        if not token_hash:
            raise ProviderError("supabase generate_link returned no token hash")

        verify = c.post(f"{base}/auth/v1/verify",
                        headers={"apikey": service_role_key, "Content-Type": "application/json"},
                        json={"type": "magiclink", "token_hash": token_hash})
        if verify.status_code >= 400:
            raise ProviderError(f"supabase verify failed: HTTP {verify.status_code}")
        session = verify.json()
        if not session.get("access_token"):
            raise ProviderError("supabase verify returned no access_token")
        logger.info("Minted Supabase session for synthetic user")
        return session
    finally:
        if owned:
            c.close()


def mint_clerk_sign_in_token(
    secret_key: str,
    user_id: str,
    *,
    client: httpx.Client | None = None,
    timeout: int = AUTH_PROVIDER_TIMEOUT,
) -> str:
    """Create a Clerk sign-in token (ticket) for ``user_id`` via the Backend API.

    The browser consumes it by navigating to the app's sign-in with
    ``?__clerk_ticket=<token>`` (handled by the session builder).
    """
    resp = _post(client, timeout, "https://api.clerk.com/v1/sign_in_tokens",
                 headers={"Authorization": f"Bearer {secret_key}", "Content-Type": "application/json"},
                 json={"user_id": user_id})
    if resp.status_code >= 400:
        raise ProviderError(f"clerk sign_in_tokens failed: HTTP {resp.status_code}")
    token = resp.json().get("token")
    if not token:
        raise ProviderError("clerk sign_in_tokens returned no token")
    logger.info("Minted Clerk sign-in ticket")
    return token


def _firebase_custom_token(service_account: dict, uid: str) -> str:
    now = int(time.time())
    client_email = service_account["client_email"]
    claims = {
        "iss": client_email,
        "sub": client_email,
        "aud": _FIREBASE_AUD,
        "iat": now,
        "exp": now + 3600,
        "uid": uid,
    }
    return pyjwt.encode(claims, service_account["private_key"], algorithm="RS256")


def mint_firebase_id_token(
    service_account: dict | str,
    api_key: str,
    uid: str = "renderpr-preview",
    *,
    client: httpx.Client | None = None,
    timeout: int = AUTH_PROVIDER_TIMEOUT,
) -> dict:
    """Mint a Firebase custom token (RS256, service account) and exchange it for an
    ID token via the Identity Toolkit REST API."""
    if isinstance(service_account, str):
        service_account = json.loads(service_account)
    custom_token = _firebase_custom_token(service_account, uid)
    resp = _post(
        client, timeout,
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}",
        json={"token": custom_token, "returnSecureToken": True},
    )
    if resp.status_code >= 400:
        raise ProviderError(f"firebase signInWithCustomToken failed: HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("idToken"):
        raise ProviderError("firebase signInWithCustomToken returned no idToken")
    logger.info("Minted Firebase ID token for synthetic user")
    return data
