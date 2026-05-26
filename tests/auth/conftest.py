"""Shared fixtures for OAuth route tests."""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette

from citation_mcp.auth.routes import OAuthConfig, build_oauth_routes
from citation_mcp.auth.storage import OAuthStorage

SIGNING_KEY = "x" * 64
ISSUER = "https://citation-mcp.example.com"
AUDIENCE = "https://citation-mcp.example.com"


@pytest.fixture
async def storage() -> OAuthStorage:
    s = OAuthStorage(":memory:")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def make_app():
    """Return a factory that builds a Starlette app for a given OAuthStorage."""

    def factory(
        storage: OAuthStorage,
        *,
        allow_missing_cf_email: bool = False,
        dev_user_email: str = "dev@localhost",
    ) -> Starlette:
        cfg = OAuthConfig(
            storage=storage,
            signing_key=SIGNING_KEY,
            issuer=ISSUER,
            audience=AUDIENCE,
            allow_missing_cf_email=allow_missing_cf_email,
            dev_user_email=dev_user_email,
        )
        return Starlette(routes=build_oauth_routes(cfg))

    return factory


@pytest.fixture
def make_client():
    """Async-context factory returning an httpx.AsyncClient bound to an ASGI app."""

    def factory(app: Starlette) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    return factory
