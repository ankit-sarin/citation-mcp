"""Streamable HTTP transport for citation-mcp with CVE-driven middleware.

Composition:
  outer Starlette app
  └── middleware (outermost first)
      ├── HostHeaderMiddleware       — CVE-2026-35568 (Java SDK DNS rebinding)
      ├── McpOriginMiddleware        — CVE-2026-33252 (Go SDK cross-site POST)
      └── McpContentTypeMiddleware   — CVE-2026-33252 belt-and-suspenders
  └── routes (checked in order)
      ├── /healthz
      ├── /.well-known/oauth-authorization-server  (RFC 8414)
      ├── /.well-known/oauth-protected-resource    (RFC 9728, overrides SDK)
      ├── /oauth/register, /oauth/authorize, /oauth/token
      └── Mount("/", app=sdk_app)   — SDK provides /mcp with bearer auth

The SDK app's auto-registered /.well-known/oauth-protected-resource is
unreachable because our outer route matches first.

The Python SDK ≥1.23 already validates Host internally (CVE-2025-66416);
HostHeaderMiddleware is belt-and-suspenders that also catches the broader
path beyond /mcp.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import AsyncIterator

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth.discovery import build_discovery_routes
from .auth.routes import OAuthConfig, build_oauth_routes, load_config_from_env
from .auth.storage import OAuthStorage
from .auth.verifier import CitationMcpTokenVerifier
from .server import build_lifespan, register_tools

logger = logging.getLogger("citation_mcp.http")

DEFAULT_ORIGIN_ALLOWLIST = ("https://claude.ai",)
DEFAULT_HOST_ALLOWLIST = (
    "citation-mcp.digitalsurgeon.dev",
    "localhost",
    "127.0.0.1",
)


# ---------------------------------------------------------------------------
# CVE-driven middlewares
# ---------------------------------------------------------------------------


def _strip_host_port(host: str) -> str:
    # Host header may carry :port; allowlist is host-only.
    return host.split(":", 1)[0].lower()


class HostHeaderMiddleware:
    """Reject requests whose Host header is not in the allowlist (421).

    Defends against CVE-2026-35568 (Java SDK DNS rebinding). Applied
    globally — not only to /mcp — so the AS endpoints are protected too.
    """

    def __init__(self, app: ASGIApp, *, allowlist: tuple[str, ...]):
        self.app = app
        self.allowlist = tuple(_strip_host_port(h) for h in allowlist)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if _strip_host_port(host) not in self.allowlist:
            resp = PlainTextResponse(
                "Misdirected Request: Host header not in allowlist.",
                status_code=421,
            )
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


class McpOriginMiddleware:
    """Reject /mcp POSTs whose Origin header is set and not allowlisted (403).

    Defends against CVE-2026-33252 (cross-site POST). Absent Origin is allowed
    (server-to-server, native clients). Only applies under /mcp; OAuth flows
    are not browser-initiated MCP traffic.
    """

    def __init__(self, app: ASGIApp, *, allowlist: tuple[str, ...]):
        self.app = app
        self.allowlist = tuple(allowlist)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        origin = headers.get("origin")
        if origin and origin not in self.allowlist:
            resp = JSONResponse(
                {"error": "forbidden", "error_description": "Origin not allowlisted"},
                status_code=403,
            )
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


class McpContentTypeMiddleware:
    """Reject /mcp POSTs whose Content-Type is not application/json (415).

    Defends against CVE-2026-33252 — the cross-site form-POST vector relies on
    text/plain / x-www-form-urlencoded; enforcing application/json closes it.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        ct = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct != "application/json":
            resp = JSONResponse(
                {"error": "unsupported_media_type", "error_description": "Content-Type must be application/json"},
                status_code=415,
            )
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _parse_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    parts = tuple(p.strip() for p in value.split(",") if p.strip())
    return parts or default


