"""JWT and opaque token primitives — pure functions, no DB access.

The explicit `issuer=` and `audience=` kwargs to jwt.decode are what give us
the iss/aud validation the SDK's built-in BearerAuth-only path lacks (open
SDK issues #1443 / #1445). Both kwargs are required at verify time; omitting
either is a bug.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

import jwt

ACCESS_TOKEN_TTL_SECONDS = 3600           # 1 hour
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
AUTHORIZATION_CODE_TTL_SECONDS = 600      # 10 minutes


def _now() -> int:
    import time

    return int(time.time())


# ---------------------------------------------------------------------------
# JWT (HS256)
# ---------------------------------------------------------------------------


def issue_access_token(
    *,
    sub: str,
    client_id: str,
    scope: str,
    signing_key: str,
    issuer: str,
    audience: str,
    jti: str | None = None,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
) -> str:
    """Mint an HS256 access token with standard + custom claims."""
    now = _now()
    payload = {
        "iss": issuer,
        "sub": sub,
        "aud": audience,
        "iat": now,
        "exp": now + int(ttl_seconds),
        "jti": jti or secrets.token_urlsafe(8),
        "client_id": client_id,
        "scope": scope or "",
    }
    return jwt.encode(payload, signing_key, algorithm="HS256")


def verify_access_token(
    token: str,
    *,
    signing_key: str,
    issuer: str,
    audience: str,
) -> dict:
    """Decode + validate iss/aud/exp/sub. Raises jwt.InvalidTokenError on failure."""
    return jwt.decode(
        token,
        signing_key,
        algorithms=["HS256"],
        issuer=issuer,
        audience=audience,
        options={"require": ["exp", "iss", "aud", "sub"]},
    )


# ---------------------------------------------------------------------------
# Opaque tokens
# ---------------------------------------------------------------------------


def new_authorization_code() -> str:
    return secrets.token_urlsafe(32)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    """Constant-time PKCE S256 verification per RFC 7636 §4.6."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(computed, challenge)
