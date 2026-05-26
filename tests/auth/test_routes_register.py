"""Tests for /oauth/register (RFC 7591 DCR)."""

from __future__ import annotations


async def test_register_happy_path(storage, make_app, make_client) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.post(
            "/oauth/register",
            json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "client_name": "Test",
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert "client_id" in body and body["client_id"]
    assert "client_id_issued_at" in body
    assert body["token_endpoint_auth_method"] == "none"


async def test_register_requires_redirect_uris(storage, make_app, make_client) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.post("/oauth/register", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


async def test_register_rejects_non_https_redirect(storage, make_app, make_client) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://evil.example/cb"]},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


async def test_register_allows_http_localhost(storage, make_app, make_client) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://localhost:9000/cb"]},
        )
    assert resp.status_code == 201


async def test_register_rejects_confidential_auth_method(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.post(
            "/oauth/register",
            json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )
    assert resp.status_code == 400


async def test_register_rejects_wrong_content_type(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.post(
            "/oauth/register",
            content='{"redirect_uris":["https://claude.ai/cb"]}',
            headers={"content-type": "text/plain"},
        )
    assert resp.status_code == 415
