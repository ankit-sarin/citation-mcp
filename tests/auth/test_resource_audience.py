"""RFC 8707 resource / JWT audience self-consistency (AUD-FIX-1024).

DIAG-1024: the protected-resource document advertised the audience without
its trailing slash, /oauth/token minted ``aud`` from the raw client
``resource``, and the verifier exact-matched the slashed OAUTH_AUDIENCE. From
2026-10-17 claude.ai sent the slashless value it read from the PRM, so every
token it was issued 401'd at /mcp and the connector looped
refresh → 401 → refresh.

Contract under test: one canonical resource (configured audience, trailing
slash stripped); absent / slashless / slashed ``resource`` accepted on
/authorize and both token grants; anything else -> ``invalid_target`` with no
code or token issued; minted ``aud`` is always canonical; the verifier still
accepts a legacy slashed ``aud``.

Config is parametrized over both OAUTH_AUDIENCE forms. The slashed form is the
production shape and the one the older tests never exercised.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from starlette.applications import Starlette

from citation_mcp.auth import tokens as tk
from citation_mcp.auth.discovery import build_discovery_routes
from citation_mcp.auth.routes import OAuthConfig, build_oauth_routes
from citation_mcp.auth.storage import OAuthStorage, new_chain_id
from citation_mcp.auth.verifier import CitationMcpTokenVerifier
from citation_mcp.http_app import build_http_app

from .test_e2e_flow import lifespan_manager

SIGNING_KEY = "x" * 64
ISSUER = "https://citation-mcp.example.com"
CANONICAL = "https://citation-mcp.example.com"
SLASHED = CANONICAL + "/"
FOREIGN = "https://evil.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "this-is-a-pkce-verifier-at-least-43-characters-long-1234567"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode("ascii")).digest())
    .decode("ascii")
    .rstrip("=")
)

CONFIG_AUDIENCES = pytest.mark.parametrize(
    "configured_audience", [SLASHED, CANONICAL], ids=["cfg-slashed", "cfg-slashless"]
)
# resource values that must be accepted (None = parameter absent)
ACCEPTED_RESOURCES = pytest.mark.parametrize(
    "resource", [CANONICAL, SLASHED, None], ids=["slashless", "slashed", "absent"]
)
# values that differ by more than one trailing slash
FOREIGN_RESOURCES = pytest.mark.parametrize(
    "resource",
    [FOREIGN, CANONICAL + "//", CANONICAL + "/mcp", "http://citation-mcp.example.com"],
    ids=["other-host", "double-slash", "sub-path", "http-scheme"],
)


def _aud(access_token: str) -> object:
    return jwt.decode(access_token, options={"verify_signature": False})["aud"]


def _with_resource(data: dict, resource: str | None) -> dict:
    if resource is not None:
        data = {**data, "resource": resource}
    return data


@pytest.fixture
async def storage():
    s = OAuthStorage(":memory:")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def _oauth_http(storage: OAuthStorage, audience: str) -> httpx.AsyncClient:
    cfg = OAuthConfig(
        storage=storage, signing_key=SIGNING_KEY, issuer=ISSUER, audience=audience
    )
    app = Starlette(routes=build_oauth_routes(cfg))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def _client_id(storage: OAuthStorage) -> str:
    client_id, _ = await storage.register_client(
        {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
    )
    return client_id


async def _seed_code(storage: OAuthStorage, client_id: str) -> str:
    code = tk.new_authorization_code()
    await storage.store_authorization_code(
        code,
        client_id=client_id,
        user_email="u@e.x",
        redirect_uri=REDIRECT,
        code_challenge=CHALLENGE,
        scope=None,
        resource=CANONICAL,
        ttl_seconds=tk.AUTHORIZATION_CODE_TTL_SECONDS,
    )
    return code


async def _seed_refresh(storage: OAuthStorage, client_id: str) -> str:
    refresh = tk.new_refresh_token()
    await storage.store_refresh_token(
        refresh,
        client_id=client_id,
        user_email="u@e.x",
        chain_id=new_chain_id(),
        parent_hash=None,
        scope=None,
        ttl_seconds=tk.REFRESH_TOKEN_TTL_SECONDS,
    )
    return refresh


def _code_form(client_id: str, code: str) -> dict:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": VERIFIER,
    }


def _refresh_form(client_id: str, refresh: str) -> dict:
    return {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}


def _authorize_params(client_id: str) -> dict:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "s",
    }


CF = {"cf-access-authenticated-user-email": "alice@example.com"}


# ---------------------------------------------------------------------------
# C1 / C4 — canonical computed once; PRM advertises it; issuer untouched
# ---------------------------------------------------------------------------


@CONFIG_AUDIENCES
def test_config_canonical_resource_strips_one_trailing_slash(configured_audience) -> None:
    cfg = OAuthConfig(
        storage=None, signing_key=SIGNING_KEY, issuer=ISSUER, audience=configured_audience
    )
    assert cfg.resource == CANONICAL
    assert cfg.audience == configured_audience  # raw config value is not rewritten


@CONFIG_AUDIENCES
async def test_prm_advertises_canonical_and_issuer_unchanged(configured_audience) -> None:
    app = Starlette(
        routes=build_discovery_routes(issuer=ISSUER, audience=configured_audience)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        prm = (await http.get("/.well-known/oauth-protected-resource")).json()
        asm = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert prm["resource"] == CANONICAL
    assert prm["authorization_servers"] == [ISSUER]
    assert asm["issuer"] == ISSUER


# ---------------------------------------------------------------------------
# C2 — /oauth/authorize
# ---------------------------------------------------------------------------


@CONFIG_AUDIENCES
@ACCEPTED_RESOURCES
async def test_authorize_accepts_resource_and_stores_canonical(
    storage, configured_audience, resource
) -> None:
    client_id = await _client_id(storage)
    async with _oauth_http(storage, configured_audience) as http:
        resp = await http.get(
            "/oauth/authorize",
            params=_with_resource(_authorize_params(client_id), resource),
            headers=CF,
            follow_redirects=False,
        )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert "error" not in qs
    record = await storage.consume_authorization_code(qs["code"][0])
    assert record is not None and record.resource == CANONICAL


@CONFIG_AUDIENCES
@FOREIGN_RESOURCES
async def test_authorize_foreign_resource_is_invalid_target_and_issues_no_code(
    storage, configured_audience, resource
) -> None:
    client_id = await _client_id(storage)
    async with _oauth_http(storage, configured_audience) as http:
        resp = await http.get(
            "/oauth/authorize",
            params=_with_resource(_authorize_params(client_id), resource),
            headers=CF,
            follow_redirects=False,
        )
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["error"] == ["invalid_target"]
    assert qs["state"] == ["s"]
    assert "code" not in qs
    cur = await storage._ensure().execute("SELECT count(*) FROM authorization_codes")
    assert (await cur.fetchone())[0] == 0


async def test_authorize_any_foreign_value_among_several_is_invalid_target(storage) -> None:
    client_id = await _client_id(storage)
    params = list(_authorize_params(client_id).items()) + [
        ("resource", CANONICAL),
        ("resource", FOREIGN),
    ]
    async with _oauth_http(storage, SLASHED) as http:
        resp = await http.get(
            "/oauth/authorize", params=params, headers=CF, follow_redirects=False
        )
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert qs["error"] == ["invalid_target"]
    assert "code" not in qs


# ---------------------------------------------------------------------------
# C2 — /oauth/token, authorization_code grant
# ---------------------------------------------------------------------------


@CONFIG_AUDIENCES
@ACCEPTED_RESOURCES
async def test_code_grant_accepts_resource_and_mints_canonical_aud(
    storage, configured_audience, resource
) -> None:
    client_id = await _client_id(storage)
    code = await _seed_code(storage, client_id)
    async with _oauth_http(storage, configured_audience) as http:
        resp = await http.post(
            "/oauth/token", data=_with_resource(_code_form(client_id, code), resource)
        )
    assert resp.status_code == 200, resp.text
    assert _aud(resp.json()["access_token"]) == CANONICAL


@CONFIG_AUDIENCES
@FOREIGN_RESOURCES
async def test_code_grant_foreign_resource_is_invalid_target_and_code_survives(
    storage, configured_audience, resource
) -> None:
    client_id = await _client_id(storage)
    code = await _seed_code(storage, client_id)
    async with _oauth_http(storage, configured_audience) as http:
        bad = await http.post(
            "/oauth/token", data={**_code_form(client_id, code), "resource": resource}
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid_target"
        assert "access_token" not in bad.json()
        # The rejected request consumed nothing: the same code still redeems.
        good = await http.post("/oauth/token", data=_code_form(client_id, code))
    assert good.status_code == 200, good.text


# ---------------------------------------------------------------------------
# C2 — /oauth/token, refresh_token grant
# ---------------------------------------------------------------------------


@CONFIG_AUDIENCES
@ACCEPTED_RESOURCES
async def test_refresh_grant_accepts_resource_and_mints_canonical_aud(
    storage, configured_audience, resource
) -> None:
    client_id = await _client_id(storage)
    refresh = await _seed_refresh(storage, client_id)
    async with _oauth_http(storage, configured_audience) as http:
        resp = await http.post(
            "/oauth/token",
            data=_with_resource(_refresh_form(client_id, refresh), resource),
        )
    assert resp.status_code == 200, resp.text
    assert _aud(resp.json()["access_token"]) == CANONICAL


@CONFIG_AUDIENCES
@FOREIGN_RESOURCES
async def test_refresh_grant_foreign_resource_is_invalid_target_and_token_not_rotated(
    storage, configured_audience, resource
) -> None:
    client_id = await _client_id(storage)
    refresh = await _seed_refresh(storage, client_id)
    async with _oauth_http(storage, configured_audience) as http:
        bad = await http.post(
            "/oauth/token",
            data={**_refresh_form(client_id, refresh), "resource": resource},
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid_target"
        rec = await storage.get_refresh_token(refresh)
        assert rec.rotated_at is None and rec.revoked_at is None
        # Not rotated, so presenting it again is NOT reuse — it still works.
        good = await http.post("/oauth/token", data=_refresh_form(client_id, refresh))
    assert good.status_code == 200, good.text


# ---------------------------------------------------------------------------
# C3 — verifier
# ---------------------------------------------------------------------------


def _token_with_aud(aud: str) -> str:
    return tk.issue_access_token(
        sub="u@e.x",
        client_id="c",
        scope="",
        signing_key=SIGNING_KEY,
        issuer=ISSUER,
        audience=aud,
    )


@CONFIG_AUDIENCES
@pytest.mark.parametrize("aud", [SLASHED, CANONICAL], ids=["legacy-slashed", "canonical"])
async def test_verifier_accepts_aud_equal_to_canonical_up_to_slash(
    configured_audience, aud
) -> None:
    v = CitationMcpTokenVerifier(
        signing_key=SIGNING_KEY, issuer=ISSUER, audience=configured_audience
    )
    result = await v.verify_token(_token_with_aud(aud))
    assert result is not None
    assert result.resource == aud


@CONFIG_AUDIENCES
@pytest.mark.parametrize(
    "aud", [FOREIGN, CANONICAL + "//", CANONICAL + "/mcp"],
    ids=["other-host", "double-slash", "sub-path"],
)
async def test_verifier_rejects_aud_beyond_trailing_slash(configured_audience, aud) -> None:
    v = CitationMcpTokenVerifier(
        signing_key=SIGNING_KEY, issuer=ISSUER, audience=configured_audience
    )
    assert await v.verify_token(_token_with_aud(aud)) is None


# ---------------------------------------------------------------------------
# Regression — the 2026-10-17 claude.ai loop, end to end through /mcp
# ---------------------------------------------------------------------------


async def _mcp_initialize(http: httpx.AsyncClient, access_token: str) -> httpx.Response:
    return await http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aud-fix-regression", "version": "0"},
            },
        },
        headers={
            "authorization": f"Bearer {access_token}",
            "origin": "https://claude.ai",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
    )


async def test_regression_1017_slashless_resource_reaches_mcp_200(
    tmp_path, monkeypatch
) -> None:
    """Production config (slashed OAUTH_AUDIENCE) + the resource claude.ai
    actually sends (the PRM value, slashless) on authorize, code exchange and
    refresh. Before AUD-FIX-1024 every /mcp call here returned 401
    InvalidAudienceError; it must now be 200 on both the first token and the
    refreshed one."""
    # A completed initialize runs the tool lifespan, which opens Cache() —
    # keep it off the default (production) data/cache.db.
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.db"))
    storage = OAuthStorage(":memory:")
    app = build_http_app(
        signing_key=SIGNING_KEY,
        issuer=ISSUER,
        audience=SLASHED,
        origin_allowlist=("https://claude.ai",),
        host_allowlist=("testserver",),
        storage=storage,
    )
    async with lifespan_manager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            prm = (await http.get("/.well-known/oauth-protected-resource")).json()
            resource = prm["resource"]
            assert resource == CANONICAL

            reg = await http.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
            client_id = reg.json()["client_id"]

            auth = await http.get(
                "/oauth/authorize",
                params={**_authorize_params(client_id), "resource": resource},
                headers=CF,
                follow_redirects=False,
            )
            code = parse_qs(urlparse(auth.headers["location"]).query)["code"][0]

            tok = await http.post(
                "/oauth/token", data={**_code_form(client_id, code), "resource": resource}
            )
            assert tok.status_code == 200, tok.text
            first = await _mcp_initialize(http, tok.json()["access_token"])
            assert first.status_code == 200, f"{first.status_code} {first.text}"

            ref = await http.post(
                "/oauth/token",
                data={
                    **_refresh_form(client_id, tok.json()["refresh_token"]),
                    "resource": resource,
                },
            )
            assert ref.status_code == 200, ref.text
            second = await _mcp_initialize(http, ref.json()["access_token"])
            assert second.status_code == 200, f"{second.status_code} {second.text}"
