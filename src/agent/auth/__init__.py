"""Synthetic-session auth: build a browser session for a fake user.

``build_session`` reads the resolved ``auth`` config + the repo's secrets and
returns an :class:`AuthSession` — a Playwright ``storage_state`` (cookies +
localStorage) and/or an entry URL — that the visual layer loads before
navigating. It never captures or replays a real user's session: every token is
forged from the app's own secret or minted for a synthetic user via a provider
admin API.

Returns ``None`` (review proceeds unauthenticated) when no auth is configured or
the required secret is absent.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from src.agent.config import (
    SYNTHETIC_SESSION_TTL_SECONDS,
    SYNTHETIC_USER_EMAIL,
    SYNTHETIC_USER_NAME,
)
from src.agent.auth.forge import forge_jwt, forge_nextauth, forge_supabase_jwt
from src.agent.auth import providers

logger = logging.getLogger(__name__)

_NEXTAUTH_DEFAULT_COOKIE = {"v5": "authjs.session-token", "v4": "next-auth.session-token"}


@dataclass
class AuthSession:
    storage_state: dict = field(default_factory=lambda: {"cookies": [], "origins": []})
    entry_url: str | None = None
    note: str = ""


def _synthetic_user(user_cfg: dict | None) -> dict:
    user = dict(user_cfg or {})
    user.setdefault("email", SYNTHETIC_USER_EMAIL)
    user.setdefault("name", SYNTHETIC_USER_NAME)
    return user


def _origin_and_host(dev_server_url: str) -> tuple[str, str]:
    parsed = urlparse(dev_server_url)
    host = parsed.hostname or "localhost"
    origin = f"{parsed.scheme or 'http'}://{host}" + (f":{parsed.port}" if parsed.port else "")
    return origin, host


def _cookie(name: str, value: str, host: str, ttl: int) -> dict:
    return {
        "name": name, "value": value, "domain": host, "path": "/",
        "httpOnly": True, "secure": False, "sameSite": "Lax",
        "expires": int(time.time()) + ttl,
    }


def _state(cookies: list | None = None, origin: str | None = None,
           local_storage: list | None = None) -> dict:
    origins = [{"origin": origin, "localStorage": local_storage}] if origin and local_storage else []
    return {"cookies": cookies or [], "origins": origins}


def _nextauth(auth: dict, secrets: dict, origin: str, host: str) -> AuthSession | None:
    secret = secrets.get("NEXTAUTH_SECRET") or secrets.get("AUTH_SECRET")
    if not secret:
        logger.warning("auth.type=nextauth but no NEXTAUTH_SECRET/AUTH_SECRET provided; skipping auth")
        return None
    version = auth.get("version", "v5")
    cookie_name = auth.get("cookieName") or _NEXTAUTH_DEFAULT_COOKIE.get(version, _NEXTAUTH_DEFAULT_COOKIE["v5"])
    user = _synthetic_user(auth.get("user"))
    claims = {"name": user.get("name"), "email": user.get("email"), "sub": user.get("sub", "renderpr-preview")}
    claims = {k: v for k, v in claims.items() if v is not None}
    salt = cookie_name if version == "v5" else ""
    token = forge_nextauth(secret, claims, version=version, salt=salt)
    return AuthSession(_state(cookies=[_cookie(cookie_name, token, host, SYNTHETIC_SESSION_TTL_SECONDS)]),
                       note=f"forged NextAuth {version} session")


def _generic_jwt(auth: dict, secrets: dict, origin: str, host: str) -> AuthSession | None:
    secret_key = auth.get("secret", "JWT_SECRET")
    secret = secrets.get(secret_key)
    if not secret:
        logger.warning("auth.type=jwt but secret %r not provided; skipping auth", secret_key)
        return None
    user = _synthetic_user(auth.get("user"))
    claims = {"sub": user.get("sub", "renderpr-preview"), **{k: v for k, v in user.items() if k != "sub"}}
    token = forge_jwt(secret, claims, algorithm=auth.get("algorithm", "HS256"))
    name = auth.get("name", "token")
    if auth.get("storage", "cookie") == "localStorage":
        return AuthSession(_state(origin=origin, local_storage=[{"name": name, "value": token}]),
                           note="forged JWT (localStorage)")
    return AuthSession(_state(cookies=[_cookie(name, token, host, SYNTHETIC_SESSION_TTL_SECONDS)]),
                       note="forged JWT (cookie)")


def _supabase_ref(base_url: str) -> str:
    return (urlparse(base_url).hostname or "").split(".")[0] or "localhost"


def _supabase_storage(base_url: str, origin: str, access_token: str,
                      refresh_token: str, user: dict) -> AuthSession:
    ref = _supabase_ref(base_url)
    payload = json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + SYNTHETIC_SESSION_TTL_SECONDS,
        "token_type": "bearer",
        "user": {"email": user.get("email"), "role": user.get("role", "authenticated")},
    })
    return AuthSession(_state(origin=origin, local_storage=[{"name": f"sb-{ref}-auth-token", "value": payload}]),
                       note="Supabase session (localStorage)")


def _supabase(auth: dict, secrets: dict, origin: str, host: str, http_client) -> AuthSession | None:
    base_url = auth.get("baseUrl") or secrets.get("NEXT_PUBLIC_SUPABASE_URL") or secrets.get("SUPABASE_URL")
    if not base_url:
        logger.warning("auth.type=supabase but no project URL (baseUrl/NEXT_PUBLIC_SUPABASE_URL); skipping auth")
        return None
    user = _synthetic_user(auth.get("user"))
    jwt_secret = secrets.get("SUPABASE_JWT_SECRET")
    if jwt_secret:  # classic symmetric secret -> forge directly
        token = forge_supabase_jwt(jwt_secret, user)
        return _supabase_storage(base_url, origin, token, "", user)
    service_role = secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if service_role:  # asymmetric-key projects -> admin API
        session = providers.mint_supabase_session(base_url, service_role, user, client=http_client)
        return _supabase_storage(base_url, origin, session["access_token"],
                                 session.get("refresh_token", ""), user)
    logger.warning("auth.type=supabase but neither SUPABASE_JWT_SECRET nor SUPABASE_SERVICE_ROLE_KEY provided")
    return None


def _clerk(auth: dict, secrets: dict, origin: str, host: str, dev_server_url: str, http_client) -> AuthSession | None:
    secret = secrets.get("CLERK_SECRET_KEY")
    user_id = auth.get("userId")
    if not secret or not user_id:
        logger.warning("auth.type=clerk requires CLERK_SECRET_KEY and auth.userId; skipping auth")
        return None
    ticket = providers.mint_clerk_sign_in_token(secret, user_id, client=http_client)
    sign_in_path = auth.get("signInPath", "/")
    entry = f"{dev_server_url.rstrip('/')}{sign_in_path}?__clerk_ticket={ticket}"
    return AuthSession(entry_url=entry, note="Clerk sign-in ticket (entry URL)")


def _firebase(auth: dict, secrets: dict, origin: str, host: str, http_client) -> AuthSession | None:
    service_account = secrets.get("FIREBASE_SERVICE_ACCOUNT")
    api_key = (auth.get("apiKey") or secrets.get("FIREBASE_API_KEY")
               or secrets.get("NEXT_PUBLIC_FIREBASE_API_KEY"))
    if not service_account or not api_key:
        logger.warning("auth.type=firebase requires FIREBASE_SERVICE_ACCOUNT and an API key; skipping auth")
        return None
    user = _synthetic_user(auth.get("user"))
    result = providers.mint_firebase_id_token(service_account, api_key,
                                              uid=user.get("sub", "renderpr-preview"), client=http_client)
    # Best-effort: Firebase web SDK persists auth in IndexedDB by default; this
    # localStorage shape works for SDKs configured with browserLocalPersistence.
    ls_key = f"firebase:authUser:{api_key}:[DEFAULT]"
    payload = json.dumps({
        "uid": user.get("sub", "renderpr-preview"),
        "email": user.get("email"),
        "stsTokenManager": {
            "accessToken": result["idToken"],
            "refreshToken": result.get("refreshToken", ""),
            "expirationTime": (int(time.time()) + SYNTHETIC_SESSION_TTL_SECONDS) * 1000,
        },
    })
    return AuthSession(_state(origin=origin, local_storage=[{"name": ls_key, "value": payload}]),
                       note="Firebase ID token (localStorage, best-effort)")


def build_session(auth_config: dict | None, secrets: dict, dev_server_url: str,
                  *, http_client=None) -> AuthSession | None:
    if not auth_config:
        return None
    atype = auth_config.get("type")
    origin, host = _origin_and_host(dev_server_url)
    try:
        if atype == "nextauth":
            session = _nextauth(auth_config, secrets, origin, host)
        elif atype == "jwt":
            session = _generic_jwt(auth_config, secrets, origin, host)
        elif atype == "supabase":
            session = _supabase(auth_config, secrets, origin, host, http_client)
        elif atype == "clerk":
            session = _clerk(auth_config, secrets, origin, host, dev_server_url, http_client)
        elif atype == "firebase":
            session = _firebase(auth_config, secrets, origin, host, http_client)
        else:
            logger.warning("Unknown auth.type %r; skipping auth", atype)
            return None
    except Exception:
        logger.warning("Failed to build %s auth session; proceeding unauthenticated", atype, exc_info=True)
        return None
    if session:
        logger.info("Built auth session: %s", session.note)
    return session
