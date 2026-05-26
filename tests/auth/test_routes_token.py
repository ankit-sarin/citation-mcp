"""Tests for /oauth/token (authorization_code + refresh_token grants)."""

from __future__ import annotations

import base64
import hashlib

from citation_mcp.auth import tokens as tk
from citation_mcp.auth.storage import new_chain_id


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


VERIFIER = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123-_"
CHALLENGE = _pkce_challenge(VERIFIER)
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


async def _seeded_client(storage):
    client_id, _ = await storage.register_client({
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
    })
    return client_id


async def _seeded_auth_code(storage, client_id: str, user_email: str = "u@e.x") -> str:
    code = tk.new_authorization_code()
    await storage.store_authorization_code(
        code,
        client_id=client_id,
        user_email=user_email,
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        scope=None,
        resource=None,
        ttl_seconds=tk.AUTHORIZATION_CODE_TTL_SECONDS,
    )
    return code


# ---------------------------------------------------------------------------
# authorization_code grant
# ---------------------------------------------------------------------------


async def test_token_authcode_happy_path(storage, make_app, make_client) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    code = await _seeded_auth_code(storage, client_id)
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": VERIFIER,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == tk.ACCESS_TOKEN_TTL_SECONDS
    assert "access_token" in body
    assert "refresh_token" in body


async def test_token_authcode_wrong_verifier(storage, make_app, make_client) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    code = await _seeded_auth_code(storage, client_id)
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": "tampered" + VERIFIER,
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_token_authcode_reused_code(storage, make_app, make_client) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    code = await _seeded_auth_code(storage, client_id)
    async with make_client(app) as http:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        }
        first = await http.post("/oauth/token", data=data)
        second = await http.post("/oauth/token", data=data)
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


async def test_token_authcode_mismatched_redirect_uri(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    code = await _seeded_auth_code(storage, client_id)
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://other.example/cb",
                "client_id": client_id,
                "code_verifier": VERIFIER,
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_token_unknown_client_returns_401(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "x",
                "redirect_uri": REDIRECT,
                "client_id": "unknown",
                "code_verifier": VERIFIER,
            },
        )
    # RFC 6749 §5.2 — client lookup failure → 401 invalid_client
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


async def test_token_wrong_content_type(storage, make_app, make_client) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            content='{"grant_type":"authorization_code"}',
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 415


# ---------------------------------------------------------------------------
# refresh_token grant
# ---------------------------------------------------------------------------


async def test_token_refresh_happy_path(storage, make_app, make_client) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    chain = new_chain_id()
    refresh = tk.new_refresh_token()
    await storage.store_refresh_token(
        refresh,
        client_id=client_id,
        user_email="u@e.x",
        chain_id=chain,
        parent_hash=None,
        scope=None,
        ttl_seconds=tk.REFRESH_TOKEN_TTL_SECONDS,
    )
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["refresh_token"] != refresh
    # Old token must be marked rotated.
    old = await storage.get_refresh_token(refresh)
    assert old.rotated_at is not None


async def test_token_refresh_reuse_revokes_chain(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    chain = new_chain_id()
    refresh = tk.new_refresh_token()
    await storage.store_refresh_token(
        refresh,
        client_id=client_id,
        user_email="u@e.x",
        chain_id=chain,
        parent_hash=None,
        scope=None,
        ttl_seconds=tk.REFRESH_TOKEN_TTL_SECONDS,
    )
    async with make_client(app) as http:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
        first = await http.post("/oauth/token", data=data)
        assert first.status_code == 200
        second = await http.post("/oauth/token", data=data)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    # Verify chain is revoked in DB
    rotated_new_token = first.json()["refresh_token"]
    rec_old = await storage.get_refresh_token(refresh)
    rec_new = await storage.get_refresh_token(rotated_new_token)
    assert rec_old.revoked_at is not None
    assert rec_new.revoked_at is not None


async def test_token_refresh_unknown_token(storage, make_app, make_client) -> None:
    app = make_app(storage)
    client_id = await _seeded_client(storage)
    async with make_client(app) as http:
        resp = await http.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "definitely-not-issued",
                "client_id": client_id,
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
