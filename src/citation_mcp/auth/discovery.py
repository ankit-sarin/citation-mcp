"""RFC 8414 + RFC 9728 discovery endpoints.

These are served by our own Starlette routes rather than relying on the
SDK's auto-registered handler — both documents need to advertise the
endpoints we actually expose (our /oauth/*, not the SDK provider's).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("citation_mcp.auth.discovery")


def _as_metadata(issuer: str) -> dict:
    base = issuer.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [],
        "service_documentation": "https://github.com/ankit-sarin/citation-mcp",
    }


def _prm_metadata(issuer: str, audience: str) -> dict:
    return {
        "resource": audience.rstrip("/"),
        "authorization_servers": [issuer.rstrip("/")],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/ankit-sarin/citation-mcp",
        "scopes_supported": [],
    }


def _warn_if_insecure(issuer: str) -> None:
    if issuer.startswith("http://") and "localhost" not in issuer and "127.0.0.1" not in issuer:
        logger.warning(
            "OAUTH_ISSUER is http:// and not a localhost — clients require https in production: %s",
            issuer,
        )


def build_discovery_routes(*, issuer: str, audience: str) -> list[Route]:
    _warn_if_insecure(issuer)

    async def authorization_server(_req: Request) -> JSONResponse:
        return JSONResponse(_as_metadata(issuer))

    async def protected_resource(_req: Request) -> JSONResponse:
        return JSONResponse(_prm_metadata(issuer, audience))

    return [
        Route(
            "/.well-known/oauth-authorization-server",
            authorization_server,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource",
            protected_resource,
            methods=["GET"],
        ),
    ]
