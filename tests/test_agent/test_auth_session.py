import json

from src.agent.auth import AuthSession, build_session
from src.agent.auth import providers

DEV_URL = "http://localhost:3000/"


def test_no_config_returns_none():
    assert build_session(None, {}, DEV_URL) is None


def test_nextauth_v5_cookie():
    s = build_session({"type": "nextauth"}, {"NEXTAUTH_SECRET": "x" * 40}, DEV_URL)
    assert isinstance(s, AuthSession)
    cookies = s.storage_state["cookies"]
    assert len(cookies) == 1
    assert cookies[0]["name"] == "authjs.session-token"
    assert cookies[0]["domain"] == "localhost"
    assert cookies[0]["secure"] is False
    assert cookies[0]["value"].count(".") == 4  # JWE


def test_nextauth_v4_cookie_name():
    s = build_session({"type": "nextauth", "version": "v4"}, {"NEXTAUTH_SECRET": "x" * 40}, DEV_URL)
    assert s.storage_state["cookies"][0]["name"] == "next-auth.session-token"


def test_nextauth_missing_secret_returns_none():
    assert build_session({"type": "nextauth"}, {}, DEV_URL) is None


def test_generic_jwt_cookie_and_localstorage():
    secrets = {"JWT_SECRET": "s" * 40}
    cookie_session = build_session({"type": "jwt"}, secrets, DEV_URL)
    assert cookie_session.storage_state["cookies"][0]["name"] == "token"

    ls_session = build_session(
        {"type": "jwt", "storage": "localStorage", "name": "auth"}, secrets, DEV_URL
    )
    origins = ls_session.storage_state["origins"]
    assert origins[0]["origin"] == "http://localhost:3000"
    assert origins[0]["localStorage"][0]["name"] == "auth"


def test_generic_jwt_custom_secret_key():
    s = build_session({"type": "jwt", "secret": "MY_TOKEN_SECRET"},
                      {"MY_TOKEN_SECRET": "z" * 40}, DEV_URL)
    assert s is not None
    assert build_session({"type": "jwt", "secret": "MY_TOKEN_SECRET"}, {}, DEV_URL) is None


def test_supabase_forge_localstorage_key():
    s = build_session(
        {"type": "supabase", "baseUrl": "https://abcdef.supabase.co"},
        {"SUPABASE_JWT_SECRET": "j" * 40}, DEV_URL,
    )
    entry = s.storage_state["origins"][0]["localStorage"][0]
    assert entry["name"] == "sb-abcdef-auth-token"
    assert json.loads(entry["value"])["access_token"]


def test_supabase_admin_path(monkeypatch):
    monkeypatch.setattr(providers, "mint_supabase_session",
                        lambda *a, **k: {"access_token": "AT", "refresh_token": "RT"})
    s = build_session(
        {"type": "supabase", "baseUrl": "https://proj.supabase.co"},
        {"SUPABASE_SERVICE_ROLE_KEY": "svc"}, DEV_URL,
    )
    value = json.loads(s.storage_state["origins"][0]["localStorage"][0]["value"])
    assert value["access_token"] == "AT"
    assert value["refresh_token"] == "RT"


def test_supabase_missing_url_returns_none():
    assert build_session({"type": "supabase"}, {"SUPABASE_JWT_SECRET": "j" * 40}, DEV_URL) is None


def test_clerk_entry_url(monkeypatch):
    monkeypatch.setattr(providers, "mint_clerk_sign_in_token", lambda *a, **k: "TICKET123")
    s = build_session({"type": "clerk", "userId": "user_1"}, {"CLERK_SECRET_KEY": "sk"}, DEV_URL)
    assert s.entry_url == "http://localhost:3000/?__clerk_ticket=TICKET123"


def test_clerk_requires_user_id():
    assert build_session({"type": "clerk"}, {"CLERK_SECRET_KEY": "sk"}, DEV_URL) is None


def test_firebase_localstorage(monkeypatch):
    monkeypatch.setattr(providers, "mint_firebase_id_token",
                        lambda *a, **k: {"idToken": "ID", "refreshToken": "RT"})
    s = build_session({"type": "firebase"},
                      {"FIREBASE_SERVICE_ACCOUNT": "{}", "FIREBASE_API_KEY": "AK"}, DEV_URL)
    entry = s.storage_state["origins"][0]["localStorage"][0]
    assert entry["name"] == "firebase:authUser:AK:[DEFAULT]"
    assert json.loads(entry["value"])["stsTokenManager"]["accessToken"] == "ID"


def test_provider_exception_degrades_to_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(providers, "mint_clerk_sign_in_token", boom)
    assert build_session({"type": "clerk", "userId": "u"}, {"CLERK_SECRET_KEY": "sk"}, DEV_URL) is None