def build_http_app(
    *,
    signing_key: str,
    issuer: str,
    audience: str,
    origin_allowlist: tuple[str, ...] = DEFAULT_ORIGIN_ALLOWLIST,
    host_allowlist: tuple[str, ...] = DEFAULT_HOST_ALLOWLIST,
    oauth_db_path: str | None = None,
    storage: OAuthStorage | None = None,
) -> Starlette:
    """Build the outer Starlette app wiring SDK /mcp + our OAuth surface."""

    # 1. FastMCP with our verifier and AuthSettings (resource-server mode).
    verifier = CitationMcpTokenVerifier(
        signing_key=signing_key, issuer=issuer, audience=audience
    )
    auth_settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=audience,
        required_scopes=[],
    )
    mcp = FastMCP(
        name="citation-mcp",
        stateless_http=True,
        token_verifier=verifier,
        auth=auth_settings,
        lifespan=build_lifespan(),
    )
    register_tools(mcp)

    sdk_app = mcp.streamable_http_app()

    # 2. OAuth storage + config. Tests inject their own; CLI hands one in.
    if storage is None:
        storage = OAuthStorage(oauth_db_path)
    oauth_cfg = OAuthConfig(
        storage=storage,
        signing_key=signing_key,
        issuer=issuer,
        audience=audience,
        allow_missing_cf_email=os.environ.get(
            "OAUTH_ALLOW_MISSING_CF_EMAIL", "false"
        ).lower() in ("1", "true", "yes"),
        dev_user_email=os.environ.get("OAUTH_DEV_USER_EMAIL", "dev@localhost"),
    )

    # 3. Healthz — unauthenticated, no CVE middleware (not under /mcp).
    async def healthz(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # 4. Compose routes. Order matters: our routes match before the SDK mount.
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        *build_discovery_routes(issuer=issuer, audience=audience),
        *build_oauth_routes(oauth_cfg),
        Mount("/", app=sdk_app),
    ]

    # 5. Outer lifespan: storage init + chain into the SDK app's lifespan
    # (session_manager.run()) — Starlette does not propagate lifespan into
    # mounted apps automatically.
    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await storage.init()
        try:
            async with sdk_app.router.lifespan_context(sdk_app):
                yield
        finally:
            await storage.close()

    middleware = [
        Middleware(HostHeaderMiddleware, allowlist=host_allowlist),
        Middleware(McpOriginMiddleware, allowlist=origin_allowlist),
        Middleware(McpContentTypeMiddleware),
    ]

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def build_http_app_from_env() -> Starlette:
    """CLI / production entry point — reads all required + optional env vars."""
    signing_key = os.environ.get("OAUTH_SIGNING_KEY")
    if not signing_key:
        raise RuntimeError(
            "OAUTH_SIGNING_KEY is required for HTTP transport. "
            "Generate one with: openssl rand -hex 32"
        )
    try:
        decoded = bytes.fromhex(signing_key)
    except ValueError as e:
        raise RuntimeError(
            "OAUTH_SIGNING_KEY must be hex-encoded. "
            "Generate one with: openssl rand -hex 32"
        ) from e
    if len(decoded) < 32:
        raise RuntimeError(
            "OAUTH_SIGNING_KEY must decode to at least 32 bytes. "
            "Generate one with: openssl rand -hex 32"
        )

    issuer = os.environ.get("OAUTH_ISSUER", "https://citation-mcp.digitalsurgeon.dev")
    audience = os.environ.get("OAUTH_AUDIENCE", issuer)
    origin_allowlist = _parse_csv(
        os.environ.get("MCP_ORIGIN_ALLOWLIST"), DEFAULT_ORIGIN_ALLOWLIST
    )
    host_allowlist = _parse_csv(
        os.environ.get("MCP_HOST_ALLOWLIST"), DEFAULT_HOST_ALLOWLIST
    )
    oauth_db_path = os.environ.get("OAUTH_DB_PATH")

    return build_http_app(
        signing_key=signing_key,
        issuer=issuer,
        audience=audience,
        origin_allowlist=origin_allowlist,
        host_allowlist=host_allowlist,
        oauth_db_path=oauth_db_path,
    )
