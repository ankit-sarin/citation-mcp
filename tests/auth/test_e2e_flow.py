"""End-to-end OAuth flow: register → authorize → token → /mcp.

Drives the full app in-process via httpx.ASGITransport. Asserts that the
refresh-rotation chain and the reuse-revocation invariants hold across
real HTTP boundaries.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from typing import AsyncIterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from citation_mcp.auth.storage import OAuthStorage
from citation_mcp.http_app import build_http_app


@contextlib.asynccontextmanager
async def lifespan_manager(app) -> AsyncIterator[None]:
    """Manually drive ASGI lifespan startup/shutdown for an in-process app."""
    in_queue: asyncio.Queue = asyncio.Queue()
    out_queue: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await in_queue.get()

    async def send(message):
        await out_queue.put(message)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await in_queue.put({"type": "lifespan.startup"})
    startup = await out_queue.get()
    assert startup["type"] == "lifespan.startup.complete", startup
    try:
        yield
    finally:
        await in_queue.put({"type": "lifespan.shutdown"})
        shutdown = await out_queue.get()
        assert shutdown["type"] == "lifespan.shutdown.complete", shutdown
        await task

SIGNING_KEY = "x" * 64
ISSUER = "https://citation-mcp.example.com"
AUDIENCE = ISSUER
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "this-is-a-pkce-verifier-at-least-43-characters-long-1234567"


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")


@pytest.fixture
async def app_and_storage():
    storage = OAuthStorage(":memory:")
    app = build_http_app(
        signing_key=SIGNING_KEY,
        issuer=ISSUER,
        audience=AUDIENCE,
        origin_allowlist=("https://claude.ai",),
        host_allowlist=("testserver", "localhost", "citation-mcp.example.com"),
        storage=storage,
    )
    async with lifespan_manager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client, storage


async def test_full_oauth_flow_with_refresh_and_reuse(app_and_storage) -> None:
    http, storage = app_and_storage

    # 1. DCR
    reg = await http.post(
        "/oauth/register",
        json={"redirect_uris": [REDIRECT]},
    )
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    # 2. Authorize (dev fallback identity — Cf header optional in test app
    # since CF env vars aren't set; we provide the header explicitly).
    challenge = _challenge(VERIFIER)
    auth_resp = await http.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "e2e",
        },
        headers={"cf-access-authenticated-user-email": "alice@example.com"},
        follow_redirects=False,
    )
    assert auth_resp.status_code == 302
    code = parse_qs(urlparse(auth_resp.headers["location"]).query)["code"][0]

    # 3. Exchange code → access + refresh tokens
    tok_resp = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert tok_resp.status_code == 200
    payload = tok_resp.json()
    access_token = payload["access_token"]
    refresh_token = payload["refresh_token"]

    # 4. /mcp with bearer should at minimum get past the bearer middleware.
    # We don't drive a full MCP initialize-handshake here — the spec accepts
    # "401 → with a bearer it isn't 401" as proof that auth is wired correctly,
    # and the SDK handles the rest. Use a benign GET to confirm the bearer
    # passes the gate (SDK responds 405 for non-POST after auth succeeds).
    mcp_resp = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        headers={
            "authorization": f"Bearer {access_token}",
            "origin": "https://claude.ai",
            "content-type": "application/json",
        },
    )
    # 200 or any non-401 means we crossed the bearer auth gate. 400/406 is
    # acceptable — the SDK rejects on protocol details (missing Accept header
    # or session id), but we proved auth worked.
    assert mcp_resp.status_code != 401, (
        f"bearer auth failed: {mcp_resp.status_code} {mcp_resp.text}"
    )

    # 5. Refresh — new pair, old marked rotated
    refresh_resp = await http.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    assert refresh_resp.status_code == 200
    new_refresh = refresh_resp.json()["refresh_token"]
    assert new_refresh != refresh_token

    # 6. Replay the original refresh → reuse detected, chain revoked
    replay = await http.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    # 7. Direct DB inspection — verify chain revocation cascade
    old_rec = await storage.get_refresh_token(refresh_token)
    new_rec = await storage.get_refresh_token(new_refresh)
    assert old_rec is not None and old_rec.revoked_at is not None
    assert new_rec is not None and new_rec.revoked_at is not None


async def test_unauthenticated_mcp_returns_401_with_www_authenticate(
    app_and_storage,
) -> None:
    http, _ = app_and_storage
    resp = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        headers={
            "origin": "https://claude.ai",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401
    # SDK adds the WWW-Authenticate header pointing at the protected-resource doc.
    www = resp.headers.get("www-authenticate", "")
    assert www.lower().startswith("bearer")
