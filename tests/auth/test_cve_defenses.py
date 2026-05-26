"""Tests for the three CVE-driven middlewares.

CVE-2026-33252 — Go SDK Streamable HTTP cross-site POST
                 (Origin allowlist + Content-Type=application/json)
CVE-2026-35568 — Java SDK DNS rebinding via Host header
                 (Host allowlist returning 421 Misdirected Request)
CVE-2025-66416 — Python SDK Host header DNS rebinding fixed in 1.23.0
                 (our HostHeaderMiddleware is belt-and-suspenders + broader scope)
"""

from __future__ import annotations

import httpx
import pytest

from citation_mcp.auth.storage import OAuthStorage
from citation_mcp.auth.tokens import issue_access_token
from citation_mcp.http_app import build_http_app
from tests.auth.test_e2e_flow import lifespan_manager

SIGNING_KEY = "x" * 64


@pytest.fixture
async def app():
    storage = OAuthStorage(":memory:")
    a = build_http_app(
        signing_key=SIGNING_KEY,
        issuer="https://citation-mcp.example.com",
        audience="https://citation-mcp.example.com",
        origin_allowlist=("https://claude.ai",),
        host_allowlist=("citation-mcp.example.com", "localhost", "testserver"),
        storage=storage,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a), base_url="http://testserver"
    ) as client:
        yield client
    await storage.close()


async def test_mcp_post_with_disallowed_origin_returns_403(app: httpx.AsyncClient) -> None:
    resp = await app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        headers={"origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403


async def test_mcp_post_with_no_origin_passes_through_to_bearer(
    app: httpx.AsyncClient,
) -> None:
    # No Origin → not blocked by origin middleware; bearer middleware then 401s.
    resp = await app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
    )
    assert resp.status_code == 401


async def test_mcp_post_with_allowlisted_origin_passes_through(
    app: httpx.AsyncClient,
) -> None:
    resp = await app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        headers={"origin": "https://claude.ai"},
    )
    assert resp.status_code == 401  # blocked by bearer, not origin


async def test_mcp_post_wrong_content_type_returns_415(app: httpx.AsyncClient) -> None:
    resp = await app.post(
        "/mcp",
        content="hello",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 415


async def test_disallowed_host_returns_421(app: httpx.AsyncClient) -> None:
    resp = await app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        headers={"host": "attacker.example.com"},
    )
    assert resp.status_code == 421


async def test_allowed_host_passes_through(app: httpx.AsyncClient) -> None:
    # "testserver" is in the allowlist for this fixture.
    resp = await app.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200


async def test_healthz_unauthenticated(app: httpx.AsyncClient) -> None:
    resp = await app.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_authenticated_mcp_with_public_host_is_not_421() -> None:
    """Regression: SDK's TransportSecurity must not 421 traffic forwarded
    from a reverse proxy.

    The SDK auto-enables DNS rebinding protection with a localhost-only
    allowlist when its host arg defaults to 127.0.0.1, and that check
    fires AFTER our bearer auth — so it's invisible to unauthenticated
    tests. Our HostHeaderMiddleware already validates against
    MCP_HOST_ALLOWLIST; the SDK's redundant copy is disabled in
    http_app.py.

    Failure mode this guards against: a valid bearer + a non-localhost
    Host header from our own allowlist gets 421 from inside the SDK.
    """
    storage = OAuthStorage(":memory:")
    issuer = "https://citation-mcp.example.com"
    audience = issuer
    a = build_http_app(
        signing_key=SIGNING_KEY,
        issuer=issuer,
        audience=audience,
        origin_allowlist=("https://claude.ai",),
        host_allowlist=("citation-mcp.example.com", "localhost", "testserver"),
        storage=storage,
    )
    token = issue_access_token(
        sub="dr.ankitsarin@gmail.com",
        client_id="test-client",
        scope="",
        signing_key=SIGNING_KEY,
        issuer=issuer,
        audience=audience,
    )
    try:
        async with lifespan_manager(a):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=a),
                base_url="http://citation-mcp.example.com",
            ) as client:
                resp = await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                    headers={
                        "host": "citation-mcp.example.com",
                        "authorization": f"Bearer {token}",
                        "origin": "https://claude.ai",
                        "content-type": "application/json",
                    },
                )
        assert resp.status_code != 421, (
            f"SDK transport_security 421'd a valid bearer + allowlisted Host. "
            f"Got {resp.status_code}: {resp.text}"
        )
        assert resp.status_code != 401, (
            f"bearer auth failed unexpectedly: {resp.status_code} {resp.text}"
        )
    finally:
        await storage.close()
