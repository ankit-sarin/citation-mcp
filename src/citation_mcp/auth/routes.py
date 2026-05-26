"""OAuth 2.1 endpoints: /oauth/register (RFC 7591 DCR),
/oauth/authorize (RFC 6749 §4.1 + RFC 7636 PKCE), /oauth/token
(authorization_code + refresh_token grants with rotation).

Identity bridge: GET /oauth/authorize reads ``Cf-Access-Authenticated-User-
Email`` set by Cloudflare Access in production. Trust boundary is at the
edge; the AS does not verify the accompanying Cf-Access-Jwt-Assertion.
In dev (``OAUTH_ALLOW_MISSING_CF_EMAIL=true``) the header may be absent
and a configurable fallback email is used.

Error shape: RFC 6749 §5.2 — JSON body ``{"error": "...", "error_description":
"..."}`` for token-endpoint failures; query-string error redirect for
authorize-endpoint failures (RFC 6749 §4.1.2.1).

Why we hand-roll the token endpoint: the SDK's
OAuthAuthorizationServerProvider currently returns the wrong RFC 6749
error code (``unauthorized_client`` instead of ``invalid_client``) and the
wrong status (400 instead of 401) for client-lookup failures. Implementing
the route ourselves sidesteps both.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlencode, urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import tokens as tk
from .storage import OAuthStorage, new_chain_id
from .models import RefreshReuseDetected, RefreshTokenRecord

logger = logging.getLogger("citation_mcp.auth.routes")

CF_ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class OAuthConfig:
    storage: OAuthStorage
    signing_key: str
    issuer: str
    audience: str
    allow_missing_cf_email: bool = False
    dev_user_email: str = "dev@localhost"


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _error_json(status: int, code: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description},
        status_code=status,
    )


def _redirect_error(
    redirect_uri: str, code: str, description: str, state: str | None
) -> RedirectResponse:
    params = {"error": code, "error_description": description}
    if state is not None:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


def _is_valid_redirect_uri(uri: str) -> bool:
    try:
        p = urlparse(uri)
    except Exception:
        return False
    if p.scheme == "https" and p.netloc:
        return True
    # Allow http://localhost / 127.0.0.1 for local development.
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"):
        return True
    return False


# ---------------------------------------------------------------------------
# /oauth/register — RFC 7591 Dynamic Client Registration
# ---------------------------------------------------------------------------


_ALLOWED_GRANT_TYPES = {"authorization_code", "refresh_token"}


def _make_register_handler(
    cfg: OAuthConfig,
) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct != "application/json":
            return _error_json(
                415,
                "invalid_request",
                "Content-Type must be application/json",
            )

        try:
            body = await request.json()
        except Exception:
            return _error_json(400, "invalid_client_metadata", "request body is not valid JSON")

        if not isinstance(body, dict):
            return _error_json(400, "invalid_client_metadata", "body must be a JSON object")

        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(u, str) and _is_valid_redirect_uri(u) for u in redirect_uris)
        ):
            return _error_json(
                400,
                "invalid_client_metadata",
                "redirect_uris is required and each entry must be an https:// URL (http://localhost is allowed)",
            )

        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            return _error_json(
                400,
                "invalid_client_metadata",
                'only token_endpoint_auth_method="none" (public client) is supported',
            )

        grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grant_types, list) or not set(grant_types).issubset(_ALLOWED_GRANT_TYPES):
            return _error_json(
                400,
                "invalid_client_metadata",
                f"grant_types must be a subset of {sorted(_ALLOWED_GRANT_TYPES)}",
            )

        response_types = body.get("response_types") or ["code"]
        if not isinstance(response_types, list) or "code" not in response_types:
            return _error_json(
                400,
                "invalid_client_metadata",
                'response_types must include "code"',
            )

        metadata = {
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "response_types": response_types,
            "token_endpoint_auth_method": "none",
        }
        for key in ("client_name", "scope"):
            if key in body and isinstance(body[key], str):
                metadata[key] = body[key]

        _, registered = await cfg.storage.register_client(metadata)
        return JSONResponse(registered, status_code=201)

    return handler


# ---------------------------------------------------------------------------
# /oauth/authorize — Authorization endpoint
# ---------------------------------------------------------------------------


def _make_authorize_handler(
    cfg: OAuthConfig,
) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        params = request.query_params
        client_id = params.get("client_id")
        redirect_uri = params.get("redirect_uri")
        response_type = params.get("response_type")
        code_challenge = params.get("code_challenge")
        code_challenge_method = params.get("code_challenge_method")
        state = params.get("state")
        scope = params.get("scope")
        resource = params.get("resource")

        # ----- 1. Validate client + redirect_uri (errors cannot be redirected) -----

        if not client_id:
            return _error_json(400, "invalid_request", "client_id is required")
        client_meta = await cfg.storage.get_client(client_id)
        if client_meta is None:
            return _error_json(400, "invalid_client", "unknown client_id")

        if not redirect_uri:
            return _error_json(400, "invalid_request", "redirect_uri is required")
        registered_redirects = client_meta.get("redirect_uris") or []
        if redirect_uri not in registered_redirects:
            return _error_json(
                400, "invalid_request", "redirect_uri does not match a registered value"
            )

        # ----- 2. Validate the rest of the request (errors can be redirected) -----

        if response_type != "code":
            return _redirect_error(
                redirect_uri,
                "unsupported_response_type",
                'response_type must be "code"',
                state,
            )
        if not code_challenge:
            return _redirect_error(
                redirect_uri, "invalid_request", "code_challenge is required", state
            )
        if code_challenge_method != "S256":
            return _redirect_error(
                redirect_uri,
                "invalid_request",
                'code_challenge_method must be "S256" (plain is disallowed by OAuth 2.1)',
                state,
            )

        # ----- 3. Identity bridge -----

        user_email = request.headers.get(CF_ACCESS_EMAIL_HEADER)
        if not user_email:
            if not cfg.allow_missing_cf_email:
                return _error_json(
                    401,
                    "access_denied",
                    "Cloudflare Access header missing; this server expects to run behind Cloudflare Access",
                )
            user_email = cfg.dev_user_email

        # ----- 4. Issue authorization code -----

        code = tk.new_authorization_code()
        await cfg.storage.store_authorization_code(
            code,
            client_id=client_id,
            user_email=user_email,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scope=scope,
            resource=resource,
            ttl_seconds=tk.AUTHORIZATION_CODE_TTL_SECONDS,
        )

        cb_params = {"code": code}
        if state is not None:
            cb_params["state"] = state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(
            f"{redirect_uri}{sep}{urlencode(cb_params)}", status_code=302
        )

    return handler


# ---------------------------------------------------------------------------
# /oauth/token — Token endpoint (authorization_code + refresh_token grants)
# ---------------------------------------------------------------------------


def _issue_token_pair(
    cfg: OAuthConfig,
    *,
    sub: str,
    client_id: str,
    scope: str,
    resource: str | None,
    refresh_token: str,
) -> dict:
    audience = resource or cfg.audience
    access_token = tk.issue_access_token(
        sub=sub,
        client_id=client_id,
        scope=scope,
        signing_key=cfg.signing_key,
        issuer=cfg.issuer,
        audience=audience,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": tk.ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": refresh_token,
        "scope": scope or "",
    }


def _make_token_handler(
    cfg: OAuthConfig,
) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ct != "application/x-www-form-urlencoded":
            return _error_json(
                415,
                "invalid_request",
                "Content-Type must be application/x-www-form-urlencoded",
            )

        form = await request.form()
        grant_type = form.get("grant_type")
        client_id = form.get("client_id")

        if not client_id:
            return _error_json(400, "invalid_request", "client_id is required")
        client_meta = await cfg.storage.get_client(client_id)
        if client_meta is None:
            return _error_json(401, "invalid_client", "unknown client_id")

        if grant_type == "authorization_code":
            code = form.get("code")
            redirect_uri = form.get("redirect_uri")
            code_verifier = form.get("code_verifier")
            if not code or not redirect_uri or not code_verifier:
                return _error_json(
                    400,
                    "invalid_request",
                    "code, redirect_uri, and code_verifier are required",
                )

            record = await cfg.storage.consume_authorization_code(code)
            if record is None:
                return _error_json(
                    400, "invalid_grant", "authorization code is invalid, expired, or already used"
                )
            if record.client_id != client_id:
                return _error_json(
                    400, "invalid_grant", "authorization code was issued to a different client"
                )
            if record.redirect_uri != redirect_uri:
                return _error_json(
                    400, "invalid_grant", "redirect_uri does not match the value used at /authorize"
                )
            if not tk.verify_pkce_s256(code_verifier, record.code_challenge):
                return _error_json(
                    400, "invalid_grant", "PKCE code_verifier does not match code_challenge"
                )

            refresh = tk.new_refresh_token()
            chain_id = new_chain_id()
            await cfg.storage.store_refresh_token(
                refresh,
                client_id=client_id,
                user_email=record.user_email,
                chain_id=chain_id,
                parent_hash=None,
                scope=record.scope,
                ttl_seconds=tk.REFRESH_TOKEN_TTL_SECONDS,
            )
            payload = _issue_token_pair(
                cfg,
                sub=record.user_email,
                client_id=client_id,
                scope=record.scope or "",
                resource=record.resource,
                refresh_token=refresh,
            )
            return JSONResponse(payload, status_code=200)

        if grant_type == "refresh_token":
            presented = form.get("refresh_token")
            requested_scope = form.get("scope")
            requested_resource = form.get("resource")
            if not presented:
                return _error_json(
                    400, "invalid_request", "refresh_token is required"
                )

            existing = await cfg.storage.get_refresh_token(presented)
            if existing is None:
                # Unknown — return invalid_grant; storage cannot revoke a chain it
                # has no record of.
                return _error_json(400, "invalid_grant", "refresh token is invalid")
            if existing.client_id != client_id:
                return _error_json(
                    400, "invalid_grant", "refresh token was issued to a different client"
                )

            new_token = tk.new_refresh_token()
            result = await cfg.storage.rotate_refresh_token(
                presented, new_token, ttl_seconds=tk.REFRESH_TOKEN_TTL_SECONDS
            )
            if isinstance(result, RefreshReuseDetected):
                return _error_json(
                    400,
                    "invalid_grant",
                    "refresh token reuse detected; chain revoked",
                )
            assert isinstance(result, RefreshTokenRecord)

            scope = existing.scope or ""
            # Spec: requested scope must be a subset of the original.
            if requested_scope is not None:
                original = set((existing.scope or "").split())
                requested = set(requested_scope.split())
                if not requested.issubset(original):
                    return _error_json(
                        400, "invalid_scope", "requested scope exceeds the original grant"
                    )
                scope = requested_scope

            payload = _issue_token_pair(
                cfg,
                sub=existing.user_email,
                client_id=client_id,
                scope=scope,
                resource=requested_resource,
                refresh_token=new_token,
            )
            return JSONResponse(payload, status_code=200)

        return _error_json(
            400, "unsupported_grant_type", f"unsupported grant_type: {grant_type!r}"
        )

    return handler


# ---------------------------------------------------------------------------
# Route builder
# ---------------------------------------------------------------------------


def build_oauth_routes(cfg: OAuthConfig) -> list[Route]:
    return [
        Route("/oauth/register", _make_register_handler(cfg), methods=["POST"]),
        Route("/oauth/authorize", _make_authorize_handler(cfg), methods=["GET"]),
        Route("/oauth/token", _make_token_handler(cfg), methods=["POST"]),
    ]


def load_config_from_env(storage: OAuthStorage) -> OAuthConfig:
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
            "OAUTH_SIGNING_KEY must be hex-encoded. Generate one with: openssl rand -hex 32"
        ) from e
    if len(decoded) < 32:
        raise RuntimeError(
            "OAUTH_SIGNING_KEY must decode to at least 32 bytes. "
            "Generate one with: openssl rand -hex 32"
        )

    issuer = os.environ.get("OAUTH_ISSUER", "https://citation-mcp.digitalsurgeon.dev")
    audience = os.environ.get("OAUTH_AUDIENCE", issuer)
    allow_missing = os.environ.get("OAUTH_ALLOW_MISSING_CF_EMAIL", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    dev_email = os.environ.get("OAUTH_DEV_USER_EMAIL", "dev@localhost")
    return OAuthConfig(
        storage=storage,
        signing_key=signing_key,
        issuer=issuer,
        audience=audience,
        allow_missing_cf_email=allow_missing,
        dev_user_email=dev_email,
    )
