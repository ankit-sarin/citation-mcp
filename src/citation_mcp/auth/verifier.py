"""TokenVerifier shim for the MCP SDK — decodes our HS256 JWTs.

The SDK calls verify_token() on every authenticated /mcp request. Returning
None signals 401 to the caller; returning an AccessToken authenticates the
request. iss/aud validation happens inside verify_access_token via the
explicit kwargs to jwt.decode (the SDK's built-in path doesn't validate
either — see SDK issues #1443 / #1445).
"""

from __future__ import annotations

import logging

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .tokens import verify_access_token

logger = logging.getLogger("citation_mcp.auth.verifier")


class CitationMcpTokenVerifier(TokenVerifier):
    def __init__(self, *, signing_key: str, issuer: str, audience: str):
        self._signing_key = signing_key
        self._issuer = issuer
        self._audience = audience

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = verify_access_token(
                token,
                signing_key=self._signing_key,
                issuer=self._issuer,
                audience=self._audience,
            )
        except jwt.InvalidTokenError as e:
            msg = str(e)
            logger.warning(
                "token verification failed: %s: %s",
                type(e).__name__,
                msg[:200] + ("…" if len(msg) > 200 else ""),
            )
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", ""),
            scopes=(claims.get("scope") or "").split(),
            expires_at=claims.get("exp"),
            resource=claims.get("aud"),
        )
