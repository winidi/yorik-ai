"""Yorik as an OpenID Connect provider for Immich (backend/oidc.py):
discovery, the code flow with the Yorik session, PKCE, single-use
codes, a verifiable id_token, userinfo."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_client


@pytest.fixture
def provider(fresh_app, monkeypatch):
    from backend import oidc as O
    from backend import credential_store as CS
    box: dict = {}
    monkeypatch.setattr(CS, "get", lambda name: box.get(name))
    monkeypatch.setattr(CS, "put", lambda name, creds: box.__setitem__(name, dict(creds)))
    monkeypatch.setattr(O, "_key_cache", None)
    O._codes.clear(); O._access.clear()
    client = O.ensure_client("immich", ["https://photos.example/auth/login", "app.immich:///oauth-callback"])
    return O, client


def _code_from(location: str) -> tuple[str, dict]:
    q = parse_qs(urlparse(location).query)
    return q["code"][0], q


def test_discovery_and_keys(provider, fresh_app):
    O, _ = provider
    anon = TestClient(fresh_app)
    d = anon.get("/.well-known/openid-configuration", headers={"host": "yorik.example", "x-forwarded-proto": "https"}).json()
    assert d["issuer"] == "https://yorik.example"
    assert d["authorization_endpoint"] == "https://yorik.example/oidc/authorize"
    keys = anon.get("/oidc/jwks").json()["keys"]
    assert keys[0]["kty"] == "RSA" and keys[0]["alg"] == "RS256" and keys[0]["kid"] == O.signing_key()["kid"]
    assert O.signing_key() is O.signing_key()                    # made once


def test_the_code_flow_signs_the_yorik_person_in(provider, fresh_app):
    O, client = provider
    beate_c, beate = login_client(fresh_app, role="member", name="Beate Mayer", email="beate@example.local")
    anon = TestClient(fresh_app)
    auth = {"client_id": "immich", "redirect_uri": "https://photos.example/auth/login", "response_type": "code",
            "scope": "openid email profile", "state": "s1", "nonce": "n1"}
    # no Yorik session: to the Yorik login, with the way back
    r = anon.get("/oidc/authorize", params=auth, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/r/home?oidc_next=%2Foidc%2Fauthorize%3F")
    # a wrong redirect_uri gets nothing at all
    assert beate_c.get("/oidc/authorize", params={**auth, "redirect_uri": "https://evil.example/"}, follow_redirects=False).status_code == 400
    # with the session: a code for Immich
    r = beate_c.get("/oidc/authorize", params=auth, follow_redirects=False)
    assert r.status_code == 302
    code, q = _code_from(r.headers["location"])
    assert r.headers["location"].startswith("https://photos.example/auth/login?") and q["state"] == ["s1"]
    # Immich exchanges the code (client_secret_post, like its default)
    r = anon.post("/oidc/token", data={"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": auth["redirect_uri"], "client_id": "immich",
                                       "client_secret": client["client_secret"]},
                  headers={"host": "yorik.example", "x-forwarded-proto": "https"})
    assert r.status_code == 200, r.text
    tok = r.json()
    pub = O.signing_key()["public"]
    claims = jwt.decode(tok["id_token"], pub, algorithms=["RS256"], audience="immich", issuer="https://yorik.example")
    assert claims["sub"] == beate and claims["email"] == "beate@example.local" and claims["nonce"] == "n1"
    assert claims["name"] == "Beate Mayer" and claims["preferred_username"] == "beate"
    info = anon.get("/oidc/userinfo", headers={"authorization": f"Bearer {tok['access_token']}"}).json()
    assert info["sub"] == beate and info["given_name"] == "Beate"
    # the code was single use; a wrong secret is refused
    r = anon.post("/oidc/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": auth["redirect_uri"],
                                       "client_id": "immich", "client_secret": client["client_secret"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert anon.post("/oidc/token", data={"grant_type": "authorization_code", "code": "x", "redirect_uri": auth["redirect_uri"],
                                          "client_id": "immich", "client_secret": "nope"}).status_code == 401
    assert anon.get("/oidc/userinfo", headers={"authorization": "Bearer nope"}).status_code == 401


def test_pkce_and_disabled_accounts(provider, fresh_app):
    O, client = provider
    from backend.database import get_conn
    kid_c, kid = login_client(fresh_app, role="restricted", name="Kid", email="kid@example.local")
    anon = TestClient(fresh_app)
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    auth = {"client_id": "immich", "redirect_uri": "app.immich:///oauth-callback", "response_type": "code",
            "scope": "openid", "code_challenge": challenge, "code_challenge_method": "S256"}
    code, _ = _code_from(kid_c.get("/oidc/authorize", params=auth, follow_redirects=False).headers["location"])
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": auth["redirect_uri"],
            "client_id": "immich", "client_secret": client["client_secret"]}
    assert anon.post("/oidc/token", data={**base, "code_verifier": "b" * 64}).status_code == 400   # wrong verifier, code burnt
    code, _ = _code_from(kid_c.get("/oidc/authorize", params=auth, follow_redirects=False).headers["location"])
    r = anon.post("/oidc/token", data={**base, "code": code, "code_verifier": verifier})
    assert r.status_code == 200 and jwt.decode(r.json()["id_token"], O.signing_key()["public"], algorithms=["RS256"],
                                                audience="immich")["sub"] == kid   # a child gets their own library
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET disabled = 1 WHERE id = ?", (kid,)); conn.commit()
    r = kid_c.get("/oidc/authorize", params=auth, follow_redirects=False)
    assert r.status_code in (302, 401, 403)
    if r.status_code == 302:
        assert "error=access_denied" in r.headers["location"] or "oidc_next" in r.headers["location"]
