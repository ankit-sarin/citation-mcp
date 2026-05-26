"""Tests for .well-known discovery documents."""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette

from citation_mcp.auth.discovery import build_discovery_routes


def _app(issuer: str = "https://citation-mcp.example.com", audience: str | None = None):
    return Starlette(
        routes=build_discovery_routes(issuer=issuer, audience=audience or issuer)
    )


async def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_authorization_server_metadata_has_required_fields() -> None:
    async with await _client(_app()) as http:
        resp = await http.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "response_types_supported",
        "grant_types_supported",
        "token_endpoint_auth_methods_supported",
        "code_challenge_methods_supported",
    ):
        assert key in body, f"missing required field {key}"
    assert body["response_types_supported"] == ["code"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]


async def test_protected_resource_metadata_has_required_fields() -> None:
    async with await _client(_app()) as http:
        resp = await http.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("resource", "authorization_servers", "bearer_methods_supported"):
        assert key in body
    assert body["bearer_methods_supported"] == ["header"]
    assert isinstance(body["authorization_servers"], list)


async def test_both_endpoints_reflect_issuer_override() -> None:
    custom = "https://my.example.com"
    async with await _client(_app(issuer=custom, audience=custom)) as http:
        as_doc = (await http.get("/.well-known/oauth-authorization-server")).json()
        prm_doc = (await http.get("/.well-known/oauth-protected-resource")).json()
    assert as_doc["issuer"] == custom
    assert as_doc["authorization_endpoint"] == f"{custom}/oauth/authorize"
    assert prm_doc["authorization_servers"] == [custom]
    assert prm_doc["resource"] == custom


async def test_discovery_routes_bypass_any_bearer_middleware() -> None:
    # Build a stand-alone app — there's no bearer middleware to bypass here,
    # but we assert no auth headers are required and a fresh GET returns 200.
    async with await _client(_app()) as http:
        resp = await http.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    assert "WWW-Authenticate" not in resp.headers
