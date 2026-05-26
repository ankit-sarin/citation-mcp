"""Tests for /oauth/authorize."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

CF_HEADER = "cf-access-authenticated-user-email"
USER_EMAIL = "user@example.com"


async def _register(client) -> str:
    resp = await client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
    )
    assert resp.status_code == 201
    return resp.json()["client_id"]


async def test_authorize_happy_path(storage, make_app, make_client) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        client_id = await _register(client)
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD",
                "code_challenge_method": "S256",
                "state": "xyz",
            },
            headers={CF_HEADER: USER_EMAIL},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "https"
    assert parsed.netloc == "claude.ai"
    qs = parse_qs(parsed.query)
    assert "code" in qs and qs["code"][0]
    assert qs["state"] == ["xyz"]


async def test_authorize_missing_code_challenge_redirects_with_error(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        client_id = await _register(client)
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge_method": "S256",
                "state": "s",
            },
            headers={CF_HEADER: USER_EMAIL},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["error"] == ["invalid_request"]
    assert qs["state"] == ["s"]


async def test_authorize_plain_method_rejected(storage, make_app, make_client) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        client_id = await _register(client)
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge": "abc",
                "code_challenge_method": "plain",
            },
            headers={CF_HEADER: USER_EMAIL},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["error"] == ["invalid_request"]


async def test_authorize_unknown_client_returns_400(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "nope",
                "redirect_uri": "https://claude.ai/cb",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
            headers={CF_HEADER: USER_EMAIL},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


async def test_authorize_mismatched_redirect_uri(
    storage, make_app, make_client
) -> None:
    app = make_app(storage)
    async with make_client(app) as client:
        client_id = await _register(client)
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://evil.example/cb",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
            headers={CF_HEADER: USER_EMAIL},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


async def test_authorize_missing_cf_header_strict(
    storage, make_app, make_client
) -> None:
    app = make_app(storage, allow_missing_cf_email=False)
    async with make_client(app) as client:
        client_id = await _register(client)
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "access_denied"


async def test_authorize_missing_cf_header_dev_fallback(
    storage, make_app, make_client
) -> None:
    app = make_app(storage, allow_missing_cf_email=True, dev_user_email="dev@local")
    async with make_client(app) as client:
        client_id = await _register(client)
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    code = qs["code"][0]
    # Confirm the stored code records the dev fallback email.
    rec = await storage.consume_authorization_code(code)
    assert rec is not None and rec.user_email == "dev@local"
